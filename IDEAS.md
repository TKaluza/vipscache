# Ideas

## Worker-owned metadata index

> **Status (2026-06): `identify` is implemented, compute-on-demand.** The worker
> exposes an `identify` ZMQ request served by `RenderWorker.measure(spec)`, and
> `CachedImage` surfaces it as Pillow-like `.size/.width/.height/.mode/.info/.n_pages`.
> v1 reads metadata live over the libvips render path on every request (no store).
> The LMDB memoization below is the remaining v2 step; it should only add caching,
> not change the `identify` contract or move geometry off libvips.

Metadata should not live in the derivative cache and should not be written into
`SourceSpec.metadata` automatically. `SourceSpec.metadata` is currently part of
the source payload and can affect identity semantics. Automatically extracted
image facts such as width, height, format, bands, bit depth, and alpha are
better treated as worker-owned state.

The worker can expose an `identify` request over ZeroMQ:

```json
{
  "method": "identify",
  "source": {
    "file_id": "ac045e19e0574d13",
    "mime": "image/jpeg"
  }
}
```

On request, the worker should:

1. Look up metadata by `file_id`.
2. If present and extractor version matches, return it.
3. If missing or stale, read the image header with libvips/pyvips.
4. Store the metadata in the worker index.
5. Return metadata as JSON over ZMQ.

The response can stay ordinary JSON. There is no strong reason to invent a
custom binary JSON format for these small payloads.

## LMDB as worker key database

LMDB is a good fit for this worker-owned state:

- stable and mature
- very fast key-value reads
- single writer, many readers
- memory-mapped and low overhead
- usable from Python now and Rust later
- natural fit for `file_id -> metadata` and graph/index records

The LMDB environment should live outside the cache tree, for example:

```text
<root>/
  raw/
  cache/
  worker-state/
    meta.lmdb/
      data.mdb
      lock.mdb
```

This makes it clear that the database is worker state, not disposable cache
content. It should not be touched by normal TTL cache eviction.

Suggested initial Python LMDB settings:

```python
env = lmdb.open(
    str(state_dir / "meta.lmdb"),
    map_size=1024 ** 3,
    subdir=True,
    max_dbs=2,
    lock=True,
    sync=True,
    metasync=True,
    writemap=False,
)
```

`map_size` should be configurable later, for example via
`IMGCACHE_META_MAP_SIZE`.

## Key layout

Use the existing `file_id` / xxh3 content hash as the stable source identity.
It is the right primary key because extracted metadata is a function of the
original file content.

Prefer namespaced keys instead of raw hash keys:

```text
source:<file_id>                  -> source metadata
source-child:<file_id>:<child>     -> relationship from source to child
child-source:<child>               -> reverse lookup to source
op:<op_key>                        -> canonical operation payload
edge:<parent_key>:<op_key>         -> child node key
node:<node_key>                    -> node state / metadata
leaf:<leaf_key>                    -> leaf state / metadata
children:<parent_key>:<child_key>  -> parent-to-child DAG index
```

Values can start as compact JSON bytes:

```python
value = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
```

MessagePack can be considered later if size or speed matters, but JSON is much
easier to inspect while the design is still moving.

Do not include these in the key:

- MIME
- filename
- width / height
- extractor version

Those belong in the value. The extractor version should be checked when reading
and used to decide whether the stored metadata must be refreshed.

## DAG index

LMDB can also describe the operation graph and dynamic branching. The important
boundary is:

- `ImageSpec` remains the source of truth for reproducible rendering.
- LMDB is a search, planning, metadata, and relationship index.
- The filesystem remains the storage location for raw files, `.v` nodes, and
  encoded leaves.

This preserves deterministic cache identity while allowing the worker to ask:

- Which children does this source or node have?
- Does `parent_key + operation` already have a known child?
- Is a child materialized?
- Which leaves exist below this branch?
- Is there a better existing starting point for this render?

The index can be written opportunistically during materialization first. The
renderer should continue to work from `ImageSpec` even if the LMDB index is
missing, stale, or rebuilt.

## Rust direction

Rust could be a good future direction for the worker or index core, especially
for:

- hashing and ingest helpers
- LMDB index handling
- DAG traversal and planning
- worker scheduling
- eventually a long-running worker daemon

libvips already performs the heavy image work in C, so Rust is not expected to
make image transforms much faster by itself. The likely benefit is stronger
state management, concurrency control, and a robust daemon.

If Rust is introduced:

- Keep LMDB if Python and Rust should share the same database directly.
- Use a Rust LMDB wrapper such as `heed` for a more ergonomic Rust API.
- Consider `redb` only if the worker becomes Rust-only and avoiding the native
  LMDB dependency matters more than LMDB's maturity and speed.

Current preference: LMDB is the conservative and probably best fit for this
project's worker-owned key database.
h
# Idee zusatz
Verwendung von msgspec entweder json oder msgpack with msgpec-struct für alle daten über zmq 
verwendung von lmdb als key storage
und https://developmentseed.org/obstore/latest/ als grundlegend s3 ersatz ggfs auch anderen ersatz
volume mount nach wie vor spanned, sollte aber auch andere varianten geben (evtl s3 oder über sshs oder ftp oder what ever)
keine metadaten in einezlenn files, cachhe hat nur iblddaten. lmdb kriegt auch dann calls und sonstiges mit gennant von clienten. 