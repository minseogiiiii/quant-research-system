import './v36_mock_coinbase_websocket.mjs';
const Base=globalThis.WebSocket;
let sockets=0;
class Duplicate extends Base {
 constructor(endpoint){super(endpoint);this.index=sockets++;}
 send(data){
  super.send(data);
  if(this.sent===3&&this.index===0)setTimeout(()=>{
   if(!this.stopped)this.fire('message',{data:JSON.stringify({channel:'l2_data',sequence_num:8,timestamp:new Date().toISOString(),events:[{type:'snapshot',product_id:'BTC-USD',updates:[{side:'bid',price_level:'100',new_quantity:'7'}]}]})});
  },450);
 }
}
globalThis.WebSocket=Duplicate;
