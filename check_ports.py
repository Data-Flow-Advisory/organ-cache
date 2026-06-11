#!/usr/bin/env python3
"""Ports-manifest conformance check (the Lego-stud check — see CONNECTORS.md).

CONTRACT.md makes every organ uniform to *run*. CONNECTORS.md makes them uniform
to *connect*: a `ports.json` beside `organ.py` declares each input/output port and
pins it to a `type` from the shared vocabulary (`types.json`). The composer wires
by type; the substrate validates payloads against the type schema. An organ whose
ports manifest doesn't hold up is connectable-by-luck only.

This script asserts the three properties CONNECTORS.md requires of a conforming
organ, plus basic structural sanity:

  1. `ports.json` parses and is well-formed (inputs/outputs lists, each port has a
     string `name` and a string `type`; `required`, if present, is a bool).
  2. Every `type` referenced exists in the shared vocabulary (`types.json`).
  3. `decide` actually **reads each declared input name** under `state` (verified by
     source-scanning organ.py) and **writes each declared output name** under
     `output` (verified by running the organ on every sample + empty state).

Pure-stdlib, no third-party deps; mirrors check_contract.py's ::error:: style so a
drift goes RED in the conformance Action.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def _fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        _fail(f"{label} missing: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        _fail(f"{label} is not valid JSON ({path}): {exc}")


def _validate_ports_structure(ports: dict) -> tuple:
    """Property 1 — ports.json is well-formed. Returns (inputs, outputs)."""
    if not isinstance(ports, dict):
        _fail("ports.json top-level must be a JSON object")

    inputs = ports.get("inputs")
    outputs = ports.get("outputs")
    if not isinstance(inputs, list):
        _fail("ports.json: 'inputs' must be a list")
    if not isinstance(outputs, list):
        _fail("ports.json: 'outputs' must be a list")
    if not outputs:
        _fail("ports.json: 'outputs' must declare at least one output port")

    def _check_port(port, kind):
        if not isinstance(port, dict):
            _fail(f"ports.json: each {kind} port must be an object, got {port!r}")
        name = port.get("name")
        ptype = port.get("type")
        if not isinstance(name, str) or not name:
            _fail(f"ports.json: {kind} port missing a non-empty string 'name': {port!r}")
        if not isinstance(ptype, str) or not ptype:
            _fail(f"ports.json: {kind} port {name!r} missing a non-empty string 'type'")
        if kind == "input" and "required" in port and not isinstance(port["required"], bool):
            _fail(f"ports.json: input port {name!r} 'required' must be a bool")

    for p in inputs:
        _check_port(p, "input")
    for p in outputs:
        _check_port(p, "output")

    print(f"✓ ports.json well-formed: {len(inputs)} input(s), {len(outputs)} output(s)")
    return inputs, outputs


def _validate_types_resolve(inputs, outputs, vocab: dict) -> None:
    """Property 2 — every declared type exists in the shared vocabulary."""
    types = vocab.get("types")
    if not isinstance(types, dict) or not types:
        _fail("types.json: 'types' must be a non-empty object")
    known = set(types.keys())

    unknown = []
    for port in list(inputs) + list(outputs):
        if port["type"] not in known:
            unknown.append((port["name"], port["type"]))
    if unknown:
        for name, t in unknown:
            _fail(f"ports.json: port {name!r} declares type {t!r} which is not in the "
                  f"vocabulary (types.json). Add it to types.json (and propose it "
                  f"upstream) or use an existing type.")
    print(f"✓ all declared types resolve against the vocabulary ({len(known)} types known)")


def _validate_decide_reads_inputs(inputs) -> None:
    """Property 3a — decide reads each declared input name under `state`.

    Source-scan organ.py for a `state` read of each declared input name. The organ
    reads inputs via `state.get("<name>")` / `state["<name>"]`, so a name with no
    such read is a manifest that lies about what the organ consumes.
    """
    source = (HERE / "organ.py").read_text()
    missing = []
    for port in inputs:
        name = re.escape(port["name"])
        pat = re.compile(
            rf"""state(?:\.get\(\s*["']{name}["']|\s*\[\s*["']{name}["']\s*\])"""
        )
        if not pat.search(source):
            missing.append(port["name"])
    if missing:
        _fail("ports.json declares input(s) the organ never reads from `state`: "
              f"{missing}. Either the organ doesn't consume them or the names drifted.")
    print(f"✓ decide() reads every declared input name from `state` ({len(inputs)} checked)")


def _run_organ(organ_input: str) -> dict:
    env = os.environ.copy()
    env["ORGAN_INPUT"] = organ_input
    result = subprocess.run(
        ["python3", "organ.py"], env=env, capture_output=True, text=True, cwd=str(HERE)
    )
    if result.returncode != 0:
        _fail(f"organ.py exited {result.returncode} on ports output-check input: {result.stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        _fail(f"organ.py emitted invalid JSON on ports output-check: {exc}\n{result.stdout}")


def _validate_decide_writes_outputs(outputs) -> None:
    """Property 3b — decide writes each declared output name under `output`.

    Run the organ on every sample (plus empty state) and assert each declared
    output name is a key of the returned `output` object.
    """
    declared = [p["name"] for p in outputs]
    inputs_to_run = ['{"state":{}}']
    inputs_to_run += [s.read_text() for s in sorted((HERE / "samples").glob("*.json"))]

    for raw in inputs_to_run:
        data = _run_organ(raw)
        out = data.get("output")
        if not isinstance(out, dict):
            _fail("organ output has no 'output' object to validate declared output ports against")
        missing = [n for n in declared if n not in out]
        if missing:
            _fail(f"ports.json declares output(s) the organ never writes under `output`: "
                  f"{missing} (sample produced keys {sorted(out)})")
    print(f"✓ decide() writes every declared output name under `output` "
          f"({len(declared)} checked over {len(inputs_to_run)} sample(s))")


def main() -> None:
    ports = _load_json(HERE / "ports.json", "ports.json")
    vocab = _load_json(HERE / "types.json", "types.json (vocabulary)")

    inputs, outputs = _validate_ports_structure(ports)          # 1
    _validate_types_resolve(inputs, outputs, vocab)             # 2
    _validate_decide_reads_inputs(inputs)                       # 3a
    _validate_decide_writes_outputs(outputs)                    # 3b

    print("✓ Ports conformance OK")


if __name__ == "__main__":
    main()
