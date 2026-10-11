import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, utimes, link, lstat, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { Reply } from 'zeromq';
import { VipsCacheClient, resolveSpec, ErrorType } from '../dist/index.js';

async function fixture(t, endpoint = 'tcp://127.0.0.1:1') {
  const root = await mkdtemp(join(tmpdir(), 'vipscache-management-'));
  const client = new VipsCacheClient({root, endpoint, timeoutMs:100, requestRetries:0});
  t.after(async () => { client.close(); await rm(root, {recursive:true, force:true}); });
  return {root, client};
}
const spec = {source:{file_id:'a'.repeat(32)}, operations:[{name:'scale',params:{width:32},materialize:'force'}], encode:{format:'png'}};

test('force specifications read existing leaves without a worker or original', async t => {
  const {root, client} = await fixture(t);
  const path = join(root, (await resolveSpec(spec)).relpath);
  await mkdir(dirname(path), {recursive:true});
  await writeFile(path, 'cached bytes');
  assert.equal((await client.readBytes(spec)).toString(), 'cached bytes');
});

test('ensure sends retention separately even when the leaf exists', async t => {
  const socket = new Reply({linger:0});
  await socket.bind('tcp://127.0.0.1:*');
  t.after(() => socket.close());
  const {root, client} = await fixture(t, socket.lastEndpoint);
  const resolved = await resolveSpec(spec);
  const path = join(root, resolved.relpath);
  await mkdir(dirname(path), {recursive:true});
  await writeFile(path, 'cached bytes');
  const responder = (async () => {
    const [frame] = await socket.receive();
    assert.deepEqual(JSON.parse(frame), {method:'ensure', spec:resolved.spec, retain:'pin'});
    await socket.send(JSON.stringify({ok:true,relpath:resolved.relpath}));
  })();
  assert.equal(await client.ensure(spec, {retain:'pin'}), path);
  await responder;
  assert.equal((await client.readBytes(spec)).toString(), 'cached bytes');
});

test('old worker rejection of ensure never falls back to materialize', async t => {
  const socket = new Reply({linger:0});
  await socket.bind('tcp://127.0.0.1:*');
  t.after(() => socket.close());
  const {client} = await fixture(t, socket.lastEndpoint);
  const responder = (async () => {
    const [frame] = await socket.receive();
    assert.equal(JSON.parse(frame).method, 'ensure');
    await socket.send(JSON.stringify({ok:false,error:{type:'ValueError',message:'unsupported method: ensure'}}));
  })();
  await assert.rejects(client.ensure(spec, {retain:'pin'}), {type:'ValueError'});
  await responder;
});

test('retention is rejected inside specifications and encoders', async () => {
  await assert.rejects(resolveSpec({...spec,retain:'pin'}), /ensure/);
  await assert.rejects(resolveSpec({...spec,encode:{format:'png',params:{retain:'pin'}}}), /ensure/);
});

for (const mode of ['register', 'link']) test(`${mode}: lost staging has a stable error and is not retried`, async t => {
  const {root, client} = await fixture(t);
  const source = join(root, 'source');
  await writeFile(source, 'original');
  const original = client.withStaging.bind(client);
  let attempts = 0;
  client.withStaging = write => original(async staged => {
    attempts++;
    await rm(dirname(staged), {recursive:true, force:true});
    return write(staged);
  });
  await assert.rejects(client[mode](source, {file_id:'a'.repeat(32)}), {type:ErrorType.StagingLost});
  assert.equal(attempts, 1);
  assert.equal((await readFile(source)).toString(), 'original');
  assert.deepEqual(await readdir(join(root, 'raw')), []);
});

test('cleanup uses directory age, recognizes legacy names and preserves foreign entries', async t => {
  const {root, client} = await fixture(t);
  const raw = join(root, 'raw');
  await mkdir(raw);
  const source = join(root, 'source');
  await writeFile(source, 'old source');
  await utimes(source, 100, 100);
  const fresh = join(raw, `.staging-${randomUUID()}`);
  await mkdir(fresh); await link(source, join(fresh, 'source'));
  const old = join(raw, `.staging-${randomUUID()}`);
  await mkdir(old); await link(source, join(old, 'source')); await utimes(old, 100, 100);
  const legacyDirectory = join(raw, `.${'b'.repeat(32)}.abcdefgh`);
  await mkdir(legacyDirectory); await link(source, join(legacyDirectory, 'source')); await utimes(legacyDirectory, 100, 100);
  const legacyLink = join(raw, `.${randomUUID()}`);
  await link(source, legacyLink);
  const foreign = join(raw, '.foreign'); await writeFile(foreign, 'keep'); await utimes(foreign, 100, 100);
  const canonical = join(raw, 'a'.repeat(32)); await link(source, canonical);
  assert.equal(await client.cleanupStaging({olderThan:3600}), 2);
  assert.ok((await lstat(fresh)).isDirectory());
  assert.ok((await lstat(legacyLink)).isFile());
  assert.equal((await readFile(foreign)).toString(), 'keep');
  assert.equal((await readFile(canonical)).toString(), 'old source');
});
