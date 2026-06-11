#!/usr/bin/env python3
"""Tests for the ports manifest (CONNECTORS.md — the connection standard).

These lock the three properties the conformance Action's `check_ports.py` step
asserts, so a manifest drift fails the Tests step too (belt and suspenders):

  1. ports.json parses and is well-formed.
  2. every declared input/output type exists in the shared vocabulary (types.json).
  3. decide() reads each declared input name under `state` and writes each declared
     output name under `output`.
"""

import json
import re
from pathlib import Path

from organ import decide

HERE = Path(__file__).parent
PORTS = json.loads((HERE / "ports.json").read_text())
VOCAB = json.loads((HERE / "types.json").read_text())
ORGAN_SRC = (HERE / "organ.py").read_text()


# --------------------------------------------------------------------------- #
# 1. Well-formed manifest
# --------------------------------------------------------------------------- #

def test_ports_has_inputs_and_outputs_lists():
    assert isinstance(PORTS.get("inputs"), list)
    assert isinstance(PORTS.get("outputs"), list)
    assert PORTS["outputs"], "must declare at least one output port"


def test_every_port_has_string_name_and_type():
    for kind in ("inputs", "outputs"):
        for port in PORTS[kind]:
            assert isinstance(port.get("name"), str) and port["name"]
            assert isinstance(port.get("type"), str) and port["type"]


def test_input_required_flags_are_bool_when_present():
    for port in PORTS["inputs"]:
        if "required" in port:
            assert isinstance(port["required"], bool)


# --------------------------------------------------------------------------- #
# 2. Types resolve against the shared vocabulary
# --------------------------------------------------------------------------- #

def test_all_declared_types_exist_in_vocabulary():
    known = set(VOCAB["types"].keys())
    for port in PORTS["inputs"] + PORTS["outputs"]:
        assert port["type"] in known, f"{port['name']} -> unknown type {port['type']}"


# --------------------------------------------------------------------------- #
# 3. decide reads/writes the declared names
# --------------------------------------------------------------------------- #

def test_decide_reads_every_declared_input_from_state():
    for port in PORTS["inputs"]:
        name = re.escape(port["name"])
        pat = re.compile(
            rf"""state(?:\.get\(\s*["']{name}["']|\s*\[\s*["']{name}["']\s*\])"""
        )
        assert pat.search(ORGAN_SRC), f"organ never reads state[{port['name']!r}]"


def test_decide_writes_every_declared_output():
    declared = {p["name"] for p in PORTS["outputs"]}
    # Run over empty state + every sample; each declared output must be present.
    cases = [{"state": {}}]
    for s in sorted((HERE / "samples").glob("*.json")):
        cases.append(json.loads(s.read_text()))
    for case in cases:
        out = decide(case.get("state", {}), case.get("context", {}))["output"]
        assert declared.issubset(out.keys()), (
            f"missing {declared - set(out.keys())} for case {case.get('state')}"
        )
