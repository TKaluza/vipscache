# imgcache wire schema v1

This document freezes the JSON payload emitted by `ImageSpec.to_payload()` for web-delivery clients. The payload is the wire contract; derivative keys are still computed by Python from canonical key data, not from this JSON object directly.

## Top-level image spec

```json
{
  "source": { "file_id": "...", "metadata": {}, "mime": "image/jpeg" },
  "operations": [],
  "encode": null
}
```

Fields:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `source` | object | yes | `SourceSpec.to_payload()`; see below. |
| `operations` | array of operation objects | yes | Ordered pipeline. Order is part of derivative identity. Empty for original/re-encode. |
| `encode` | object or `null` | yes | `null` for an original or non-materializable intermediate; object for a cache leaf. |

## Source payload

`SourceSpec.to_payload()` emits exactly:

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `file_id` | string | yes | none | Content-addressed original id; this is the first parent key. |
| `metadata` | object | yes | `{}` | JSON metadata copied from ingest, such as `filename`, dimensions, or page count. |
| `mime` | string or `null` | yes | `null` | MIME type when known. PDF detection also accepts `metadata.filename` ending in `.pdf`. |

## Operation payload

Every operation in `operations` emits exactly:

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `name` | string | yes | none | Operation name. |
| `params` | object | yes | `{}` | Operation-specific JSON parameters. Python stores keys sorted in the dataclass. |
| `materialize` | string | yes | `"never"` | One of `"never"`, `"force"`, `"pin"`. Materialization policy is transported but excluded from derivative key data. |

Supported operation names and parameters:

| Builder method | Wire `name` | Parameters |
| --- | --- | --- |
| `page(page=1, dpi=None, **params)` | `render` | `page` integer; optional `dpi` integer/float; any extra JSON params passed through. Use for PDF/page sources. |
| `normalize(colorspace="srgb")` | `normalize` | `colorspace` string. |
| direct `Operation("scale", ...)` / `scale(...)` | `scale` | Any of `longest_edge`, `width`, `height` as integers; `scale_factor` as float. Omitted values are absent. |
| direct `Operation("resize", ...)` / `resize(...)` | `resize` | Any of `width`, `height`, `longest_edge` as integers; `scale_factor` as float. Omitted values are absent. |
| `crop(x, y, w, h)` | `crop` | `x`, `y`, `w`, `h` integers. |
| `crop_fraction(left=0.0, top=0.0, right=1.0, bottom=1.0)` | `crop_fraction` | `left`, `top`, `right`, `bottom` floats. Builder defaults are explicit in the payload when the method is used. |
| `rotate(degrees)` | `rotate` | `degrees` integer or float. |
| `fast_rotate(degrees)` | `fast_rotate` | `degrees` integer, normally one of `0`, `90`, `180`, `270`. |
| `flip()` | `flip` | No parameters: `{}`. |
| `flop()` | `flop` | No parameters: `{}`. |

`colorspace` and `alpha` operation names are accepted by canonicalization as stage-1 operations, but the web-delivery v1 public builder operation documented for the frozen contract is `normalize`.

## Encode payload

When present, `encode` emits exactly:

| Field | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `engine_version` | string | yes | `"imgcache-v1"` | Bump when an intentional key/wire behavior change occurs. |
| `format` | string | yes | none | Lowercase output format. `jpeg` is accepted internally but `jpg` is the documented web format. |
| `params` | object | yes | `{}` | Encode-specific JSON parameters, excluding `format`. |

Documented web encodes:

| Builder method | Wire `format` | Parameters |
| --- | --- | --- |
| `webp(quality=82, **params)` | `webp` | `quality` integer default `82`; extra JSON params pass through. |
| `png(**params)` | `png` | No default params; extra JSON params pass through. |
| `jpg(quality=85, **params)` | `jpg` | `quality` integer default `85`; extra JSON params pass through. |
| `avif(quality=60, **params)` | `avif` | `quality` integer default `60`; extra JSON params pass through. |
| `tif(**params)` | `tif` | No default params; extra JSON params pass through. |

## Leaf relative path

For an encoded spec, the expected cache leaf path is derived from the current Python key calculation:

```text
leaves/<first-two-hex-chars-of-leaf-key>/<leaf-key>.<extension>
```

`extension` is the encode format, except `jpeg` maps to `jpg`. The golden fixture lines store this as `relpath` without a leading `cache/` prefix.

## Golden fixtures

`tests/golden/specs.jsonl` contains one JSON object per line:

```json
{ "spec": { ...ImageSpec.to_payload()... }, "relpath": "leaves/..../file.ext" }
```

`tests/golden/hashes.json` contains shared `xxh3-128` hex vectors for the ingest hasher. TypeScript clients must match these vectors before writing `raw/` objects.
