#!/usr/bin/env python3
"""Port-manifest conformance — the connection-standard (Lego stud) check.

Asserts, per CONNECTORS.md (Data-Flow-Advisory/orchestrator):

  1. ports.json parses as JSON with the
       {inputs: [{name, type, required?}], outputs: [{name, type}]}
     shape.
  2. Every declared port `type` EXISTS in the shared type vocabulary —
     vocab/types.json (a pinned snapshot of the orchestrator vocabulary)
     UNION vocab/proposed_types.json (the new types THIS PR proposes upstream).
  3. decide() actually READS each declared input name (off `state`) and
     WRITES each declared output name (under `output`):
       - a source-level read check against organ.py, and
       - a sample-presence check (every input name is exercised by >=1
         sample's state; every output name is produced when organ.py runs
         on the samples).

An organ with no valid ports manifest is "connectable-by-luck only"
(CONNECTORS.md) — so this gate goes RED if the studs don't line up.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ORGAN = HERE / "organ.py"


def _fail(msg: str) -> None:
    print(f"::error::{msg}")
    sys.exit(1)


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - report any parse/read failure clearly
        _fail(f"{path}: not valid JSON ({exc})")


def _load_vocabulary():
    """Return (vocab_type_names, proposed_type_names)."""
    official = HERE / "vocab" / "types.json"
    if not official.exists():
        _fail("vocab/types.json (pinned vocabulary snapshot) is missing")
    vocab = dict((_load_json(official).get("types") or {}))

    proposed_path = HERE / "vocab" / "proposed_types.json"
    proposed = {}
    if proposed_path.exists():
        proposed = dict((_load_json(proposed_path).get("types") or {}))
        vocab.update(proposed)
    return set(vocab), set(proposed)


def main() -> None:
    ports_path = HERE / "ports.json"
    if not ports_path.exists():
        _fail("no ports.json — organ is connectable-by-luck only (CONNECTORS.md)")

    ports = _load_json(ports_path)
    if not isinstance(ports, dict):
        _fail("ports.json must be a JSON object")
    for side in ("inputs", "outputs"):
        if not isinstance(ports.get(side), list):
            _fail(f"ports.json missing list '{side}'")

    vocab, proposed = _load_vocabulary()

    # 1 + 2: structure and type-existence.
    declared_inputs, declared_outputs = [], []
    for p in ports["inputs"]:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"input port malformed (need name + type): {p!r}")
        if "required" in p and not isinstance(p["required"], bool):
            _fail(f"input port {p['name']!r}: 'required' must be a bool")
        declared_inputs.append(p["name"])
        if p["type"] not in vocab:
            _fail(f"input port {p['name']!r}: type {p['type']!r} not in the vocabulary")
    for p in ports["outputs"]:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"output port malformed (need name + type): {p!r}")
        declared_outputs.append(p["name"])
        if p["type"] not in vocab:
            _fail(f"output port {p['name']!r}: type {p['type']!r} not in the vocabulary")

    if not declared_inputs:
        _fail("ports.json declares no inputs")
    if not declared_outputs:
        _fail("ports.json declares no outputs")

    # 3a: source-level — decide() reads each declared input name off `state`.
    src = ORGAN.read_text()
    for name in declared_inputs:
        if f'state.get("{name}"' not in src and f'state["{name}"' not in src:
            _fail(f"input port {name!r} declared but organ.py never reads state[{name!r}]")

    # 3b: sample-presence — each declared input name is exercised by a sample.
    samples = sorted((HERE / "samples").glob("*.json"))
    if not samples:
        _fail("no samples/*.json to validate ports against")
    sample_states = [(_load_json(s).get("state") or {}) for s in samples]
    for name in declared_inputs:
        if not any(isinstance(st, dict) and name in st for st in sample_states):
            _fail(f"input port {name!r} not exercised by any sample state")

    # 3c: outputs — run organ.py on each sample; every declared output name
    # must actually be produced under `output`.
    produced = set()
    for s in samples:
        env = os.environ.copy()
        env["ORGAN_INPUT"] = str(s)
        proc = subprocess.run(
            [sys.executable, str(ORGAN)], env=env, capture_output=True, text=True
        )
        if proc.returncode != 0:
            _fail(f"organ.py failed on {s.name}: {proc.stderr}")
        out = (_load_or_none(proc.stdout) or {}).get("output", {})
        if isinstance(out, dict):
            produced.update(out.keys())
    for name in declared_outputs:
        if name not in produced:
            _fail(f"output port {name!r} declared but never written under output by any sample")

    print(
        f"✓ ports.json: {len(declared_inputs)} inputs, "
        f"{len(declared_outputs)} outputs, all typed against the vocabulary"
    )
    if proposed:
        in_use = sorted(
            {p["type"] for p in ports["inputs"] + ports["outputs"]} & proposed
        )
        if in_use:
            print(f"  proposed-upstream types in use (pending review): {in_use}")
    print("✓ every declared input name is read by decide() and exercised by a sample")
    print("✓ every declared output name is produced by decide()")


def _load_or_none(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    main()
