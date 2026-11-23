/* Q24 P0-A: synthetic-only WebSocket lifecycle probe contract. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

test('private WebSocket close probe captures timing without payload or secrets',async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'q24_probe_fixture_'));
 try{
  const output=path.join(root,'ws_trace.jsonl');
  process.env.Q24_WS_DIAG_PRIVATE_PATH=output;
  class SyntheticSocket {
   constructor(url){this.url=url;this.handlers=new Map();}
   addEventListener(name,callback){
    if(!this.handlers.has(name))this.handlers.set(name,[]);
    this.handlers.get(name).push(callback);
   }
   fire(name,event){for(const cb of this.handlers.get(name)??[])cb(event);}
   close(){this.fire('close',{code:1000,wasClean:true,reason:'SECRET_REASON'});
    return 'unchanged_native_return';}
   send(){return 42;}
  }
  globalThis.WebSocket=SyntheticSocket;
  await import('../scripts/q24_ws_close_probe.mjs');
  const socket=new WebSocket('wss://PRIVATE_TOKEN_DO_NOT_LEAK');
  assert.ok(socket instanceof SyntheticSocket);
  assert.equal(socket.send('private price=100'),42);
  socket.fire('open',{});
  socket.fire('message',{data:JSON.stringify({
   channel:'heartbeats',sequence_num:0,
   events:[{heartbeat_counter:1,secret:'PRIVATE_TRADE_PAYLOAD'}],
  })});
  socket.fire('message',{data:JSON.stringify({
   channel:'l2_data',secret:'PRIVATE_PRICE_123',
  })});
  socket.fire('error',{message:'SECRET_NETWORK_ENDPOINT_567'});
  socket.fire('close',{
   code:1006,wasClean:false,reason:'PRIVATE_CLOSE_REASON_abc',
  });
  const second=new WebSocket('wss://OTHER_PRIVATE_TOKEN');
  second.fire('open',{});
  assert.equal(second.close(),'unchanged_native_return');
  const bytes=fs.readFileSync(output,'utf8');
  const lines=bytes.trim().split('\n').map(JSON.parse);
  assert.equal(lines.length,9);
  assert.deepEqual(lines.map(e=>e.seq),Array.from({length:9},(_,i)=>i));
  assert.deepEqual(lines.map(e=>e.event),[
   'TRACE_START','SOCKET_CONSTRUCTED','SOCKET_OPEN',
   'SOCKET_ERROR','SOCKET_CLOSE','SOCKET_CONSTRUCTED','SOCKET_OPEN',
   'LOCAL_CLOSE_REQUEST','SOCKET_CLOSE',
  ]);
  const first=lines.find(e=>e.event==='SOCKET_CLOSE'&&e.connection_index===0);
  assert.equal(first.close_code,1006);
  assert.equal(first.was_clean,false);
  assert.equal(first.local_close_requested,false);
  assert.equal(first.heartbeat_count,1);
  assert.equal(first.message_count,2);
  assert.ok(first.last_heartbeat_age_ms>=0);
  const local=lines.at(-1);
  assert.equal(local.close_code,1000);
  assert.equal(local.local_close_requested,true);
  assert.equal(lines[5].connection_index,1);
  assert.ok(lines[5].reconnect_delay_ms>=0);
  assert.ok((fs.statSync(output).mode&0o077)===0);
  for(const secret of [
   'PRIVATE_TOKEN','PRIVATE_PRICE','PRIVATE_TRADE','SECRET_NETWORK',
   'SECRET_REASON','PRIVATE_CLOSE','wss://','password','api_key',
  ])assert.equal(bytes.includes(secret),false,'unexpected trace: '+secret);
  assert.equal(first.ping_pong_control_frames_visible,false);
  assert.equal(first.real_orders_enabled,false);
 }finally{
  delete process.env.Q24_WS_DIAG_PRIVATE_PATH;
  fs.rmSync(root,{recursive:true,force:true});
 }
});
