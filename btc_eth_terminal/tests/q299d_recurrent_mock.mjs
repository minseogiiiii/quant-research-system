/* Q299 synthetic-only WebSocket workload. No real network or orders. */
import fs from 'node:fs';
if(process.env.SOAK_TEST_FIXTURE!=='1'||process.env.V36_TAP_FIXTURE_MODE!=='1')
 throw Error('Q299_SYNTHETIC_ONLY');
const products=['BTC-USD','ETH-USD'];
const utc='2026-01-01T00:00:00Z';
const ordinary=(n)=>Array.from({length:10},(_,i)=>({
 side:'bid',price_level:String(n-i),new_quantity:String(i+1),event_time:utc
})).concat(Array.from({length:10},(_,i)=>({
 side:'offer',price_level:String(n+1+i),new_quantity:String(i+1),event_time:utc
})));
// Calibrate the escaped archival envelope at module initialization, before
// either collector sends its first snapshot. The offered burst contains no
// binary search, level-array generation, or repeated large serialization.
const frameTarget=5200000;
function wireText(events,seq){
 return '{"channel":"l2_data","events":'+events+
  ',"sequence_num":'+seq+',"timestamp":"'+new Date().toISOString()+'"}';
}
function envelopeSize(raw){
 return Buffer.byteLength(JSON.stringify({
  connection_id:0,received_utc:new Date().toISOString(),
  local_monotonic_ns:process.hrtime.bigint().toString(),
  raw_text:raw,sha256:'0'.repeat(64)
 })+'\n');
}
function prepare(product,quantity){
 const prefix='[{"product_id":"'+product+'","type":"update","updates":[';
 const suffix=']}]';
 const parts=[];
 let bytes=envelopeSize(wireText(prefix+suffix,0));
 for(let i=0;i<90000;i++){
  const part=JSON.stringify({side:'offer',price_level:String(1000+i),
   new_quantity:quantity,event_time:utc});
  const next=JSON.stringify(part).length-2+(parts.length?1:0);
  if(bytes+next>frameTarget)break;
  parts.push(part);bytes+=next;
 }
 const events=prefix+parts.join(',')+suffix;
 const raw=wireText(events,0), observed=envelopeSize(raw);
 if(observed<frameTarget-1000||observed>frameTarget||
    Buffer.byteLength(raw)>=9000000)throw Error('Q299_PREBUILD_SIZE');
 return events;
}
const prepared=['1.23456789','2.23456789'].map(q=>products.map(p=>prepare(p,q)));
class Fixture {
 constructor(url){
  if(url!=='wss://advanced-trade-ws.coinbase.com')throw Error('Q299_ENDPOINT');
  this.events={};this.seq=0;this.sent=0;this.closed=false;this.hb=0;
  this.cycle=0;this.rateTicks=0;this.maxFrame=0;
  setImmediate(()=>this.fire('open',{}));
 }
 addEventListener(k,fn){(this.events[k]??=[]).push(fn);}
 fire(k,v){for(const fn of this.events[k]??[])fn(v);}
 raw(channel,events){return JSON.stringify({
  channel,events,sequence_num:this.seq,timestamp:new Date().toISOString()
 });}
 size(raw){return Buffer.byteLength(JSON.stringify({
  connection_id:0,received_utc:new Date().toISOString(),
  local_monotonic_ns:process.hrtime.bigint().toString(),
  raw_text:raw,sha256:'0'.repeat(64)
 })+'\n');}
 emit(channel,events){
  if(this.closed)return 0;
  const raw=this.raw(channel,events), bytes=this.size(raw);
  this.seq++;this.maxFrame=Math.max(this.maxFrame,bytes);
  this.fire('message',{data:raw});return bytes;
 }
 small(){
  this.emit('l2_data',products.map((product_id,i)=>({
   product_id,type:'update',updates:[{side:'bid',
    price_level:String(100*(i+1)),new_quantity:'1',event_time:utc}]
  })));
 }
 large(product_id,target){
  // Genuine L2 update arrays with distinct, non-crossing offer levels.
  const updates=Array.from({length:90000},(_,i)=>({
   side:'offer',price_level:String(1000+i),new_quantity:'1.23456789',event_time:utc
  }));
  let lo=1,hi=updates.length,best=0;
  while(lo<=hi){
   const n=(lo+hi)>>1;
   const raw=this.raw('l2_data',[{product_id,type:'update',updates:updates.slice(0,n)}]);
   if(this.size(raw)<=target){best=n;lo=n+1;}else hi=n-1;
  }
  if(best<1)throw Error('Q299_SIZE_CALIBRATION');
  const events=[{product_id,type:'update',updates:updates.slice(0,best)}];
  const raw=this.raw('l2_data',events),bytes=this.size(raw);
  if(bytes<target-1000||Buffer.byteLength(raw)>=9000000)
   throw Error('Q299_FRAME_OUTSIDE_RAW_CAP');
  return this.emit('l2_data',events);
 }
 receipt(name,value){
  fs.writeFileSync(name+'.tmp',JSON.stringify(value)+'\n',{flag:'wx',mode:0o600});
  fs.renameSync(name+'.tmp',name);
 }
 tick(){
  if(this.closed)return;
  if(this.cycle<6&&this.rateTicks===0&&fs.existsSync('q299_go_'+(this.cycle+1))){
   this.cycle++;
   const raw=[], first=this.seq, variant=prepared[(this.cycle-1)%2];
   const offeredStart=process.hrtime.bigint();
   for(const events of [variant[0],variant[1],variant[0]]){
    const text=wireText(events,this.seq++);
    raw.push(text);
    this.fire('message',{data:text});
   }
   const offeredEnd=process.hrtime.bigint();
   const sizes=raw.map(envelopeSize);
   this.maxFrame=Math.max(this.maxFrame,...sizes);
   this.receipt('q299_offered_'+this.cycle+'.json',{
    cycle:this.cycle,first_sequence:first,first_burst_end:this.seq-1,
    frame_bytes:sizes,prebuilt:true,
    synchronous_offer_ns:Number(offeredEnd-offeredStart)});
   this.rateTicks=120;
  }
  if(this.rateTicks>0){
   // One 80-message microburst plus normal 20-message batches.
   // The measured trailing 100ms window must independently meet 70.
   const n=this.rateTicks===120?80:20;
   for(let i=0;i<n;i++)this.small();
   this.rateTicks--;
   if(this.rateTicks===0)this.receipt('q299_done_'+this.cycle+'.json',{
    cycle:this.cycle,end_sequence:this.seq-1,rate_messages:2460,max_frame_bytes:this.maxFrame
   });
  }else this.small();
  this.emit('heartbeats',[{heartbeat_counter:++this.hb}]);
 }
 send(raw){
  const m=JSON.parse(raw);
  if(m.type!=='subscribe'||!['level2','market_trades','heartbeats'].includes(m.channel))
   throw Error('Q299_UNSAFE_SUBSCRIPTION');
  if(++this.sent!==3)return;
  setTimeout(()=>{
   if(this.closed)return;
   this.emit('l2_data',products.map((product_id,i)=>({
    product_id,type:'snapshot',updates:ordinary(100*(i+1))
   })));
   this.emit('subscriptions',[{subscriptions:{
    level2:products,market_trades:products,heartbeats:['heartbeats']
   }}]);
   this.tick();this.timer=setInterval(()=>this.tick(),100);
  },25);
 }
 close(){
  if(this.closed)return;this.closed=true;clearInterval(this.timer);
  this.fire('close',{code:1000,wasClean:true});
 }
}
globalThis.WebSocket=Fixture;
