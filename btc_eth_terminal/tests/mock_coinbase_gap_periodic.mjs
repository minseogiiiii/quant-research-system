/* Gap is injected once; later session remains healthy with periodic heartbeats. */
import './mock_coinbase_gap.mjs';
const Base=globalThis.WebSocket;
class GapPeriodic extends Base {
  sendSample(){
    super.sendSample();
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
globalThis.WebSocket=GapPeriodic;
