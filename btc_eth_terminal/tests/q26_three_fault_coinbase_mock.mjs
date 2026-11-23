/* Q26 synthetic-only WebSocket source. NO network I/O or Coinbase attestation.
 * A separate test supplies Q26_TEST_SCENARIO:
 *   NETWORK_CLOSE: unexpected abnormal WebSocket close, 1006
 *   SEQUENCE_GAP: corrupted wire sequence after valid dual book
 *   ALL_FEED_SILENCE: heartbeat AND other frames stop; original collector
 *     autonomously triggers STALE_FEED after 12s, then reconnects
 *   HEARTBEAT_ONLY_SILENCE: source L2 keeps arriving while heartbeat stops.
 *     This is a NEGATIVE liveness/control case, not evidence of recovery.
 */
const scenario=process.env.Q26_TEST_SCENARIO;
const choices=new Set([
 'NETWORK_CLOSE','SEQUENCE_GAP','ALL_FEED_SILENCE','HEARTBEAT_ONLY_SILENCE'
]);
if(!choices.has(scenario)||process.env.SOAK_TEST_FIXTURE!=='1')
 throw Error('Q26_SYNTHETIC_FIXTURE_MUST_BE_EXPLICIT');
const products=['BTC-USD','ETH-USD'];
const levels=n=>Array.from({length:10},(_,i)=>({
 side:'bid',price_level:String(n-i),new_quantity:String(i+1)
})).concat(Array.from({length:10},(_,i)=>({
 side:'offer',price_level:String(n+1+i),new_quantity:String(i+1)
})));
const snap=(product,n)=>({
 channel:'l2_data',events:[{type:'snapshot',product_id:product,updates:levels(n)}]
});
const ack={channel:'subscriptions',events:[{
 subscriptions:{level2:products,market_trades:products,
                heartbeats:['heartbeats']}
}]};
const hb=n=>({channel:'heartbeats',events:[{heartbeat_counter:n}]});
const update=(product,index)=>({
 channel:'l2_data',events:[{
 type:'update',product_id:product,updates:[{
 side:'bid',price_level:product==='BTC-USD'?'100':'200',
 new_quantity:String(4+index)
 }]
}]});
let nextId=0;
class SyntheticWebSocket{
 constructor(url){
  if(url!=='wss://advanced-trade-ws.coinbase.com')
   throw Error('EXPECTED_PINNED_PUBLIC_SOURCE_ENDPOINT');
  this.id=nextId++;this.listeners=new Map();this.sent=0;
  this.stopped=false;this.timers=[];
  setImmediate(()=>this.fire('open',{}));
 }
 addEventListener(event,cb){
  if(!this.listeners.has(event))this.listeners.set(event,[]);
  this.listeners.get(event).push(cb);
 }
 fire(event,data){
  for(const cb of this.listeners.get(event)??[])cb(data);
 }
 later(delay,fn){
  const t=setTimeout(()=>{if(!this.stopped)fn();},delay);
  this.timers.push(t);
 }
 message(data,sequence){
  if(this.stopped)return;
  this.fire('message',{data:JSON.stringify({
   ...data,sequence_num:sequence,timestamp:new Date().toISOString()
  })});
 }
 remoteClose(){
  if(this.stopped)return;
  this.stopped=true;
  this.timers.forEach(clearTimeout);
  // 1006 is a locally reported abnormal closure, not a sent WS close frame.
  this.fire('close',{code:1006,wasClean:false,reason:'PRIVATE_NOT_PERSISTED'});
 }
 close(){
  if(this.stopped)return;
  this.stopped=true;
  this.timers.forEach(clearTimeout);
  this.fire('close',{code:1000,wasClean:true});
 }
 send(raw){
  const obj=JSON.parse(raw);
  if(obj.type!=='subscribe'||
     !['level2','market_trades','heartbeats'].includes(obj.channel)||
     JSON.stringify(obj.product_ids)!==JSON.stringify(products))
   throw Error('UNSAFE_SYNTHETIC_SUBSCRIPTION');
  if(++this.sent!==3)return;
  const first=this.id===0;
  const schedule=first?[
   [20,snap('BTC-USD',100),0],
   [45,snap('ETH-USD',200),1],
   [70,ack,2],
   [95,hb(1),3],
   [120,update('BTC-USD',1),4],
   [145,update('ETH-USD',2),5],
   [170,hb(2),6],
  ]:[
   [20,snap('BTC-USD',100),0],
   [80,ack,1],
   [110,hb(1),2],
   [1700,snap('ETH-USD',200),3],
   [1850,update('BTC-USD',3),4],
   [1950,update('ETH-USD',4),5],
   [2050,hb(2),6],
  ];
  for(const [delay,data,seq] of schedule)
   this.later(delay,()=>this.message(data,seq));
  if(!first)return;
  if(scenario==='NETWORK_CLOSE')
   this.later(1100,()=>this.remoteClose());
  if(scenario==='SEQUENCE_GAP')
   this.later(1100,()=>this.message(update('BTC-USD',5),11));
  if(scenario==='HEARTBEAT_ONLY_SILENCE'){
   // New L2 traffic continuously arrives, but heartbeat count is frozen
   // at 2. The original 12s any-message idle rule therefore won't fire.
   for(let i=1;i<=18;i++)
    this.later(350+i*450,()=>this.message(
     update(i%2?'BTC-USD':'ETH-USD',i),6+i
    ));
  }
  // ALL_FEED_SILENCE: no further messages; the original collector handles
  // its own unmodified 12s stale timeout, then builds a fresh source.
 }
}
globalThis.WebSocket=SyntheticWebSocket;
