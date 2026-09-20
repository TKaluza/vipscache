import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Reply } from 'zeromq';
import { setTimeout as sleep } from 'node:timers/promises';
import { ImgCacheClient } from '../dist/index.js';
async function server(t) {
  const socket = new Reply({linger:0});
  await socket.bind('tcp://127.0.0.1:*');
  t.after(() => socket.close());
  return socket;
}
function client(t, endpoint, options = {}) {
  const c = new ImgCacheClient({root:'/tmp',endpoint,timeoutMs:1000,...options});
  t.after(() => c.close());
  return c;
}
test('Busy replies retry then succeed on a valid REQ exchange', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint);
  const responder = (async () => {
    for (let i=0;i<3;i++) { await s.receive(); await s.send(JSON.stringify(i<2 ? {ok:false,error:{type:'Busy'},retry_after:0.01} : {ok:true})); }
  })();
  assert.equal(await c.healthcheck(),true); await responder;
});
test('worker errors retain type and are not retried', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint);
  const responder = (async () => { await s.receive(); await s.send(JSON.stringify({ok:false,error:{type:'ValueError',message:'bad spec'}})); })();
  await assert.rejects(c.healthcheck(), {type:'ValueError',message:'bad spec'}); await responder;
});
test('concurrency and queue are bounded; queued calls expire', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint,{maxConcurrency:1,maxQueue:1,timeoutMs:100,requestRetries:0});
  const one = assert.rejects(c.healthcheck(),{type:'Timeout'});
  const two = assert.rejects(c.healthcheck(),{type:'Timeout'});
  await assert.rejects(c.healthcheck(),{type:'QueueFull'});
  await Promise.all([one,two]);
});
test('late replies cannot poison subsequent requests', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint,{timeoutMs:150,requestRetries:0});
  const responder = (async () => {
    await s.receive(); await sleep(200); await s.send(JSON.stringify({ok:false,error:{type:'Late',message:'old request'}}));
    await s.receive(); await s.send(JSON.stringify({ok:true}));
  })();
  await assert.rejects(c.healthcheck(),{type:'Timeout'});
  assert.equal(await c.healthcheck(),true); await responder;
});
test('close rejects active and queued calls', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint,{maxConcurrency:1});
  const one = assert.rejects(c.healthcheck(),{type:'Closed'});
  const two = assert.rejects(c.healthcheck(),{type:'Closed'});
  await sleep(10); c.close(); await Promise.all([one,two]);
  await assert.rejects(c.healthcheck(),{type:'Closed'});
});
test('transport retries use a fresh socket within total deadline', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint,{timeoutMs:900,requestRetries:2});
  const responder = (async () => {
    await s.receive(); await sleep(350); await s.send(JSON.stringify({ok:false,error:{type:'Late'}}));
    await s.receive(); await s.send(JSON.stringify({ok:true}));
  })();
  assert.equal(await c.healthcheck(),true); await responder;
});
test('persistent Busy is bounded by total deadline', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint,{timeoutMs:100});
  const responder = (async () => { await s.receive(); await s.send(JSON.stringify({ok:false,error:{type:'Busy'},retry_after:1})); })();
  await assert.rejects(c.healthcheck(),{type:'Timeout'}); await responder;
});
test('malformed responses fail without hanging', async t => {
  const s = await server(t); const c = client(t,s.lastEndpoint);
  const responder = (async () => { await s.receive(); await s.send('null'); })();
  await assert.rejects(c.healthcheck(),{type:'ProtocolError'}); await responder;
});
