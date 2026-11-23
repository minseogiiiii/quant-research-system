/* Q26 EXPERIMENTAL synthetic-only heartbeat-channel watchdog.
 * It is deliberately NOT enabled for real Coinbase, production, 5h, or
 * research. In the original frozen collector, STALE_FEED only fires when
 * ALL frames stop, so continuing L2 frames can hide a heartbeat-only loss.
 * This test-only wrapper forces fail-closed connection rotation before
 * the pinned v36 5-second heartbeat health threshold.
 */
if(process.env.SOAK_TEST_FIXTURE!=='1'||
   process.env.Q26_TEST_SCENARIO!=='HEARTBEAT_ONLY_SILENCE')
 throw Error('Q26_WATCHDOG_REQUIRES_EXPLICIT_SYNTHETIC_SOURCE');
const Native=globalThis.WebSocket;
if(typeof Native!=='function')throw Error('Q26_NO_SYNTHETIC_WS');
const WATCHDOG_MS=4500;
globalThis.WebSocket=new Proxy(Native,{
 construct(target,args){
  const socket=Reflect.construct(target,args,target);
  let lastHeartbeatMs=null;
  let stopped=false;
  socket.addEventListener('message',event=>{
   const value=event?.data;
   if(typeof value!=='string'||value.length>8192||
      !value.includes('"channel"')||!value.includes('"heartbeats"'))return;
   try{
    if(JSON.parse(value)?.channel==='heartbeats')
     lastHeartbeatMs=performance.now();
   }catch{/* collector still exclusively validates all source JSON */}
  });
  socket.addEventListener('close',()=>{stopped=true;clearInterval(timer);});
  const timer=setInterval(()=>{
   if(stopped||lastHeartbeatMs===null)return;
   if(performance.now()-lastHeartbeatMs<=WATCHDOG_MS)return;
   stopped=true;
   clearInterval(timer);
   // Fixed static marker only, no source text/URL/price or secret.
   process.stdout.write('Q26_SYNTHETIC_HEARTBEAT_WATCHDOG_INTERVENED\n');
   socket.close(); // existing collector close callback triggers fresh source.
  },90);
  timer.unref();
  return socket;
 }
});
