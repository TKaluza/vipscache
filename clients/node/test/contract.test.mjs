import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { xxhash128 } from 'hash-wasm';
import { canonicalJson, fileId, resolveSpec } from '../dist/index.js';
const vectors = JSON.parse(await readFile(new URL('../../../contracts/vectors.json', import.meta.url)));
for (const item of vectors.files) test(`file hash: ${item.name}`, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'vipscache-hash-'));
  try {
    const path = join(dir, 'file');
    await writeFile(path, Buffer.concat(Array(item.repeat).fill(Buffer.from(item.hex, 'hex'))));
    assert.equal(await fileId(path), item.hash);
  } finally { await rm(dir, {recursive:true,force:true}); }
});
for (const [i, item] of vectors.canonical.entries()) test(`canonical JSON ${i}`, async () => {
  assert.equal(canonicalJson(item.input), item.json);
  assert.equal(await xxhash128(canonicalJson(item.input)), item.hash);
});
for (const item of vectors.specs) test(`spec: ${item.name}`, async () => {
  assert.deepEqual(await resolveSpec(item.input), {spec:JSON.parse(JSON.stringify(item.payload)),key:item.key,relpath:item.relpath});
});
test('reject non-JSON and lossy numbers', () => {
  for (const value of [NaN, Infinity, 2**53, undefined, {a:undefined}, '\ud800']) assert.throws(() => canonicalJson(value));
});
