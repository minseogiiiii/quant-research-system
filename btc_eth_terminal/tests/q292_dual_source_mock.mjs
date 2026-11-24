/* Synthetic only. Stable dual L2 books and ongoing valid heartbeats. */
if(process.env.SOAK_TEST_FIXTURE!=='1')throw Error('SYNTHETIC_ONLY');
const products=['BTC-USD','ETH-USD'];
const levels=n=>Array.from({length:10},(_,i)=>({
 side:'bid',price_level:String(n-i),new_quantity:String(i+1)
})).concat(Array.from({length:10},(_,i)=>({
 side:'offer',price_level:String(n+1+i),new_quantity:String(i+1)
})));
class Fixture {
 constructor(url){
  if(url!=='wss://advanced-trade-ws.coinbase.com')throw Error('BAD_ENDPOINT');
  this.events={};this.sent=0;this.seq=0;this.closed=false;this.timer=null;
  setImmediate(()=>this.fire('open',{}));
 }
 addEventListener(k,fn){(this.events[k]??=[]).push(fn);}
 fire(k,v){for(const fn of this.events[k]??[])fn(v);}
 emit(channel,events){
  if(!this.closed)this.fire('message',{data:JSON.stringify({
   channel,events,sequence_num:this.seq++,timestamp:new Date().toISOString()
  })});
 }
 send(raw){
  const m=JSON.parse(raw);
  if(m.type!=='subscribe'||!['level2','market_trades','heartbeats'].includes(m.channel))
   throw Error('UNSAFE_SUBSCRIBE');
  if(++this.sent!==3)return;
  setTimeout(()=>{
   if(this.closed)return;
   this.emit('l2_data',products.map((product_id,i)=>({
    product_id,type:'snapshot',updates:levels(100*(i+1))
   })));
   this.emit('subscriptions',[{subscriptions:{
    level2:products,market_trades:products,heartbeats:['heartbeats']
   }}]);
   let hb=0;
   const tick=()=>{
    this.emit('heartbeats',[{heartbeat_counter:++hb}]);
    this.emit('l2_data',products.map((product_id,i)=>({
     product_id,type:'update',updates:[{
      side:'bid',price_level:String(100*(i+1)),new_quantity:'1'
     }]
    })));
   };
   tick();this.timer=setInterval(tick,250);
  },25);
 }
 close(){
  if(this.closed)return;this.closed=true;clearInterval(this.timer);
  this.fire('close',{code:1000,wasClean:true});
 }
}
globalThis.WebSocket=Fixture;
