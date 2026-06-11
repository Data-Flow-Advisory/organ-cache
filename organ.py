#!/usr/bin/env python3
"""
Cache-Decision Organ — extracted pure caching policy from discovery-engine.

A pure, stdlib-only decider that reads {state, context} JSON on stdin (or via
the ORGAN_INPUT env var) and writes {output, rationale, self_metric} on stdout.

It does NOT touch Redis. The original `app/services/cache.py` mixed two things:
  1. side-effecting Redis I/O (connect / GET / SETEX / DELETE), and
  2. pure caching *policy* — the decisions taken around that I/O.

This organ extracts only (2): given a cache operation request and a config, it
decides whether caching is enabled, what canonical cache key to use, what TTL to
apply, and which action the spine should take (use the hit, compute on a miss,
store a value, skip storing a None result, or bypass caching entirely).

Faithful mapping from cache.py:
  - `is_configured()`      -> caching_enabled (bool(redis_url) / context.configured)
  - `cached` key building  -> cache_key derivation from key_prefix + key_parts
  - get()/get_json() hit   -> is_hit = (cached_value is not None)
  - the decorator's
      "if result is not None: set_json(...)"  -> should_store = value_present
  - set()'s default ttl=300 / clamping        -> ttl resolution + clamp

Deliberate improvement over the source: cache.py derives keys with Python's
builtin `hash(str(args)+str(kwargs))`, which is salted per process
(PYTHONHASHSEED) and therefore NON-deterministic across restarts — a latent
cache-miss-storm bug. This organ derives keys with a stable hashlib digest of a
canonicalised JSON form, so the same key_parts always map to the same key.

Contract:
  INPUT:  {
    "state": {
      "operation": "get" | "set" | "lookup" | "store",   # what's being attempted
      "key_prefix": "blueprint",                          # namespace for the key
      "key_parts": <any JSON>,                            # components hashed into the key
      "key": "explicit:key",                              # optional; overrides derivation
      "ttl": 300,                                          # requested TTL in seconds
      "value_present": true,                              # set/store: is the result non-None?
      "cached_value": <any or null>                        # get/lookup: what the store returned
    },
    "context": {
      "configured": true,           # caching backend available?
      "redis_url": "redis://...",   # presence implies configured (mirrors is_configured())
      "default_ttl": 300,
      "min_ttl": 1,
      "max_ttl": 86400
    }
  }

  OUTPUT: {
    "output": {
      "caching_enabled": true,
      "cache_key": "blueprint:ab12cd34ef56...",
      "ttl": 300,
      "action": "hit" | "miss" | "store" | "skip_store" | "bypass",
      "is_hit": false,
      "should_store": false
    },
    "rationale": "<why>",
    "self_metric": {"confidence": 0.0, ...}
  }

The organ is pure: all inputs arrive via JSON, it makes no DB/network calls,
it is deterministic, and it fails safe to a cache *bypass* (never a confident
false "hit") on malformed or empty state.
"""

import hashlib
import json
import os
import sys
from typing import Any, Dict, Tuple


# Operations that read the cache.
_READ_OPS = {"get", "lookup", "read", "fetch"}
# Operations that write the cache.
_WRITE_OPS = {"set", "store", "put", "write"}

# Source-of-truth defaults, mirroring cache.py's ttl=300 default.
_DEFAULT_TTL = 300
_DEFAULT_MIN_TTL = 1
_DEFAULT_MAX_TTL = 86400  # 24h — a sane ceiling; cache.py imposed none.

# Length of the hex digest used in derived keys (16 hex chars = 64 bits).
_KEY_DIGEST_LEN = 16


def _is_caching_enabled(context: Dict[str, Any]) -> bool:
    """Mirror cache.py::is_configured() in pure form.

    Caching is enabled when the caller declares a configured backend, either
    explicitly via `configured: true` or implicitly via a non-empty `redis_url`.
    Absent / falsey -> disabled (conservative).
    """
    if not isinstance(context, dict):
        return False
    if "configured" in context:
        return bool(context.get("configured"))
    return bool(context.get("redis_url"))


