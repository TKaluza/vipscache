# vipscache shared client contract

`vectors.json` is a checked-in set of expected results consumed by Python
(`tests/test_contract.py`) and Node (`clients/node/test/contract.test.mjs`).
Do not regenerate expected values as part of tests.

## Files and keys

- Source IDs: seed-zero XXH3-128 over the complete file bytes, 32 lowercase hex
  digits. Raw storage: `raw/<file_id>` below the shared vipscache root.
- Canonical JSON: UTF-8, no ASCII escaping or whitespace, object keys sorted by
  Unicode code point. Arrays and operation order are significant. No Unicode
  normalization. Numbers must be finite; portable integers are within
  JavaScript's safe integer range. Integral floats, including negative zero,
  hash as integers. Nonintegral numbers use Python JSON's decimal/exponent
  spelling (including two-digit small exponents).
- Node key: XXH3-128 of canonical JSON containing `engine_version`,
  `operation: {name, params}`, `parent_key`, `type: "node"`.
  First parent is the source file ID. Materialization policy is excluded.
- Leaf key: canonical JSON containing `engine_version`, `format`,
  `operation: "encode"`, `params`, `parent_key`, `type: "leaf"`.
  Format is lowercase. The encode engine version applies to the whole chain;
  default is `vipscache-v1`. No output defaults such as quality are injected.
- Leaf path: `cache/leaves/<first-two-hex>/<key>.<format>`; only `jpeg` maps
  to extension `jpg`. `tiff` remains `tiff`, matching Python.
- Source MIME and metadata do not enter the node/leaf key, matching Python.
  Callers must give consistent source interpretation for identical bytes.
- Specs preserve operation order, matching `ImageSpec.build/from_payload`.
  Python's optional `ImageSpec.canonical` operation reordering is not exposed
  by the small Node client. Supply operations in the intended order.
- `retain` is an argument of the `ensure` request (`ttl` default, or `pin`),
  alongside `spec`. It is rejected inside specs and encoder options and never
  enters keys. Materialization policies also remain outside identity. The
  `requests` vectors cover retention separately from the unchanged spec keys.
- New `thumbnail` parameters are `longest_edge` or `width`/`height`, plus
  `autorotate` (normalized to explicit `true` when absent). It must be first.
  `autorotate` as a separate operation has empty params. No defaults of existing
  operations change. Existing vectors are preserved; new vectors cover both.

### Compatibility note

Integral Python floats previously hashed differently from integers (`1.0`
versus `1`). Both clients now normalize them, including values nested inside
params. Existing entries made with integral-float params can be regenerated
under the normalized key; they expire through ordinary cache eviction.
File hashes and specs using integer or nonintegral-float params are unchanged.
NaN/Infinity are rejected instead of creating non-JSON hash inputs.

## Wire protocol

One UTF-8 JSON frame per request/reply over REQ/REP:

```json
{"method":"materialize","spec":{"source":{"file_id":"<32 hex>","mime":"application/pdf","metadata":{}},"operations":[{"name":"render","params":{"page":1},"materialize":"never"}],"encode":{"format":"png","params":{},"engine_version":"vipscache-v1"}}}
```

`identify` takes the same spec and replies `{ok:true, meta:{...}}`.
`materialize` replies `{ok:true, relpath:"cache/leaves/..."}`.
`health` replies `{ok:true}`. Errors use
`{ok:false, error:{type,message}}`; `type:"Busy"` may include a top-level
`retry_after` in **seconds**, default 0.5. Node bounds waits by a total request
deadline, retries transport timeouts on fresh sockets, and never shares a REQ
socket between concurrent exchanges. Requests can execute more than once.
Registration and cache-path calculation remain local.

Retention-aware clients additionally use:

```json
{"method":"ensure","spec":{"source":{"file_id":"<32 hex>"},"operations":[],"encode":{"format":"png"}},"retain":"pin"}
{"method":"unpin","spec":{"source":{"file_id":"<32 hex>"},"encode":{"format":"png"}}}
{"method":"delete","specs":[{"source":{"file_id":"<32 hex>"},"encode":{"format":"png"}}]}
```

`ensure` returns the same `{ok:true,relpath:...}` shape as `materialize`, only
after all requested nodes and retention have been confirmed. It is always an
explicit client call, including on local hits, and never falls back to
`materialize` on old workers. Omitted `retain` means `ttl`; existing pins are never
downgraded. `ensure` without a leaf pin works on unmanaged caches; `retain=pin`
requires explicitly configured management.

`path`/`open`/byte reads always try existing leaves directly, regardless of node
policies. Misses send `materialize` without a retention argument; force/pin nodes
are written along the chosen render path, as in 0.4.2. Only `ensure` promises that
all requested nodes exist, including ancestors bypassed by a deeper cached node.

`unpin` returns `{ok:true}`, preserves bytes and starts TTL on the pin-to-ttl
transition, including when the file is missing. `delete` returns `{ok:true}` after
removing exactly the named leaves and their records; original/node files are
unaffected. All three are idempotent; independent operations may interleave.

`evict_ttl` now performs coordinated TTL and optional byte-budget cleanup.
Stable error categories include `OriginalMissing`, `UnreadableFormat`,
`LimitExceeded`, `InvalidSpec`, `Timeout`, `ManagementUnavailable`, and `Busy`.
Python `ErrorType` and `RetainPolicy` are string enums. Unknown worker exceptions
are `WorkerError`, not guessed from generic exception classes. Error messages
exclude the category prefix; Python errors round-trip through pickle.
Local import cleanup races report `StagingLost`, with no automatic retry.
Both clients expose `.type` on `VipsCacheError`; transport/deadline errors may
originate locally. Requests operate within a trusted application boundary.

Client hits and paths retain the old contract. Never run old pin-unaware workers
or evictors concurrently with a managed cache. libvips major/minor/micro versions
are diagnostic: `vipscache-v1` is unchanged, including on micro/security updates.

The clients and worker must share the same protocol/hash implementation.
Only small control JSON travels over ZeroMQ. Sources and rendered bytes live
on a trusted shared filesystem; authorization belongs to the application.
