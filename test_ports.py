#!/usr/bin/env python3
"""Tests for the connection-standard port manifest (ports.json).

Mirrors check_ports.py so the conformance Action's pytest step also covers the
port checks (CONNECTORS.md, Data-Flow-Advisory/orchestrator).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _load(rel):
    return json.loads((HERE / rel).read_text())


def _vocabulary():
    vocab = set((_load("vocab/types.json").get("types") or {}))
    proposed_path = HERE / "vocab" / "proposed_types.json"
    if proposed_path.exists():
        vocab |= set((_load("vocab/proposed_types.json").get("types") or {}))
    return vocab


def test_ports_json_parses_with_shape():
    ports = _load("ports.json")
    assert isinstance(ports, dict)
    assert isinstance(ports["inputs"], list) and ports["inputs"]
    assert isinstance(ports["outputs"], list) and ports["outputs"]
    for p in ports["inputs"]:
        assert "name" in p and "type" in p
        if "required" in p:
            assert isinstance(p["required"], bool)
    for p in ports["outputs"]:
        assert "name" in p and "type" in p


def test_every_port_type_exists_in_vocabulary():
    vocab = _vocabulary()
    ports = _load("ports.json")
    for p in ports["inputs"] + ports["outputs"]:
        assert p["type"] in vocab, f"type {p['type']!r} not in vocabulary"


def test_vendored_vocabulary_types_block_unmodified():
    # The pinned snapshot's curated `types` block must stay intact; new types
    # this PR proposes live in vocab/proposed_types.json, never in the snapshot.
    snapshot = _load("vocab/types.json")
    assert snapshot["version"] == 1
    assert "Blueprint" in snapshot["types"]  # a known curated domain type
    assert "String" not in snapshot["types"]  # proposed types must NOT leak in


def test_decide_reads_every_declared_input_name():
    src = (HERE / "organ.py").read_text()
    for p in _load("ports.json")["inputs"]:
        name = p["name"]
        assert f'state.get("{name}"' in src or f'state["{name}"' in src, name


def test_decide_writes_every_declared_output_name():
    produced = set()
    for sample in sorted((HERE / "samples").glob("*.json")):
        env = os.environ.copy()
        env["ORGAN_INPUT"] = str(sample)
        proc = subprocess.run(
            [sys.executable, str(HERE / "organ.py")],
            env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        produced |= set(json.loads(proc.stdout)["output"].keys())
    for p in _load("ports.json")["outputs"]:
        assert p["name"] in produced, p["name"]


def test_every_declared_input_name_exercised_by_a_sample():
    states = [
        (json.loads(s.read_text()).get("state") or {})
        for s in (HERE / "samples").glob("*.json")
    ]
    for p in _load("ports.json")["inputs"]:
        assert any(p["name"] in st for st in states), p["name"]


def test_check_ports_script_passes():
    proc = subprocess.run(
        [sys.executable, str(HERE / "check_ports.py")],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
