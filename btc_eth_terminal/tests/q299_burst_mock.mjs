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
class Fixture {
 constructor(url){
  if(url!=='wss://advanced-trade-ws.coinbase.com')throw Error('Q299_ENDPOINT');
  this.events={};this.seq=0;this.sent=0;this.closed=false;this.hb=0;
  this.stress=false;this.rateTicks=0;this.maxFrame=0;
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
  if(!this.stress&&fs.existsSync('q299_go')){
   this.stress=true;
   const mode=fs.readFileSync('q299_go','utf8').trim();
   if(mode==='burst'){
    // Prebuild/serialize each frame independently: actual receive-time windows
    // are measured from the sealed archive, not from the offered target.
    const first=this.large(products[0],7500000);
    const second=this.large(products[1],7500000);
    this.receipt('q299_offered.json',{mode,first,second,first_burst_end:this.seq-1});
    this.rateTicks=120;
   }else if(mode==='overflow'){
    const offered=this.large(products[0],9390000);
    // Next record forces a previous oversized record's chunk to be sealed.
    this.small();
    this.receipt('q299_offered.json',{mode,oversized_bytes:offered,
     oversized_sequence:this.seq-2});
   }else throw Error('Q299_MODE');
  }
  if(this.rateTicks>0){
   // One 80-message microburst plus normal 20-message batches.
   // The measured trailing 100ms window must independently meet 70.
   const n=this.rateTicks===120?80:20;
   for(let i=0;i<n;i++)this.small();
   this.rateTicks--;
   if(this.rateTicks===0)this.receipt('q299_done.json',{
    end_sequence:this.seq-1,rate_messages:2460,max_frame_bytes:this.maxFrame
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
