/* Q25 ONLY: staged synthetic Coinbase-style WebSocket test source.
 * This never contacts Coinbase. The collector remains the unchanged
 * production file and its fault injection must never be research admitted.
 */
const products=['BTC-USD','ETH-USD'];
const levels=n=>Array.from({length:10},(_,i)=>({
 side:'bid',price_level:String(n-i),new_quantity:'1'
})).concat(Array.from({length:10},(_,i)=>({
 side:'offer',price_level:String(n+1+i),new_quantity:'2'
})));
const snap=(p,n)=>({channel:'l2_data',events:[{
 type:'snapshot',product_id:p,updates:levels(n),
}]});
const ack={channel:'subscriptions',events:[{
 subscriptions:{level2:products,market_trades:products,
                heartbeats:['heartbeats']}
}]};
const hb=n=>({channel:'heartbeats',events:[{heartbeat_counter:n}]});
const update=p=>({channel:'l2_data',events:[{
 type:'update',product_id:p,updates:[{
  side:'bid',price_level:p==='BTC-USD'?'100':'200',new_quantity:'4'
 }]
}]});
let nextSocket=0;
class StagedWebSocket{
 constructor(url){
  if(url!=='wss://advanced-trade-ws.coinbase.com')throw Error('WRONG_ENDPOINT');
  this.id=nextSocket++;this.handlers={};this.sent=0;this.stopped=false;
  setImmediate(()=>this.fire('open',{}));
 }
 addEventListener(name,fn){(this.handlers[name]??=[]).push(fn);}
 fire(name,event){for(const fn of this.handlers[name]??[])fn(event);}
 send(value){
  const m=JSON.parse(value);
  if(m.type!=='subscribe'||!['heartbeats','level2','market_trades'].includes(m.channel))
   throw Error('INVALID_SUBSCRIBE');
  if(++this.sent!==3)return;
  const stages=this.id===0?[
   [10,snap('BTC-USD',100)],
   [30,snap('ETH-USD',200)],
   [55,ack],
   [85,hb(1)],
   [115,update('BTC-USD')],
   [1200,hb(2)],
  ]:[
   [10,snap('BTC-USD',100)],
   [40,ack],
   [70,hb(1)],
   [1500,snap('ETH-USD',200)],
   [1600,update('BTC-USD')],
  ];
  for(const [delay,data] of stages){
   setTimeout(()=>{
    if(this.stopped)return;
    // Per-connection Coinbase sequence restart at zero, in send order.
    const i=stages.findIndex(z=>z[1]===data);
    this.fire('message',{data:JSON.stringify({
     ...data,sequence_num:i,timestamp:new Date().toISOString(),
    })});
   },delay);
  }
 }
 close(){this.stopped=true;this.fire('close',{code:1000,wasClean:true});}
}
globalThis.WebSocket=StagedWebSocket;
