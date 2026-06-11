# organ-cache

A pure, stdlib-only organism **organ** that decides cache policy: whether caching
is enabled, what canonical key to use, what TTL to apply, and which action the
spine should take around a cache operation (use a hit, compute on a miss, store a
value, skip storing a `None`, or bypass caching entirely).

## What is an Organ?

An organ is a small, self-contained decision-maker that conforms to the
orchestrator protocol:

- **Input**: reads `{state, context}` JSON on stdin or via `$ORGAN_INPUT`
- **Output**: writes `{output, rationale, self_metric}` JSON to stdout
- **Pure**: no database calls, no network I/O, no side effects — every input
  arrives as JSON
- **Stdlib-only**: depends only on the Python standard library (Python 3.12+)
- **Fail-safe**: never crashes on bad input; on malformed/empty `state` it falls
  back to a cache **bypass** — never a confident false "hit"

## What was extracted

The source, discovery-engine `app/services/cache.py`, is a Redis caching layer
that mixes two concerns:

1. **side-effecting Redis I/O** — connect / `GET` / `SETEX` / `DELETE`, and
2. **pure caching policy** — the decisions taken *around* that I/O.

This organ extracts only (2). It never touches Redis. Faithful mapping:

| cache.py                                   | organ decision                         |
|--------------------------------------------|----------------------------------------|
| `is_configured()` (`bool(REDIS_URL)`)      | `caching_enabled`                      |
| `cached` key building                      | `cache_key` derivation                 |
| `get()` / `get_json()` returning a value   | `is_hit = cached_value is not None`    |
| `if result is not None: set_json(...)`     | `should_store = value_present`         |
| `set(..., ttl=300)` default + clamping     | `ttl` resolution + clamp               |

### Deliberate improvement over the source

`cache.py` derives keys with the builtin `hash(str(args)+str(kwargs))`, which is
**salted per process** (`PYTHONHASHSEED`) and therefore non-deterministic across
restarts — a latent cache-miss-storm bug. This organ derives keys with a stable
`hashlib.sha256` digest over a canonicalised (sorted-key) JSON form, so the same
`key_parts` always map to the same key, in any process, in any key order.

## Contract

### Input

```json
{
  "state": {
    "operation": "get | lookup | set | store",
    "key_prefix": "blueprint",
    "key_parts": { "tenant_id": 5, "signature": "a1b2c3d4" },
    "key": "explicit:key (optional; overrides derivation)",
    "ttl": 300,
    "value_present": true,
    "cached_value": { "...": "what the store returned, or null" }
  },
  "context": {
    "configured": true,
    "redis_url": "redis://...",
    "default_ttl": 300,
    "min_ttl": 1,
    "max_ttl": 86400
  }
}
```

`context` is optional — the organ works with it absent (caching then reads as
disabled, the conservative default).

### Output

```json
{
  "output": {
    "caching_enabled": true,
    "cache_key": "blueprint:ab12cd34ef560a7b",
    "ttl": 300,
    "action": "hit | miss | store | skip_store | bypass",
    "is_hit": false,
    "should_store": false
  },
  "rationale": "human-readable why, derivable from state alone",
  "self_metric": { "confidence": 0.95, "...": "organ counters" }
}
```

### Actions

| action       | meaning                                                      |
|--------------|-------------------------------------------------------------|
| `hit`        | read found a value — use it, skip recompute                 |
| `miss`       | read found nothing — compute, then store if non-None        |
| `store`      | write a non-None value with the resolved TTL                |
| `skip_store` | write of a `None`/absent value — do not cache it            |
| `bypass`     | caching disabled, no derivable key, or unknown op — compute |

## Usage

```bash
# From stdin
cat samples/blueprint_cache_hit.json | python3 organ.py

# From a file via env var
ORGAN_INPUT=samples/store_non_none_result.json python3 organ.py
```

As a library:

```python
from organ import decide
result = decide(
    {"operation": "get", "key_prefix": "bp", "key_parts": {"t": 1}, "cached_value": {"x": 1}},
    {"configured": True},
)
# result["output"]["action"] == "hit"
```

## Samples

- `blueprint_cache_hit.json` — a configured backend, read with a cached value → `hit`
- `store_non_none_result.json` — a write of a present value → `store` (TTL clamped to `max_ttl`)
- `backend_unconfigured_bypass.json` — no backend configured → `bypass`
- `explicit_key_store.json` — a write with an explicit `key` override → `store`

## Connection ports (the Lego stud)

`ports.json` declares this organ's typed inputs/outputs against the shared
connection-type vocabulary, per
[`CONNECTORS.md`](https://github.com/Data-Flow-Advisory/orchestrator/blob/feat/drift-gate/CONNECTORS.md).
Each `name` is the literal key `decide()` reads under `state` (inputs) or writes
under `output` (outputs); each `type` is a vocabulary entry — the stud size that
decides which ports may wire together. (`context` keys are cache-substrate
configuration, not wired ports, so they are not listed.)

- `vocab/types.json` — a pinned snapshot of the orchestrator vocabulary (curated
  domain types; the `types` block is kept byte-faithful to upstream).
- `vocab/proposed_types.json` — the types this organ needs that the vocabulary
  doesn't yet carry, **proposed for upstream review**: four foundational scalar
  studs `String` / `Integer` / `Boolean` / `Json`, plus the cache-semantic
  `CacheAction` enum. organ-cache is infrastructure — its ports are scalars, not
  domain payloads, so no existing curated type fits.

## Tests & conformance

```bash
python -m pip install pytest
python3 check_contract.py   # contract shape on every sample + empty state
python3 check_ports.py      # ports.json parses, types ∈ vocabulary, names read/written
python -m pytest -q         # full unit suite (incl. test_ports.py)
```

The `conformance` GitHub Action runs all three on every push.
