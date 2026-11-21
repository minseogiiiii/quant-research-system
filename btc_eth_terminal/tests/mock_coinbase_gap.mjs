/* A synthetic forced message gap and clean new session: TEST DATA ONLY. */
import './mock_coinbase_websocket.mjs';
const Base = globalThis.WebSocket;
let sessions=0;
class GapWebSocket extends Base {
  constructor(url){
    super(url);
    this.syntheticSession=++sessions;
  }
  sendSample(){
    super.sendSample();
    if(this.syntheticSession===1){
      setTimeout(()=>{
        if(this.closed)return;
        const malformed={channel:'heartbeats',sequence_num:999,
          timestamp:new Date().toISOString(),events:[{heartbeat_counter:'2',current_time:new Date().toISOString()}]};
        this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(malformed)}));
      },50);
    }
  }
}
globalThis.WebSocket=GapWebSocket;
