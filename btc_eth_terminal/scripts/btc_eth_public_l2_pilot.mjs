/* Public BTC/ETH Coinbase L2/market_trades/heartbeats recording, no orders. */
import fs from 'node:fs';
import crypto from 'node:crypto';

const DURATION_MS = 35000;
const MAX_MESSAGE_BYTES = 8000000;
const MAX_SESSION_BYTES = 24000000;
const dir = process.env.L2_CAPTURE_DIR || 'l2_evidence';
fs.mkdirSync(dir,{recursive:true});
const f = fs.openSync(`${dir}/raw_websocket_payloads.jsonl`,'wx');
const hash = crypto.createHash('sha256');
const products = ['BTC-USD','ETH-USD'];
const state = {
  status:'INCOMPLETE',products,channels:['level2','market_trades','heartbeats'],
  endpoint:'wss://advanced-trade-ws.coinbase.com',start_utc:new Date().toISOString(),
  records:0,payload_bytes:0,error_count:0,
  hash_is_of_utf8_text_not_wire_frames:true,
  receive_time_is_local_not_exchange_time:true,
  verified_sequence_integrity:false,verified_profitability:false,orders_enabled:false,
  capture_duration_ms:DURATION_MS,
  counts:Object.fromEntries(products.map(p=>[p,{snapshot:0,update:0,trades:0}])),
  channel_count:{},errors:[],
};
let completed = false, closed = false, client;
function finish(reason) {
  if(completed) return;
  completed=true;
  try{client?.close();}catch{}
  try{fs.fsyncSync(f);fs.closeSync(f);}catch{}
  state.end_utc=new Date().toISOString();
  state.finish_reason=reason;
  state.stream_hash=hash.digest('hex');
  state.closed_early=closed;
  const both=products.every(p=>state.counts[p].snapshot===1 && state.counts[p].update>0);
  const heartbeats=(state.channel_count.heartbeats||0)>0;
  state.status=both && heartbeats && !closed && !state.error_count
    ?'PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED':'INCOMPLETE_OR_UNVERIFIED';
  fs.writeFileSync(`${dir}/capture_status.json`,JSON.stringify(state,null,2)+'\n');
  console.log(JSON.stringify(state));
  process.exitCode=state.status==='PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED'?0:1;
}
if(typeof WebSocket!=='function') {
  state.error_count++;state.errors.push('Node native WebSocket unavailable');finish('no_native_websocket');
} else {
  client = new WebSocket(state.endpoint);
  client.addEventListener('open',()=>{
    for(const channel of state.channels)
      client.send(JSON.stringify({type:'subscribe',channel,product_ids:products}));
  });
  client.addEventListener('message',evt=>{
    if(completed) return;
    const raw=typeof evt.data==='string'?evt.data:evt.data.toString();
    const size=Buffer.byteLength(raw,'utf8');
    if(size>MAX_MESSAGE_BYTES||state.payload_bytes+size>MAX_SESSION_BYTES){
      state.errors.push('message/capture byte cap exceeded');state.error_count++;finish('limit');return;
    }
    const row={received_utc:new Date().toISOString(),
      local_monotonic_ns:process.hrtime.bigint().toString(),
      raw_text:raw,sha256:crypto.createHash('sha256').update(raw).digest('hex')};
    fs.writeSync(f,JSON.stringify(row)+'\n');
    hash.update(raw+'\n');state.records++;state.payload_bytes+=size;
    try {
      const m=JSON.parse(raw), ch=m.channel||m.type||'unknown';
      state.channel_count[ch]=(state.channel_count[ch]||0)+1;
      for(const e of m.events||[]){
        if(ch==='market_trades'&&Array.isArray(e.trades)){
          for(const trade of e.trades){
            if(Object.hasOwn(state.counts,trade.product_id))
              state.counts[trade.product_id].trades++;
          }
        }
        if(Object.hasOwn(state.counts,e.product_id)&&ch==='l2_data'&&
            (e.type==='snapshot'||e.type==='update'))
          state.counts[e.product_id][e.type]++;
      }
    }catch(ex){state.error_count++;state.errors.push(String(ex).slice(0,120));}
  });
  client.addEventListener('error',ev=>{
    state.error_count++;state.errors.push(String(ev.message||'WebSocket error'));
  });
  client.addEventListener('close',ev=>{
    if(!completed){closed=true;state.errors.push('Socket closed: '+ev.code);finish('early_close');}
  });
  setTimeout(()=>finish('35s_sampling_complete'),DURATION_MS);
}
