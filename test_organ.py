#!/usr/bin/env python3
"""Tests for the cache-decision organ."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from organ import (
    decide,
    run_organ,
    _derive_key,
    _resolve_ttl,
    _is_caching_enabled,
    _coerce_int,
    _DEFAULT_TTL,
)

HERE = Path(__file__).parent


# --------------------------------------------------------------------------- #
# caching_enabled (mirrors is_configured())
# --------------------------------------------------------------------------- #

def test_enabled_via_explicit_configured_true():
    assert _is_caching_enabled({"configured": True}) is True


def test_enabled_via_redis_url():
    assert _is_caching_enabled({"redis_url": "redis://localhost:6379"}) is True


def test_disabled_when_configured_false_overrides_redis_url():
    # explicit configured flag wins over redis_url presence
    assert _is_caching_enabled({"configured": False, "redis_url": "redis://x"}) is False


def test_disabled_when_empty():
    assert _is_caching_enabled({}) is False


def test_disabled_when_empty_redis_url():
    assert _is_caching_enabled({"redis_url": ""}) is False


def test_disabled_when_context_not_dict():
    assert _is_caching_enabled("nope") is False


# --------------------------------------------------------------------------- #
# key derivation
# --------------------------------------------------------------------------- #

def test_explicit_key_wins():
    assert _derive_key("ignored", {"a": 1}, "exact:key") == "exact:key"


def test_derived_key_has_prefix():
    key = _derive_key("blueprint", {"tenant": 5, "sig": "abc"}, None)
    assert key.startswith("blueprint:")
    assert len(key.split(":")[1]) == 16  # 16 hex chars


def test_key_derivation_is_deterministic():
    a = _derive_key("p", {"x": 1, "y": 2}, None)
    b = _derive_key("p", {"y": 2, "x": 1}, None)  # different key order
    assert a == b  # canonical JSON sorts keys -> stable digest


def test_different_parts_give_different_keys():
    a = _derive_key("p", {"x": 1}, None)
    b = _derive_key("p", {"x": 2}, None)
    assert a != b


def test_no_prefix_no_parts_gives_empty_key():
    assert _derive_key("", None, None) == ""


def test_prefix_only_no_parts_returns_prefix():
    assert _derive_key("standalone", None, None) == "standalone"


def test_non_serialisable_parts_do_not_raise():
    # set is not JSON-serialisable; derivation must still produce a key
    key = _derive_key("p", {1, 2, 3}, None)
    assert key.startswith("p:")


# --------------------------------------------------------------------------- #
# ttl resolution + clamping
# --------------------------------------------------------------------------- #

def test_default_ttl_when_missing():
    ttl, adjusted = _resolve_ttl({}, {})
    assert ttl == _DEFAULT_TTL
    assert adjusted is False


def test_requested_ttl_honoured():
    ttl, adjusted = _resolve_ttl({"ttl": 120}, {})
    assert ttl == 120
    assert adjusted is False


def test_context_default_ttl_used():
    ttl, _ = _resolve_ttl({}, {"default_ttl": 999})
    assert ttl == 999


def test_invalid_ttl_falls_back_and_flags_adjusted():
    ttl, adjusted = _resolve_ttl({"ttl": -5}, {})
    assert ttl == _DEFAULT_TTL
    assert adjusted is True


def test_ttl_clamped_to_max():
    ttl, adjusted = _resolve_ttl({"ttl": 1_000_000}, {"max_ttl": 3600})
    assert ttl == 3600
    assert adjusted is True


def test_ttl_clamped_to_min():
    ttl, adjusted = _resolve_ttl({"ttl": 1}, {"min_ttl": 60})
    assert ttl == 60
    assert adjusted is True


def test_swapped_min_max_are_corrected():
    # min > max should be auto-swapped, not crash
    ttl, _ = _resolve_ttl({"ttl": 50}, {"min_ttl": 100, "max_ttl": 10})
    assert 10 <= ttl <= 100


# --------------------------------------------------------------------------- #
# coerce_int
# --------------------------------------------------------------------------- #

def test_coerce_int_rejects_bool():
    assert _coerce_int(True, 99) == 99


def test_coerce_int_parses_string():
    assert _coerce_int("42", 0) == 42


def test_coerce_int_floors_float():
    assert _coerce_int(3.9, 0) == 3


def test_coerce_int_fallback_on_garbage():
    assert _coerce_int("abc", 7) == 7


# --------------------------------------------------------------------------- #
# decide() — read path
# --------------------------------------------------------------------------- #

def test_read_hit():
    r = decide(
        {"operation": "get", "key_prefix": "bp", "key_parts": {"t": 1}, "cached_value": {"x": 1}},
        {"configured": True},
    )
    assert r["output"]["action"] == "hit"
    assert r["output"]["is_hit"] is True
    assert r["self_metric"]["confidence"] == 0.95


def test_read_miss():
    r = decide(
        {"operation": "lookup", "key_prefix": "bp", "key_parts": {"t": 1}, "cached_value": None},
        {"configured": True},
    )
    assert r["output"]["action"] == "miss"
    assert r["output"]["is_hit"] is False


def test_read_hit_requires_enabled():
    # even with a cached_value, a disabled backend can't be a hit
    r = decide(
        {"operation": "get", "key_prefix": "bp", "key_parts": {"t": 1}, "cached_value": {"x": 1}},
        {"configured": False},
    )
    assert r["output"]["action"] == "bypass"
    assert r["output"]["is_hit"] is False


# --------------------------------------------------------------------------- #
# decide() — write path
# --------------------------------------------------------------------------- #

def test_write_store_non_none():
    r = decide(
        {"operation": "set", "key_prefix": "bp", "key_parts": {"t": 1}, "value_present": True, "ttl": 600},
        {"configured": True},
    )
    assert r["output"]["action"] == "store"
    assert r["output"]["should_store"] is True
    assert r["output"]["ttl"] == 600


def test_write_skip_store_when_value_absent():
    # mirrors "if result is not None: set_json(...)"
    r = decide(
        {"operation": "store", "key_prefix": "bp", "key_parts": {"t": 1}, "value_present": False},
        {"configured": True},
    )
    assert r["output"]["action"] == "skip_store"
    assert r["output"]["should_store"] is False


def test_write_disabled_bypasses():
    r = decide(
        {"operation": "set", "key_prefix": "bp", "key_parts": {"t": 1}, "value_present": True},
        {"configured": False},
    )
    assert r["output"]["action"] == "bypass"
    assert r["output"]["should_store"] is False


# --------------------------------------------------------------------------- #
# decide() — bypass / conservative paths
# --------------------------------------------------------------------------- #

def test_disabled_backend_confidence_full():
    r = decide({"operation": "get"}, {"configured": False})
    assert r["output"]["action"] == "bypass"
    assert r["self_metric"]["confidence"] == 1.0


def test_enabled_but_no_key_bypasses():
    r = decide({"operation": "get", "cached_value": "x"}, {"configured": True})
    assert r["output"]["action"] == "bypass"
    assert r["output"]["cache_key"] == ""
    assert r["self_metric"]["confidence"] == 0.6


def test_unknown_operation_bypasses_low_confidence():
    r = decide(
        {"operation": "frobnicate", "key_prefix": "bp", "key_parts": {"t": 1}},
        {"configured": True},
    )
    assert r["output"]["action"] == "bypass"
    assert r["self_metric"]["confidence"] == 0.3


def test_empty_state_fails_safe():
    r = decide({}, {})
    assert r["output"]["action"] == "bypass"
    assert r["output"]["is_hit"] is False
    assert r["output"]["should_store"] is False


def test_non_dict_state_does_not_raise():
    r = decide("not a dict", None)
    assert r["output"]["action"] == "bypass"


# --------------------------------------------------------------------------- #
# contract shape
# --------------------------------------------------------------------------- #

def test_output_shape_keys():
    r = decide({"operation": "get", "key_prefix": "p", "key_parts": {"a": 1}}, {"configured": True})
    assert set(r.keys()) == {"output", "rationale", "self_metric"}
    assert isinstance(r["rationale"], str) and r["rationale"]
    assert "confidence" in r["self_metric"]
    c = r["self_metric"]["confidence"]
    assert isinstance(c, (int, float)) and 0.0 <= c <= 1.0


def test_run_organ_normalises_none_to_clean_bypass():
    # None input normalises to empty state -> disabled backend -> crisp bypass.
    r = run_organ(None)
    assert r["output"]["action"] == "bypass"
    assert r["self_metric"]["confidence"] == 1.0


def test_run_organ_unparseable_state_fails_safe():
    # A state whose access raises forces the except path -> confidence 0.0.
    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    r = run_organ({"state": Boom(), "context": {}})
    assert r["output"]["action"] == "bypass"
    assert r["self_metric"]["confidence"] == 0.0


# --------------------------------------------------------------------------- #
# CLI / samples
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sample", sorted((HERE / "samples").glob("*.json")))
def test_samples_conform(sample):
    env = os.environ.copy()
    env["ORGAN_INPUT"] = str(sample)
    proc = subprocess.run(
        [sys.executable, str(HERE / "organ.py")],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert {"output", "rationale", "self_metric"} <= set(data)
    assert 0.0 <= data["self_metric"]["confidence"] <= 1.0


def test_cli_reads_stdin():
    proc = subprocess.run(
        [sys.executable, str(HERE / "organ.py")],
        input=json.dumps({"state": {"operation": "get", "key_prefix": "p",
                                     "key_parts": {"a": 1}, "cached_value": 1},
                          "context": {"configured": True}}),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["output"]["action"] == "hit"
