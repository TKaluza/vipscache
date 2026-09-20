# Changelog

## 0.3.0

- Add the server-side `@docmist/imgcache` TypeScript/Node client over ZeroMQ
  and shared storage, with bounded requests, deadlines and Busy retries.
- Share fixed source-hash and cache-key contract vectors between Python and
  TypeScript; verify real image/PDF rendering in separate worker/client containers.
- Use explicit client names: Python `register` / `aregister`, `resolve` /
  `aresolve`, `CachedImage.aidentify`, `read_bytes` / `aread_bytes` and `aopen`;
  TypeScript `readBytes`. Existing Python names remain compatible aliases.
- Normalize integral floats in canonical spec JSON (`1.0` becomes `1`,
  negative zero becomes `0`) and reject nonfinite numbers. Existing derivatives
  whose keys included integral floats may be regenerated; source hashes are
  unchanged. See [the protocol contract](contracts/README.md).
- Align Python and Node package versions at 0.3.0. The ZeroMQ protocol and
  render engine version remain unchanged.
