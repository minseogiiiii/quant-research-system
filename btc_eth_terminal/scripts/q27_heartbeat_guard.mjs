/* Q27 P0-D: explicitly opted-in heartbeat-channel liveness guard.
 * Node.js --import preload, NOT a modification to the frozen Q14 collector.
 * No network I/O, orders, source frames, URL, prices or credentials logged.
 * A timeout closes the existing WebSocket; the unchanged collector's close
 * handler must invalidate the old epoch before any reconnect.
 *
 * WHY: the frozen collector's STALE_FEED only handles ALL-FRAME silence.
 * Continued L2 traffic can mask a missing Coinbase heartbeats channel.
 */
import fs from 'node:fs';
import path from 'node:path';

if(process.env.Q27_HEARTBEAT_GUARD_ENABLED!=='1')
 throw Error('Q27_EXPLICIT_ENABLE_FLAG_REQUIRED');
if(!['0','1'].includes(process.env.SOAK_TEST_FIXTURE??''))
 throw Error('Q27_SOURCE_MODE_NOT_EXPLICIT');
const privatePath=process.env.Q27_HEARTBEAT_TRACE_PRIVATE;
if(typeof privatePath!=='string'||!path.isAbsolute(privatePath)||
   Buffer.byteLength(privatePath)>200)
 throw Error('Q27_ABSOLUTE_PRIVATE_TRACE_PATH_REQUIRED');
const parent=fs.lstatSync(path.dirname(privatePath));
if(!parent.isDirectory()||(parent.mode&0o077)!==0)
 throw Error('Q27_PRIVATE_DIR_REJECTED');
const Base=globalThis.WebSocket;
if(typeof Base!=='function')throw Error('Q27_WEBSOCKET_REQUIRED');
const flag=fs.constants.O_WRONLY|fs.constants.O_EXCL|fs.constants.O_CREAT|
 (fs.constants.O_NOFOLLOW??0);
const fd=fs.openSync(privatePath,flag,0o600);
const MAX_BYTES=55_000,MAX_EVENTS=400,PERIOD_MS=100;
const STARTUP_GRACE_MS=9000,HEARTBEAT_STALE_MS=4500;
const begun=process.hrtime.bigint();
const now=()=>Number((process.hrtime.bigint()-begun)/1_000_000n);
let count=0,bytes=0,safe=true,connections=0,active=null,violations=0;
const sockets=[];
function write(event,s=null){
 if(!safe)return;
 const t=now();
 const data={
  schema:'q27_heartbeat_guard_event_v1',
  index:count,event,
  socket_index:s?.index??null,
  elapsed_ms:t,
  socket_age_ms:s?Math.max(0,t-s.born):null,
  last_heartbeat_age_ms:s==null||s.lastHeartbeat===null?null:
   Math.max(0,t-s.lastHeartbeat),
  heartbeat_count:s?.heartbeatCount??0,
  other_message_count:s?.otherMessageCount??0,
  timeout_threshold_ms:HEARTBEAT_STALE_MS,
  startup_grace_ms:STARTUP_GRACE_MS,
  interventions_total:violations,
  synthetic_fixture:process.env.SOAK_TEST_FIXTURE==='1',
  real_orders_enabled:false,
  native_ping_pong_visible:false,
 };
 const b=Buffer.from(JSON.stringify(data)+'\n');
 if(count>=MAX_EVENTS||bytes+b.length>MAX_BYTES){safe=false;return;}
 try{
  if(fs.writeSync(fd,b)!==b.length)throw Error('SHORT_TRACE_WRITE');
  fs.fsyncSync(fd);count++;bytes+=b.length;
 }catch{safe=false;}
}
write('GUARD_START');
globalThis.WebSocket=new Proxy(Base,{
 construct(target,args){
  // Do not swallow WebSocket constructor errors; the original collector owns
  // its connection failure/retry budget.
  const socket=Reflect.construct(target,args,target);
  const s={index:connections++,born:now(),lastHeartbeat:null,
   heartbeatCount:0,otherMessageCount:0,opened:null,stopped:false,
   timeoutSent:false,timer:null};
  active=s;sockets.push(s);
  write('SOCKET_CONSTRUCTED',s);
  socket.addEventListener('open',()=>{
   s.opened=now();write('SOCKET_OPEN',s);
  });
  socket.addEventListener('message',event=>{
   if(s.stopped)return;
   const raw=event?.data;
   if(typeof raw!=='string'||raw.length>8192){
    s.otherMessageCount++;return;
   }
   if(!raw.includes('"channel"')||!raw.includes('heartbeats')){
    s.otherMessageCount++;return;
   }
   try{
    const wire=JSON.parse(raw);
    if(wire?.channel==='heartbeats'&&
       Number.isSafeInteger(wire.sequence_num)&&
       wire.sequence_num>=0&&
       Array.isArray(wire.events)&&wire.events.length>0){
      s.lastHeartbeat=now();
      s.heartbeatCount++;
      if(s.heartbeatCount===1)write('FIRST_HEARTBEAT',s);
      return;
    }
   }catch{/* receiver's original validation is unchanged */}
   s.otherMessageCount++;
  });
  socket.addEventListener('close',()=>{
   if(s.stopped)return;
   s.stopped=true;clearInterval(s.timer);write('SOCKET_CLOSE',s);
  });
  socket.addEventListener('error',()=>{write('SOCKET_ERROR',s);});
  s.timer=setInterval(()=>{
   if(s.stopped||s.timeoutSent||s.opened===null)return;
   const t=now();
   const stale=s.lastHeartbeat===null
    ?t-s.opened>STARTUP_GRACE_MS
    :t-s.lastHeartbeat>HEARTBEAT_STALE_MS;
   if(!stale)return;
   // Only a documented, enum-labelled liveness event is persisted.
   // Source/close reason, endpoint, original error text stay private.
   s.timeoutSent=true;violations++;
   write(s.lastHeartbeat===null?
     'FIRST_HEARTBEAT_TIMEOUT':'HEARTBEAT_STALE_FORCE_CLOSE',s);
   try{
    // The original collector's CLOSE handler quarantines the old source,
    // not the guard. Do not synthesize continuation or bypass order limits.
    socket.close();
   }catch{
    write('GUARD_CLOSE_THROW',s);
    // A close failure cannot be allowed to continue publishing stale
    // market data. Abort the entire process; downstream observer fails
    // closed on local tap EOF.
    process.exitCode=42;
    process.kill(process.pid,'SIGTERM');
   }
  },PERIOD_MS);
  s.timer.unref();
  return socket;
 }
});
process.once('exit',()=>{
 write('PROCESS_EXIT',active);
 for(const s of sockets)clearInterval(s.timer);
 try{fs.closeSync(fd);}catch{}
});
