/* Q24 P0-A. Optional CI-only WebSocket lifecycle forensics preload.
 *
 * Wraps the existing global WebSocket constructor without touching frozen
 * Coinbase collector code. Never records URL, payload, price, token, close
 * reason, error.message, headers, IP address or session ID.
 * WHATWG WebSocket does not expose native ping/pong control frames.
 */
import fs from 'node:fs';
import path from 'node:path';

const file=process.env.Q24_WS_DIAG_PRIVATE_PATH;
if(typeof file!=='string'||!path.isAbsolute(file)||Buffer.byteLength(file)>200)
 throw Error('Q24_ABSOLUTE_PRIVATE_DIAGNOSTIC_PATH_REQUIRED');
const parent=fs.lstatSync(path.dirname(file));
if(!parent.isDirectory()||(parent.mode&0o077)!==0)
 throw Error('Q24_PRIVATE_DIRECTORY_PERMISSIONS_INVALID');
const Base=globalThis.WebSocket;
if(typeof Base!=='function')throw Error('Q24_NATIVE_WEBSOCKET_MISSING');
const flags=fs.constants.O_CREAT|fs.constants.O_EXCL|fs.constants.O_WRONLY|
 (fs.constants.O_NOFOLLOW??0);
const fd=fs.openSync(file,flags,0o600);
const begun=process.hrtime.bigint();
const MAX_EVENTS=480, MAX_BYTES=190_000;
let n=0,used=0,usable=true,idx=0,previousCloseMs=null;
const connections=[];
const elapsed=()=>Number((process.hrtime.bigint()-begun)/1_000_000n);
const age=(then,now)=>then===null?null:Math.max(0,now-then);
function emit(event,s=null,extra={}){
 if(!usable)return;
 const now=elapsed();
 const obj={
  schema:'q24_ws_close_event_v1',seq:n,event,
  connection_index:s?.index??null,elapsed_ms:now,
  connection_uptime_ms:s?age(s.start,now):null,
  message_count:s?.messages??0,heartbeat_count:s?.heartbeats??0,
  last_message_age_ms:s?age(s.lastMessage,now):null,
  last_heartbeat_age_ms:s?age(s.lastHeartbeat,now):null,
  close_code:extra.closeCode??null,was_clean:extra.wasClean??null,
  local_close_requested:s?.localCloseRequested??false,
  reconnect_delay_ms:extra.reconnectDelayMs??null,
  ping_pong_control_frames_visible:false,real_orders_enabled:false,
 };
 const line=Buffer.from(JSON.stringify(obj)+'\n');
 if(n>=MAX_EVENTS||used+line.length>MAX_BYTES){usable=false;return;}
 try{
  const written=fs.writeSync(fd,line);
  if(written!==line.length)throw Error('Q24_INCOMPLETE_TRACE_WRITE');
  fs.fsyncSync(fd);
  used+=written;n++;
 }catch{usable=false;}
}
emit('TRACE_START');
function attach(ws){
 const now=elapsed();
 const state={index:idx++,start:now,open:null,
              lastMessage:null,lastHeartbeat:null,
              messages:0,heartbeats:0,localCloseRequested:false};
 connections.push(state);
 const delay=previousCloseMs===null?null:Math.max(0,now-previousCloseMs);
 emit('SOCKET_CONSTRUCTED',state,{reconnectDelayMs:delay});
 const originalClose=ws.close.bind(ws);
 // Intercept ONLY local close requests, preserving the native method's
 // arguments, return value, exception behavior, and event delivery.
 Object.defineProperty(ws,'close',{configurable:true,writable:true,
  value:function(...args){
   state.localCloseRequested=true;
   emit('LOCAL_CLOSE_REQUEST',state);
   return originalClose(...args);
  }});
 ws.addEventListener('open',()=>{
  state.open=elapsed();
  emit('SOCKET_OPEN',state);
 });
 ws.addEventListener('message',ev=>{
  const t=elapsed();state.messages++;state.lastMessage=t;
  // Heartbeats have small JSON envelopes. Do not parse, persist, or inspect
  // large L2/trade frames beyond their bounded string length.
  const raw=ev?.data;
  if(typeof raw==='string'&&raw.length<=8192&&
     raw.includes('"heartbeats"')&&raw.includes('"channel"')){
   try{
    if(JSON.parse(raw)?.channel==='heartbeats'){
     state.heartbeats++;state.lastHeartbeat=t;
    }
   }catch{/* the actual collector owns wire validation */}
  }
 });
 ws.addEventListener('error',()=>{
  // ErrorEvent.message may contain IPs, URLs and connection details.
  emit('SOCKET_ERROR',state);
 });
 ws.addEventListener('close',ev=>{
  const code=ev?.code;
  const wasClean=ev?.wasClean;
  emit('SOCKET_CLOSE',state,{
   closeCode:Number.isInteger(code)&&code>=1000&&code<=4999?code:null,
   wasClean:typeof wasClean==='boolean'?wasClean:null,
  });
  previousCloseMs=elapsed();
 });
 return ws;
}
globalThis.WebSocket=new Proxy(Base,{
 construct(target,args){
  const ws=Reflect.construct(target,args,target);
  return attach(ws);
 },
});
const interval=setInterval(()=>{
 const s=connections.at(-1)??null;
 if(s)emit('PROGRESS',s);
},60_000);
interval.unref();
process.once('exit',()=>{
 try{emit('PROCESS_EXIT',connections.at(-1)??null);}finally{
  clearInterval(interval);
  try{fs.closeSync(fd);}catch{}
 }
});