def _canonicalise(value: Any) -> str:
    """Produce a stable string form of arbitrary JSON for hashing.

    Uses sorted keys and compact separators so equivalent dicts in any key
    order hash identically. Falls back to repr() for non-JSON-serialisable
    inputs so derivation never raises.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return repr(value)


def _derive_key(key_prefix: str, key_parts: Any, explicit_key: Any) -> str:
    """Build the canonical cache key.

    - An explicit `key` always wins (caller already knows the exact key).
    - Otherwise: `{key_prefix}:{stable_digest(key_parts)}`.
    - With no prefix and no parts, returns "" (no derivable key).

    The digest is a deterministic SHA-256 over the canonical JSON of key_parts,
    replacing cache.py's per-process-salted builtin hash().
    """
    if isinstance(explicit_key, str) and explicit_key:
        return explicit_key

    prefix = str(key_prefix) if key_prefix not in (None, "") else ""

    has_parts = key_parts not in (None, "", [], {})
    if not has_parts:
        # Nothing to hash. If we at least have a prefix, that's the key.
        return prefix

    digest = hashlib.sha256(_canonicalise(key_parts).encode("utf-8")).hexdigest()[:_KEY_DIGEST_LEN]
    return f"{prefix}:{digest}" if prefix else digest


def _resolve_ttl(state: Dict[str, Any], context: Dict[str, Any]) -> Tuple[int, bool]:
    """Resolve and clamp the TTL.

    Precedence: state.ttl -> context.default_ttl -> _DEFAULT_TTL. An invalid /
    non-positive requested TTL falls back to the default. The result is clamped
    into [min_ttl, max_ttl].

    Returns (ttl, was_adjusted) where was_adjusted is True when the requested
    value was missing, invalid, or had to be clamped.
    """
    default_ttl = _coerce_int(context.get("default_ttl"), _DEFAULT_TTL)
    min_ttl = _coerce_int(context.get("min_ttl"), _DEFAULT_MIN_TTL)
    max_ttl = _coerce_int(context.get("max_ttl"), _DEFAULT_MAX_TTL)
    if min_ttl > max_ttl:
        min_ttl, max_ttl = max_ttl, min_ttl

    requested = state.get("ttl", None)
    adjusted = False

    ttl = _coerce_int(requested, None)
    if ttl is None or ttl <= 0:
        ttl = default_ttl
        adjusted = requested is not None  # caller asked for something invalid

    if ttl < min_ttl:
        ttl, adjusted = min_ttl, True
    elif ttl > max_ttl:
        ttl, adjusted = max_ttl, True

    return ttl, adjusted


def _coerce_int(value: Any, fallback: Any) -> Any:
    """Best-effort int coercion; returns `fallback` on failure."""
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return fallback
    return fallback


def decide(state: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Pure cache-policy decision.

    Returns {output, rationale, self_metric} per CONTRACT.md.
    """
    if not isinstance(state, dict):
        state = {}
    if not isinstance(context, dict):
        context = {}

    enabled = _is_caching_enabled(context)
    operation = str(state.get("operation", "") or "").strip().lower()
    key_prefix = state.get("key_prefix", "")
    key = _derive_key(key_prefix, state.get("key_parts"), state.get("key"))
    ttl, ttl_adjusted = _resolve_ttl(state, context)

    is_read = operation in _READ_OPS
    is_write = operation in _WRITE_OPS

    # A read is a hit only when the store actually returned something.
    is_hit = bool(is_read and state.get("cached_value") is not None and enabled)
    # A write stores only a non-None result (mirrors the decorator guard).
    value_present = bool(state.get("value_present"))
    should_store = bool(is_write and value_present and enabled)

    # Decide the action the spine should take.
    if not enabled:
        action = "bypass"
    elif not key:
        # Caching is on but we can't form a key -> we cannot safely cache.
        action = "bypass"
    elif is_read:
        action = "hit" if is_hit else "miss"
    elif is_write:
        action = "store" if should_store else "skip_store"
    else:
        # Unknown operation -> conservative bypass.
        action = "bypass"

    output = {
        "caching_enabled": enabled,
        "cache_key": key,
        "ttl": ttl,
        "action": action,
        "is_hit": is_hit,
        "should_store": should_store,
    }

    rationale = _build_rationale(
        enabled=enabled,
        operation=operation,
        is_read=is_read,
        is_write=is_write,
        key=key,
        action=action,
        is_hit=is_hit,
        should_store=should_store,
        value_present=value_present,
        ttl=ttl,
        ttl_adjusted=ttl_adjusted,
    )

    confidence = _confidence(
        enabled=enabled,
        operation=operation,
        is_read=is_read,
        is_write=is_write,
        key=key,
        action=action,
    )

    self_metric = {
        "confidence": confidence,
        "caching_enabled": enabled,
        "operation_recognised": bool(is_read or is_write),
        "key_derived": bool(key),
        "ttl": ttl,
        "ttl_adjusted": ttl_adjusted,
        "action": action,
    }

    return {"output": output, "rationale": rationale, "self_metric": self_metric}


