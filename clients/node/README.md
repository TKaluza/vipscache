# @docmist/imgcache

Small server-side Node/TypeScript client using the existing Python ZeroMQ
worker and a shared imgcache root. Includes TypeScript declarations. No HTTP
server, rendering engine, or Python-style fluent image builder.

```sh
cd clients/node
npm ci
npm test
npm pack
# In docmist-web, install the resulting tarball:
# npm install /path/to/docmist-imgcache-0.3.0.tgz
```

```ts
import { ImgCacheClient } from '@docmist/imgcache';

// One instance per server process, closed during application shutdown.
const images = new ImgCacheClient({
  root: '/storage/imgcache',
  endpoint: 'tcp://imgcache:5555',
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

Registration copies into a temporary file in `raw/`, hashes the snapshot in
chunks with WASM XXH3-128, and atomically renames it. It does not buffer the
whole source. Concurrent registrations of identical bytes converge on the
same ID. Application code should limit simultaneous source ingests separately;
`maxConcurrency` and `maxQueue` bound ZeroMQ requests. The deadline includes
request queuing, send/receive, transport retries and Busy waits. File I/O is not
subject to that deadline. `close()` rejects pending requests and closes sockets.

Use only plain JSON values in specs. See the [shared contract](../../contracts/README.md)
for number normalization, cache compatibility, and fixed test vectors.
`ImgCacheError.type` distinguishes `Timeout`, `QueueFull`, `Closed`, protocol
errors and worker error types. Filesystem errors retain Node's error codes.

## API names in 0.3.0

All data methods return Promises: `register`, `identify`, `resolve`, `open`
and `readBytes`. `close()` is synchronous. `register(path)` returns a source;
`open(spec)` returns a file handle, rendering on a miss. `readBytes(spec)`
returns a Buffer and closes the handle automatically. There is no `bytes`
alias in this first Node release.

Python uses `register` / `aregister` and `read_bytes` / `aread_bytes` with its
existing `CachedImage` builder. Legacy Python names remain compatible; see
the [method mapping and migration notes](../../README.md#client-method-names-030).

## docmist-web integration

The sibling repo uses npm, TypeScript and server code under `src/lib/server`.
Keep this package in that server boundary. Its current Compose file provides
PostgreSQL and a storage volume, but no Web runtime image; adapter-auto is not
an explicit Node production deployment. This change supplies the client, not
the Web deployment or authorized preview route.

For a Node deployment, mount the same imgcache directory in Web and worker
(the mount paths may differ), and keep ZeroMQ on the internal container network.
Resolve the existing opaque preview `source_id` within the authorized job;
never take source paths or arbitrary specs directly from a browser. Map the
preview's one-based page and requested size to explicit operations as above.
Do not introduce a separate preview-file tree. The cross-repository follow-up
remains in [the synchronization checklist](../../../docmist-ai/todo_web.md).
Raw-source lifetime/cleanup, deeper source validation and worker resource
limits remain separate work.

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
