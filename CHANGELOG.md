# Changelog

## 0.4.0

Breaking: the project is renamed from `imgcache` to `vipscache`. There are no
compatibility shims.

- Python package and import name `vipscache`; classes `VipsCacheClient` and
  `VipsCacheError`; commands `vipscache-zmq-worker` and
  `vipscache-zmq-healthcheck`.
- Environment variables use the `VIPSCACHE_` prefix instead of `IMGCACHE_`.
- The Node client is now `@tkaluza/vipscache`.
- Engine version is now `vipscache-v1`. All derivative keys change, so existing
  caches regenerate; source hashes (`raw/`) are unchanged.
- Remove the 0.3.0 Python compatibility aliases: `client.open` / `client.aopen`
  (use `register` / `aregister`), `client.path_for` (use `resolve`),
  `image.bytes` / `image.abytes` (use `read_bytes` / `aread_bytes`) and
  `image.ainfo` (use `aidentify`).
- Add MIT license, package metadata, and GitHub Actions CI and release workflows.

## 0.3.0

- Add the server-side `@tkaluza/imgcache` TypeScript/Node client over ZeroMQ
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