def _build_rationale(**kw: Any) -> str:
    if not kw["enabled"]:
        return (
            "Caching backend not configured (mirrors is_configured() == False) — "
            f"action=bypass; the spine should compute directly. TTL resolved to "
            f"{kw['ttl']}s but is unused while bypassing."
        )
    if not kw["key"]:
        return (
            "Caching enabled but no cache key could be derived (no explicit key, "
            "prefix, or key_parts) — action=bypass to avoid an unsafe blind cache."
        )
    if kw["is_read"]:
        verdict = "HIT" if kw["is_hit"] else "MISS"
        return (
            f"Read op '{kw['operation']}' on key resolved to {verdict}: "
            f"cached_value {'present' if kw['is_hit'] else 'absent'}. "
            f"action={kw['action']}."
            + ("" if kw["is_hit"] else " Spine should compute, then store the result if non-None.")
        )
    if kw["is_write"]:
        if kw["should_store"]:
            return (
                f"Write op '{kw['operation']}' with a non-None value — action=store "
                f"with ttl={kw['ttl']}s (mirrors 'if result is not None: set_json')."
            )
        return (
            f"Write op '{kw['operation']}' but value_present is False — action=skip_store; "
            "None results are not cached (mirrors the decorator guard)."
        )
    return (
        f"Unrecognised operation {kw['operation']!r} — action=bypass (conservative). "
        "Expected one of get/lookup/set/store."
    )


def _confidence(**kw: Any) -> float:
    """Confidence the decision is well-grounded.

    High when the backend state and operation are both unambiguous; lower when
    we had to fall back to a conservative bypass on an unknown operation.
    """
    if not kw["enabled"]:
        # is_configured() is a crisp boolean — bypass is certainly correct.
        return 1.0
    if not kw["key"]:
        return 0.6  # enabled but unusable input
    if kw["is_read"] or kw["is_write"]:
        return 0.95
    # Enabled backend but operation unknown — we bypassed, but unsure that's intended.
    return 0.3


def run_organ(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Top-level entry: parse {state, context}, never raise (fail-safe)."""
    try:
        if not isinstance(input_data, dict):
            input_data = {}
        state = input_data.get("state", {})
        context = input_data.get("context", {})
        return decide(state, context)
    except Exception as exc:  # fail-safe: conservative bypass
        return {
            "output": {
                "caching_enabled": False,
                "cache_key": "",
                "ttl": _DEFAULT_TTL,
                "action": "bypass",
                "is_hit": False,
                "should_store": False,
            },
            "rationale": f"Error during decision, failing safe to bypass: {exc}",
            "self_metric": {"confidence": 0.0},
        }


def main() -> None:
    """CLI entry: read JSON from ORGAN_INPUT (value or file path) or stdin."""
    try:
        input_str = os.environ.get("ORGAN_INPUT")
        if input_str:
            if os.path.isfile(input_str):
                with open(input_str, "r") as f:
                    input_str = f.read()
        else:
            input_str = sys.stdin.read()

        input_data = json.loads(input_str) if input_str.strip() else {}
        result = run_organ(input_data)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except json.JSONDecodeError as exc:
        json.dump(
            {
                "output": {
                    "caching_enabled": False,
                    "cache_key": "",
                    "ttl": _DEFAULT_TTL,
                    "action": "bypass",
                    "is_hit": False,
                    "should_store": False,
                },
                "rationale": f"Invalid JSON input: {exc}",
                "self_metric": {"confidence": 0.0},
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
