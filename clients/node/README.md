# @tkaluza/vipscache

Small server-side Node/TypeScript client using the existing Python ZeroMQ
worker and a shared vipscache root. Includes TypeScript declarations. No HTTP
server, rendering engine, or Python-style fluent image builder.

```sh
cd clients/node
npm ci
npm test
npm pack
# Install the resulting tarball (also attached to each GitHub release):
# npm install /path/to/tkaluza-vipscache-0.4.2.tgz
```

```ts
import { VipsCacheClient } from '@tkaluza/vipscache';

// One instance per server process, closed during application shutdown.
const images = new VipsCacheClient({
  root: '/storage/vipscache',
  endpoint: 'tcp://vipscache:5555',
  timeoutMs: 30_000,
  maxConcurrency: 4,
  maxQueue: 64,
  requestRetries: 2
});

// The application first authorizes the source within the current job.
const source = await images.register('/storage/staged/authorized-source.pdf', {
  mime: 'application/pdf'
});
// Persist/reuse source in server-owned job state; don't copy it on every request.
const spec = {
  source,
  operations: [
    { name: 'render', params: { page: 1, dpi: 100 } },
    { name: 'resize', params: { width: 800 } }
  ],
  encode: { format: 'webp' as const, params: { quality: 82 } }
};
const metadata = await images.identify(spec);
const bytes = await images.readBytes(spec);
// Serve bytes through the authorized Web response with image/webp content type.
```

`resolve(spec)` computes `{key, relpath, spec}` locally. `open(spec)` returns an
open Node `FileHandle`; the caller must close it. `readBytes(spec)` reads and closes
that handle. Existing cached files need no worker round trip. Missing encoded
files are rendered, with one retry for eviction between rendering and opening.
Original files must already exist. `identify` asks the worker for metadata.

Registration copies into `raw/.staging-<uuid>/source`, hashes the snapshot in
chunks with WASM XXH3-128, and atomically renames it. It does not buffer the
whole source. Concurrent registrations of identical bytes converge on the
same ID. Application code should limit simultaneous source ingests separately;
`maxConcurrency` and `maxQueue` bound ZeroMQ requests. The deadline includes
request queuing, send/receive, transport retries and Busy waits. File I/O is not
subject to that deadline. `close()` rejects pending requests and closes sockets.

Use only plain JSON values in specs. See the [shared contract](../../contracts/README.md)
for number normalization, cache compatibility, and fixed test vectors.
`VipsCacheError.type` distinguishes `Timeout`, `QueueFull`, `Closed`, protocol
errors and worker error types. Filesystem errors retain Node's error codes.

## API names

`register(path, {file_id, verify})` can copy a source using a known XXH3-128 hash.
`link(path, {file_id, mode: 'hardlink', verify})` avoids copying and, with a known
hash, avoids reading the source bytes. Symlinks accept `mode: 'symlink'` and an
absolute worker-visible `link_target`. `unlink(file_id)` removes only the raw
entry and is idempotent. The caller owns original reference counting.

```ts
const preview = {
  source,
  operations: [{name: 'thumbnail', params: {longest_edge: 320}, materialize: 'force'}],
  encode: {format: 'webp', params: {quality: 80}},
} satisfies ImageSpec;
await images.ensure(preview, {retain: 'pin'}); // explicit pin/force confirmation
const bytes = await images.readBytes(preview); // direct hit; no retention request
await images.unpin(preview);     // starts TTL, leaves bytes unchanged
await images.delete([preview]);  // deletes exactly these leaves, including pins
```

Import `ImageSpec` as a TypeScript type. Retention belongs only to
`ensure(spec, {retain: 'pin'})`, which returns the local path after confirmation.
`ensure(spec)` confirms intermediate requirements without requesting a leaf pin.
An old worker rejecting `ensure` is never bypassed with `materialize`.
`open` and `readBytes` always read hits directly, including force/pin specifications;
misses use ordinary `materialize`. Existing pins are never downgraded by reads or
ordinary ensures. Durable leaf pins require opt-in worker management. `thumbnail` must be first,
preserves aspect ratio, and includes the default `autorotate: true` in its key.
Separate `autorotate` is an operation with empty params. See the shared
[retention and deployment rules](../../README.md#worker).

`cleanupStaging({olderThan: 86400})` explicitly removes known old import staging
and returns the number of removed entries. It recognizes the shared UUID-directory
schema and legacy client names, preserves foreign hidden entries, and uses the
staging age instead of the original's mtime. It never follows symlink targets.
Cleanup may overlap imports: `StagingLost` means the caller may retry the import;
no automatic retry is performed.

All data methods return Promises, including `ensure`, `unpin`, `delete`, `link`,
`unlink` and `cleanupStaging`. `close()` is synchronous. `register(path)` returns a source;
`open(spec)` returns a file handle, rendering on a miss. `readBytes(spec)`
returns a Buffer and closes the handle automatically.

Python uses `register` / `aregister` and `read_bytes` / `aread_bytes` with its
existing `CachedImage` builder; see the
[method mapping](../../README.md#client-method-names).

## Deployment notes

Use this package in server code only. Mount the same vipscache directory in the
application and the worker (the mount paths may differ), and keep ZeroMQ on an
internal container network. Resolve sources from your own authorized
identifiers; never take source paths or arbitrary specs directly from a
browser. Raw-source lifetime/cleanup, deeper source validation and worker
resource limits are the application's responsibility.

## Validation

From the repository root:

```sh
npm ci --prefix clients/node
npm test --prefix clients/node
uv run pytest
scripts/test_node_container.sh
```

The container script builds the Python worker and a Node 24 Debian Bookworm
client, installs dependencies inside the client image, runs contract/transport
tests, and performs real registration, metadata, rendering, concurrent requests,
offline cache reads and eviction recovery across two containers. It uses a
disposable volume with **different mount paths** in the containers. Set
`CONTAINER_ENGINE=podman` if needed. Re-run against the eventual Web base image
and architecture before deployment. ZeroMQ.js is native; hash-wasm needs no
additional native addon. See [ZeroMQ.js](https://zeromq.github.io/zeromq.js/)
and its [REQ socket contract](https://zeromq.github.io/zeromq.js/classes/Request.html).
