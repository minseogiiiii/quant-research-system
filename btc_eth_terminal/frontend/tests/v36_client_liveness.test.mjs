import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {once} from 'node:events';
import {freshSourceHealth,readOnlyJson,HEALTH_PROBE_MAX_AGE_MS} from '../src/v36_client_liveness.ts';

const live={mode:'LIVE_SOURCE',live_feed:true,trading_enabled:false,status:'HEALTHY'};
const archive={mode:'ARCHIVE_SIMULATION',live_feed:false,trading_enabled:false,status:'HEALTHY'};

test('fresh health is bounded by monotonic elapsed time and source mode',()=>{
 for(const h of [live,archive]){
  assert.equal(freshSourceHealth(h,0,0),true);
  assert.equal(freshSourceHealth(h,100,100+HEALTH_PROBE_MAX_AGE_MS),true);
  assert.equal(freshSourceHealth(h,100,101+HEALTH_PROBE_MAX_AGE_MS),false);
 }
 for(const h of [null,{...live,status:'STALE'},{...live,live_feed:false},
  {...archive,live_feed:true},{...live,trading_enabled:true}]){
  assert.equal(freshSourceHealth(h,100,101),false);
 }
 for(const v of [null,NaN,Infinity,-1])assert.equal(freshSourceHealth(live,v,100),false);
 for(const v of [NaN,Infinity,98])assert.equal(freshSourceHealth(live,100,v),false);
});

test('unresponsive HTTP health probe aborts within its explicit budget',async()=>{
 const server=createServer((_req,_res)=>{});
 server.listen(0,'127.0.0.1');await once(server,'listening');
 try{
  const {port}=server.address();
  const started=performance.now();
  await assert.rejects(readOnlyJson(`http://127.0.0.1:${port}/health`,undefined,150),
    err=>err.name==='TimeoutError'||err.name==='AbortError');
  assert.ok(performance.now()-started<1500,'fetch request was not bounded');
 }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('caller abort remains authoritative during a pending request',async()=>{
 const server=createServer((_req,_res)=>{});
 server.listen(0,'127.0.0.1');await once(server,'listening');
 const ctrl=new AbortController();
 try{
  const {port}=server.address();
  const pending=readOnlyJson(`http://127.0.0.1:${port}/snapshot`,ctrl.signal,4000);
  ctrl.abort();
  await assert.rejects(pending,err=>err.name==='AbortError');
 }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('valid read-only JSON resolves and failed HTTP status rejects',async()=>{
 const server=createServer((req,res)=>{
  res.setHeader('Content-Type','application/json');
  res.statusCode=req.url==='/api/v36/health'?200:503;
  res.end(JSON.stringify(req.url==='/api/v36/health'?live:{error:'SOURCE_UNHEALTHY'}));
 });
 server.listen(0,'127.0.0.1');await once(server,'listening');
 try{
  const {port}=server.address();
  assert.deepEqual(await readOnlyJson(`http://127.0.0.1:${port}/api/v36/health`,undefined,1000),live);
  await assert.rejects(readOnlyJson(`http://127.0.0.1:${port}/api/v36/snapshot`,undefined,1000),/HTTP 503/);
 }finally{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});

test('LiveTerminal gates displayed quote, label, and chart by fresh health',async()=>{
 const {readFile}=await import('node:fs/promises');
 const src=await readFile(new URL('../src/LiveTerminal.tsx',import.meta.url),'utf8');
 assert.match(src,/const healthFresh=freshSourceHealth\(health,healthProbeAt,clockNow\)/);
 assert.match(src,/const trustedQuote=healthFresh/);
 assert.match(src,/\{trustedQuote && state \?<>/);
 assert.match(src,/color:trustedQuote\?/);
 assert.match(src,/HEALTH_PROBE_EXPIRED/);
 assert.match(src,/await readOnlyJson<Health>/);
 assert.doesNotMatch(src,/await api<Health>/);
});
