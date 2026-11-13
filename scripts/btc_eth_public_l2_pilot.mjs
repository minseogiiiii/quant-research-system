/* Short public-only BTC/ETH level2 capture; no orders/auth, no alpha claims. */
import fs from 'node:fs';
import crypto from 'node:crypto';
fs.mkdirSync('l2_evidence', {recursive:true});
const f = fs.openSync('l2_evidence/raw_websocket_payloads.jsonl','w');
const state = {status:'INCOMPLETE',products:['BTC-USD','ETH-USD'],channels:['level2','market_trades','heartbeats'],endpoint:'wss://advanced-trade-ws.coinbase.com',start_utc:new Date().toISOString(),records:0,payload_bytes:0,error_count:0,hash_is_of_utf8_text_not_wire_frames:true,receive_time_is_local_not_exchange_time:true,verified_sequence_integrity:false,verified_profitability:false,orders_enabled:false,counts:{'BTC-USD':{snapshot:0,update:0,trades:0},'ETH-USD':{snapshot:0,update:0,trades:0}},channel_count:{},errors:[]};
const hash=crypto.createHash('sha256');
let completed=false, closed=false, client;
function finish(reason){
  if(completed)return; completed=true;
  try{client?.close();}catch{}
  try{fs.fsyncSync(f);fs.closeSync(f);}catch{}
  state.end_utc=new Date().toISOString();state.finish_reason=reason;
  state.stream_hash=hash.digest('hex');state.closed_early=closed;
  const both=state.products.every(p=>state.counts[p].snapshot>0);
  state.status=both&&!closed&&!state.error_count?'PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED':'INCOMPLETE_OR_UNVERIFIED';
  fs.writeFileSync('l2_evidence/capture_status.json',JSON.stringify(state,null,2)+'\n');
  console.log(JSON.stringify(state));
  process.exitCode=state.status==='PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED'?0:1;
}
if(typeof WebSocket!=='function'){
  state.errors.push('Node native WebSocket unavailable');state.error_count++;finish('no_native_websocket');
}else{
  client=new WebSocket(state.endpoint);
  client.addEventListener('open',()=>{
    for(const channel of state.channels)client.send(JSON.stringify({type:'subscribe',channel,product_ids:state.products}));
  });
  client.addEventListener('message',evt=>{
    if(completed)return;
    const raw=typeof evt.data==='string'?evt.data:evt.data.toString();
    const size=Buffer.byteLength(raw,'utf8');
    if(size>8_000_000||state.payload_bytes+size>24_000_000){state.errors.push('message/capture byte cap exceeded');state.error_count++;finish('limit');return;}
    const row={received_utc:new Date().toISOString(),local_monotonic_ns:process.hrtime.bigint().toString(),raw_text:raw,sha256:crypto.createHash('sha256').update(raw).digest('hex')};
    fs.writeSync(f,JSON.stringify(row)+'\n');hash.update(raw+'\n');state.records++;state.payload_bytes+=size;
    try{
      const m=JSON.parse(raw),ch=m.channel||m.type||'unknown';
      state.channel_count[ch]=(state.channel_count[ch]||0)+1;
      for(const e of m.events||[]){
        if(Object.hasOwn(state.counts,e.product_id)){
          if(ch==='l2_data'&&(e.type==='snapshot'||e.type==='update'))state.counts[e.product_id][e.type]++;
          if(ch==='market_trades'&&Array.isArray(e.trades))state.counts[e.product_id].trades+=e.trades.length;
        }
      }
    }catch(ex){state.error_count++;state.errors.push(String(ex).slice(0,120));}
  });
  client.addEventListener('error',ev=>{state.error_count++;state.errors.push(String(ev.message||'WebSocket error'));});
  client.addEventListener('close',ev=>{if(!completed){closed=true;state.errors.push('Socket closed: '+ev.code);finish('early_close');}});
  setTimeout(()=>finish('20s_sampling_complete'),20000);
}
