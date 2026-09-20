# imgcache shared client contract

`vectors.json` is a checked-in set of expected results consumed by Python
(`tests/test_contract.py`) and Node (`clients/node/test/contract.test.mjs`).
Do not regenerate expected values as part of tests.

## Files and keys

- Source IDs: seed-zero XXH3-128 over the complete file bytes, 32 lowercase hex
  digits. Raw storage: `raw/<file_id>` below the shared imgcache root.
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
  default is `imgcache-v1`. No output defaults such as quality are injected.
- Leaf path: `cache/leaves/<first-two-hex>/<key>.<format>`; only `jpeg` maps
  to extension `jpg`. `tiff` remains `tiff`, matching Python.
- Source MIME and metadata do not enter the node/leaf key, matching Python.
  Callers must give consistent source interpretation for identical bytes.
- Specs preserve operation order, matching `ImageSpec.build/from_payload`.
  Python's optional `ImageSpec.canonical` operation reordering is not exposed
  by the small Node client. Supply operations in the intended order.

### Compatibility note

Integral Python floats previously hashed differently from integers (`1.0`
versus `1`). Both clients now normalize them, including values nested inside
params. Existing entries made with integral-float params can be regenerated
under the normalized key; they expire through ordinary cache eviction.
File hashes and specs using integer or nonintegral-float params are unchanged.
NaN/Infinity are rejected instead of creating non-JSON hash inputs.

## Wire protocol (unchanged)

One UTF-8 JSON frame per request/reply over REQ/REP:

```json
{"method":"materialize","spec":{"source":{"file_id":"<32 hex>","mime":"application/pdf","metadata":{}},"operations":[{"name":"render","params":{"page":1},"materialize":"never"}],"encode":{"format":"png","params":{},"engine_version":"imgcache-v1"}}}
```

`identify` takes the same spec and replies `{ok:true, meta:{...}}`.
`materialize` replies `{ok:true, relpath:"cache/leaves/..."}`.
`health` replies `{ok:true}`. Errors use
`{ok:false, error:{type,message}}`; `type:"Busy"` may include a top-level
`retry_after` in **seconds**, default 0.5. Node bounds waits by a total request
deadline, retries transport timeouts on fresh sockets, and never shares a REQ
socket between concurrent exchanges. Requests can execute more than once.
Registration and cache-path calculation are local; no new worker endpoints.

The clients and worker must share the same protocol/hash implementation.
Only small control JSON travels over ZeroMQ. Sources and rendered bytes live
on a trusted shared filesystem; authorization belongs to the application.
