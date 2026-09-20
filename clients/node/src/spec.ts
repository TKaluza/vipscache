import { createXXHash128, xxhash128 } from 'hash-wasm';
import { createReadStream } from 'node:fs';
import type { ImageSpec, Json, Resolution } from './index.js';

export async function fileId(path: string): Promise<string> {
  const hash = await createXXHash128();
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}

/** Python-compatible canonical JSON for the shared JSON number domain. */
export function canonicalJson(value: Json): string {
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))
      throw new TypeError('Specs require finite numbers and safe integers');
    // Python uses scientific notation below 1e-4, with two-digit exponents.
    const text = value !== 0 && Math.abs(value) < 1e-4 ? value.toExponential() : JSON.stringify(value);
    return text.replace(/e([+-])(\d)$/, 'e$10$2');
  }
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'string') {
    if (!value.isWellFormed()) throw new TypeError('Specs require valid Unicode');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return '[' + Array.from(value, canonicalJson).join(',') + ']';
  if (typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype)
    throw new TypeError('Specs require plain JSON values');
  // Python sorts Unicode code points, whereas default JS sorting uses UTF-16.
  const compare = (a: string, b: string) => {
    const aa = Array.from(a, c => c.codePointAt(0)!); const bb = Array.from(b, c => c.codePointAt(0)!);
    for (let i = 0; i < Math.min(aa.length, bb.length); i++) if (aa[i] !== bb[i]) return aa[i]! - bb[i]!;
    return aa.length - bb.length;
  };
  return '{' + Object.keys(value).sort(compare).map(k => canonicalJson(k) + ':' + canonicalJson(value[k]!)).join(',') + '}';
}

export async function resolveSpec(input: ImageSpec): Promise<Resolution> {
  // Validate and snapshot so concurrent caller mutations cannot alter a sent spec.
  const spec = JSON.parse(canonicalJson(input as unknown as Json)) as ImageSpec;
  if (!/^[0-9a-f]{32}$/.test(spec.source.file_id)) throw new TypeError('Invalid source file_id');
  spec.source = { file_id: spec.source.file_id, mime: spec.source.mime ?? null, metadata: spec.source.metadata ?? {} };
  spec.operations = (spec.operations ?? []).map(op => {
    const policy = op.materialize ?? 'never';
    if (!['never', 'force', 'pin'].includes(policy)) throw new TypeError('Invalid materialize policy');
    return { name: op.name, params: op.params ?? {}, materialize: policy };
  });
  let parent = spec.source.file_id;
  const engine = spec.encode?.engine_version ?? 'imgcache-v1';
  for (const op of spec.operations) {
    parent = await xxhash128(canonicalJson({ engine_version: engine, operation: { name: op.name, params: op.params! }, parent_key: parent, type: 'node' }));
  }
  if (!spec.encode) {
    if (spec.operations.length) throw new TypeError('Transformed specs require an output format');
    spec.encode = null;
    return { spec, key: parent, relpath: `raw/${parent}` };
  }
  const format = spec.encode.format.toLowerCase() as NonNullable<ImageSpec['encode']>['format'];
  if (!['webp', 'png', 'jpg', 'jpeg', 'avif', 'tif', 'tiff'].includes(format)) throw new TypeError('Unsupported output format');
  spec.encode = { engine_version: engine, format, params: spec.encode.params ?? {} };
  const key = await xxhash128(canonicalJson({ engine_version: engine, format, operation: 'encode', params: spec.encode.params!, parent_key: parent, type: 'leaf' }));
  return { spec, key, relpath: `cache/leaves/${key.slice(0, 2)}/${key}.${format === 'jpeg' ? 'jpg' : format}` };
}
