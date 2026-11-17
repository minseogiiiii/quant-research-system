/* SYNTHETIC TEST WEBSOCKET. Loaded only by Node --import in contract tests.
   These messages MUST never be represented as real Coinbase observations. */
const products=['BTC-USD','ETH-USD'];
const mkLevels=p=>Array.from({length:10},(_,i)=>({
 side:'bid',price_level:String(p-i),new_quantity:String(i+1)})).concat(
 Array.from({length:10},(_,i)=>({side:'offer',price_level:String(p+1+i),new_quantity:String(i+1)})));
const messages=[
 {channel:'l2_data',events:[{type:'snapshot',product_id:'BTC-USD',updates:mkLevels(100)}]},
 {channel:'l2_data',events:[{type:'snapshot',product_id:'ETH-USD',updates:mkLevels(200)}]},
 {channel:'subscriptions',events:[{subscriptions:{level2:products,market_trades:products,heartbeats:['heartbeats']}}]},
 {channel:'heartbeats',events:[{heartbeat_counter:1}]},
 {channel:'market_trades',events:[{type:'update',trades:[{product_id:'BTC-USD',price:'100',size:'0.1',side:'BUY',trade_id:'test-1'}]}]},
 {channel:'l2_data',events:[{type:'update',product_id:'BTC-USD',updates:[{side:'bid',price_level:'100',new_quantity:'7'}]}]},
 {channel:'l2_data',events:[{type:'update',product_id:'ETH-USD',updates:[{side:'offer',price_level:'201',new_quantity:'3'}]}]},
 {channel:'heartbeats',events:[{heartbeat_counter:2}]},
]; 
// Strictly synthetic 4.7 MiB first Coinbase-style L2 frame. The padding is
// untrusted fixture content, never presented as exchange market observations.
if(process.env.V36_TAP_LARGE_SNAPSHOT_TEST==='1') messages[0].synthetic_padding='x'.repeat(4_700_000);
class MockCoinbaseWebSocket {
 constructor(endpoint){
  if(endpoint!=='wss://advanced-trade-ws.coinbase.com')throw Error('Unexpected endpoint');
  this.handlers={};this.sent=0;this.stopped=false;
  setImmediate(()=>this.fire('open',{}));
 }
 addEventListener(name,callback){(this.handlers[name]??=[]).push(callback);}
 fire(name,event){for(const f of this.handlers[name]??[])f(event);}
 send(data){
  const msg=JSON.parse(data);
  if(msg.type!=='subscribe'||!['level2','market_trades','heartbeats'].includes(msg.channel))throw Error('Unsafe subscribe');
  if(++this.sent!==3)return;
  messages.forEach((data,i)=>setTimeout(()=>{
   if(this.stopped)return;
   this.fire('message',{data:JSON.stringify({...data,sequence_num:i,timestamp:new Date().toISOString()})});
  },30*(i+1)));
 }
 close(){this.stopped=true;this.fire('close',{code:1000});}
}
globalThis.WebSocket=MockCoinbaseWebSocket;
