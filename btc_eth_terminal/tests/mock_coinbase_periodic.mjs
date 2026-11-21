/* In-memory heartbeat fixture: 3-second intervals, never exchange observations. */
import './mock_coinbase_websocket.mjs';
const Base=globalThis.WebSocket;
class Periodic extends Base {
  sendSample(){
    super.sendSample();
    if(this.closed)return; // A synchronous collector close can occur during sendSample().
    this.heartbeat=setInterval(()=>{
      if(this.closed)return;
      const now=new Date().toISOString();
      this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({
        channel:'heartbeats',sequence_num:this.seq++,timestamp:now,
        events:[{heartbeat_counter:String(this.seq),current_time:now}]})}));
    },3000);
  }
  close(){if(this.heartbeat)clearInterval(this.heartbeat);this.closed=true;this.dispatchEvent(new Event('close'));}
}
globalThis.WebSocket=Periodic;
