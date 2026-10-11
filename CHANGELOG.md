# Changelog

## Unreleased

- Remove the ineffective `WorkerLimits.max_copy_memory_bytes` constructor argument;
  callers must stop passing it. It never enforced a memory limit.
- Deprecate `NodeSpec.with_materialize()` and `ImageSpec.is_materializable_leaf`
  with PEP 702 annotations and `DeprecationWarning`, preserving their behavior.
  Use nested `dataclasses.replace()` calls and `spec.encode is not None` respectively.
  No removal version is scheduled. Add `typing-extensions` for Python 3.12 support.
- Reject the unimplemented `alpha` operation during canonical ordering, remove
  the private `_is_pdf()` forwarding method, and clarify the POSIX requirements
  of managed publication. Keep `io.open_cache_hit()` as the read extension point.
- Keep existing `vipscache-v1` keys, paths and contract vectors. Add source-first
  `thumbnail` with explicit EXIF handling and `autorotate` without changing old
  `scale`/`normalize` behavior.
- Import immutable originals with hardlinks/symlinks and known XXH3-128 hashes;
  optional verification and raw-only idempotent unlink in Python and Node.
- Add fluent materialization and explicit `ensure`/`aensure` to honor later
  force/pin requests even on cache hits. Reads always try existing leaves directly. Reuse pinned intermediate nodes from ordinary specs.
- Add `ensure(retain="pin")`, explicit unpin/targeted deletion, and an opt-in local
  SQLite owner separate from disposable LMDB. Coordinate publication, pins and
  TTL/byte-budget cleanup across render processes; recover interrupted writes.
  Preserve legacy files during background reconciliation; keep missing pin intents
  and reconcile missing TTL files. Unpin works even without the file. Shared read
  locks permit parallel derivatives, with at most two sorted lock attempts.
  Bound management socket/shutdown waits and query GC candidates in indexed batches.
  Skip unchanged catalog rows and commit reconciliation changes in bounded batches.
  Remove the superseded `expiry` index from existing development catalogs so GC
  uses the ordered live-artifact index without sorting timestamp ties.
  Keep retention opt-in in the container image; enable it with persistent storage
  in Compose. Reject a byte budget without management and expose the owner session timeout.
- Check Python source, tests and scripts with Astral `ty` in CI. Clarify optional
  worker capabilities, share read/open retry paths, and document complex operations.
- Unify raw staging under `.staging-<uuid>/source`, add explicit age-based cleanup
  for recognized current/legacy names, and report cleanup races as `StagingLost`.
  Preserve unknown hidden files; callers decide whether to retry imports.
- Use enum-backed error categories and pickleable Python errors; report unknown
  exceptions as `WorkerError`. Add real process-kill recovery tests.
- Add stable Python `VipsCacheError.type`, cancellation/reuse tests and configurable
  input/output pixel and PDF DPI limits.
- Build the worker with SHA-256-pinned libvips 8.18.7 and explicit HEIC/AVIF,
  JPEG XL, TIFF, PNG, WebP, JPEG and PDF support. Record codec versions in the image.
- Add format, old-cache compatibility and 24 MP thumbnail benchmark scripts.
  Sliding TTL and rendering across multiple hosts remain future work.

## 0.4.2

- Support and test Python 3.14 and 3.15.
- Worker container image builds again (`README.md` and `LICENSE` are copied
  into the build context for `uv_build`).
- CI uses the Node 24 releases of `upload-artifact`, `download-artifact` and
  `setup-uv`.

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
