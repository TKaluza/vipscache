// Runs against an actual Python worker; also used unchanged in the Node container.
import assert from 'node:assert/strict';
import { writeFile, unlink, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { ImgCacheClient } from '../dist/index.js';
const root = process.env.IMGCACHE_ROOT;
const endpoint = process.env.IMGCACHE_ENDPOINT;
const client = new ImgCacheClient({root,endpoint,timeoutMs:10000,maxConcurrency:2});
try {
  assert.equal(await client.healthcheck(), true);
  const input = join(root,'sample.ppm');
  await writeFile(input,Buffer.concat([Buffer.from('P6\n8 6\n255\n'),Buffer.alloc(8*6*3,127)]));
  const source = await client.register(input,{mime:'image/x-portable-pixmap'});
  assert.deepEqual(await client.readBytes({source}),await readFile(input));
  const spec = {source,operations:[{name:'resize',params:{width:4}}],encode:{format:'png'}};
  const meta = await client.identify(spec);
  assert.equal(meta.width,4); assert.equal(meta.height,3);
  const results = await Promise.all(Array.from({length:8},() => client.readBytes(spec)));
  for (const data of results) assert.equal(data.subarray(1,4).toString(),'PNG');
  const resolved = await client.resolve(spec);
  const offline = new ImgCacheClient({root,endpoint:'tcp://127.0.0.1:1',timeoutMs:50,requestRetries:0});
  try { assert.deepEqual(await offline.readBytes(spec),results[0]); } finally { offline.close(); }
  await unlink(join(root,resolved.relpath));
  assert.deepEqual(await client.readBytes(spec),results[0]);
  await assert.rejects(client.readBytes({...spec,source:{file_id:'0'.repeat(32)}}));
  // Minimal self-contained PDF fixture; no network download or PDF library.
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Resources << >> /Contents 4 0 R >>',
    '<< /Length 23 >>\nstream\n1 0 0 rg 0 0 72 72 re f\nendstream'
  ];
  let pdf = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((body,i) => { offsets.push(Buffer.byteLength(pdf)); pdf += `${i+1} 0 obj\n${body}\nendobj\n`; });
  const xref = Buffer.byteLength(pdf);
  pdf += 'xref\n0 5\n0000000000 65535 f \n';
  for (const offset of offsets.slice(1)) pdf += `${String(offset).padStart(10,'0')} 00000 n \n`;
  pdf += `trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  const pdfPath = join(root,'sample.pdf'); await writeFile(pdfPath,pdf);
  const pdfSource = await client.register(pdfPath,{mime:'application/pdf'});
  const pdfSpec = {source:pdfSource,operations:[{name:'render',params:{page:1,dpi:72}},{name:'crop_fraction',params:{left:0,top:0,right:1,bottom:0.5}}],encode:{format:'png'}};
  const pdfMeta = await client.identify(pdfSpec);
  assert.equal(pdfMeta.width,72); assert.equal(pdfMeta.height,36);
  assert.equal((await client.readBytes(pdfSpec)).subarray(1,4).toString(),'PNG');
  console.log('PASS: register, metadata, render, concurrent calls, offline cache hit, eviction recovery, worker error, PDF page/crop');
} finally { client.close(); }
