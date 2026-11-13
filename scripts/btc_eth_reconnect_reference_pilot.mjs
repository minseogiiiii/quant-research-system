/* Read-only two-segment Coinbase WebSocket recovery and REST book probe.
 * All sources are raw archived. Never place orders or claim executable alpha.
 * Node 22+ only. Intentional disconnect between separate sessions.
 */
import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';

const ROOT = 'reconnect_evidence';
fs.mkdirSync(ROOT,{recursive:true});
const PRODUCTS = ['BTC-USD','ETH-USD'], CHANNELS=['level2','market_trades','heartbeats'];
const ENDPOINT='wss://advanced-trade-ws.coinbase.com';
const DURATION_MS=15000;
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const iso = () => new Date().toISOString();
const summary = {scope:'PUBLIC_BTC_ETH_NO_AUTH_NO_ORDERS',started_at_utc:iso(),
  intentional_reconnection:true,parallel_rest_books:true,segments:[],
  source_clock_synchronization_verified:false,strict_replay_verified:false,
  independently_matched_book_verified:false,executable_alpha_verified:false,
  status:'STARTED'};
let refsPending=[];

async function probeBook(folder, product){
  const url='https://api.coinbase.com/api/v3/brokerage/market/product_book?product_id='+encodeURIComponent(product)+'&limit=5';
  const requested_at_utc=iso(), started_ns=process.hrtime.bigint().toString();
  let outcome={product,requested_at_utc,started_ns,url,success:false};
  try{
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),7500);
    try{
      const res=await fetch(url,{signal:controller.signal,headers:{Accept:'application/json'},redirect:'error'});
      const raw=Buffer.from(await res.arrayBuffer());
      const finished_ns=process.hrtime.bigint().toString();
      if(raw.length>1_000_000)throw Error('Oversized REST response');
      const relative=path.join('reference_books',product.replace('-','_')+'.json');
      fs.mkdirSync(path.join(folder,'reference_books'),{recursive:true});
      fs.writeFileSync(path.join(folder,relative),raw);
      outcome={...outcome,received_at_utc:iso(),finished_ns,status_code:res.status,
        response_mime_type:res.headers.get('content-type'),raw_sha256:sha(raw),
        raw_bytes:raw.length,raw_file:relative,success:res.ok,
        raw_response_not_synchronized_with_ws:true};
    }finally{clearTimeout(timeout);}
  }catch(e){outcome={...outcome,received_at_utc:iso(),error:String(e).slice(0,200)};}
  const f=path.join(folder,'reference_books',product.replace('-','_')+'.meta.json');
  fs.mkdirSync(path.dirname(f),{recursive:true});
  fs.writeFileSync(f,JSON.stringify(outcome,null,2)+'\n');
  return outcome;
}
async function capture(label){
  const folder=path.join(ROOT,label);
  fs.mkdirSync(folder,{recursive:true});
  const fd=fs.openSync(path.join(folder,'raw_websocket_payloads.jsonl'),'w');
  const streamhash=crypto.createHash('sha256');
  const st={status:'INCOMPLETE',products:PRODUCTS,channels:CHANNELS,
    endpoint:ENDPOINT,start_utc:iso(),records:0,payload_bytes:0,error_count:0,
    hash_is_of_utf8_text_not_wire_frames:true,
    receive_time_is_local_not_exchange_time:true,
    verified_sequence_integrity:false,verified_profitability:false,orders_enabled:false,
    segment_label:label,controlled_disconnect_requested:false,errors:[],
    counts:{'BTC-USD':{snapshot:0,update:0,trades:0},
            'ETH-USD':{snapshot:0,update:0,trades:0}},channel_count:{}};
  let finished=false, opened=false, ws, resolveDone, error=false;
  const done=new Promise(resolve=>{resolveDone=resolve;});
  const stop=(reason)=>{
    if(finished)return;
    finished=true;
    st.end_utc=iso();st.finish_reason=reason;
    st.stream_hash=streamhash.digest('hex');
    try{fs.fsyncSync(fd);fs.closeSync(fd);}catch(e){st.errors.push('close failed: '+String(e));st.error_count++;}
    const both=PRODUCTS.every(p=>st.counts[p].snapshot>0 && st.counts[p].update>0);
    st.status=both&&opened&&st.error_count===0&&reason==='intentional_rotation'
      ?'PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED':'INCOMPLETE_OR_UNVERIFIED';
    st.closed_early=reason!=='intentional_rotation';
    st.controlled_disconnect_requested=reason==='intentional_rotation';
    fs.writeFileSync(path.join(folder,'capture_status.json'),JSON.stringify(st,null,2)+'\n');
    try{ws?.close(1000,'intentional test rotation');}catch{}
    resolveDone(st);
  };
  ws=new WebSocket(ENDPOINT);
  ws.addEventListener('open',()=>{
    opened=true;
    for(const channel of CHANNELS){
      ws.send(JSON.stringify({type:'subscribe',channel,product_ids:PRODUCTS}));
    }
  });
  ws.addEventListener('message',ev=>{
    if(finished)return;
    const raw=typeof ev.data==='string'?ev.data:ev.data.toString();
    const n=Buffer.byteLength(raw);
    if(n>8_000_000||st.payload_bytes+n>24_000_000){
      st.errors.push('bounded source size exceeded');st.error_count++;stop('size_limit');return;
    }
    const row={received_utc:iso(),local_monotonic_ns:process.hrtime.bigint().toString(),
      raw_text:raw,sha256:sha(raw)};
    try{fs.writeSync(fd,JSON.stringify(row)+'\n');}catch(e){
      st.errors.push('write failed: '+String(e));st.error_count++;stop('write_failure');return;
    }
    streamhash.update(raw+'\n');st.records++;st.payload_bytes+=n;
    try{
      const message=JSON.parse(raw),ch=message.channel||message.type||'unknown';
      st.channel_count[ch]=(st.channel_count[ch]||0)+1;
      for(const event of message.events||[]){
        if(ch==='l2_data'&&PRODUCTS.includes(event.product_id)
            &&(event.type==='snapshot'||event.type==='update')){
          st.counts[event.product_id][event.type]++;
        }
        if(ch==='market_trades'&&Array.isArray(event.trades)){
          for(const trade of event.trades)if(PRODUCTS.includes(trade.product_id))
            st.counts[trade.product_id].trades++;
        }
      }
    }catch(e){st.errors.push('invalid message: '+String(e).slice(0,200));st.error_count++;stop('parse_failure');}
  });
  ws.addEventListener('error',e=>{if(!finished){st.errors.push('WebSocket error: '+String(e.message||e));st.error_count++;stop('network_failure');}});
  ws.addEventListener('close',e=>{if(!finished){st.errors.push('Unexpected close '+e.code);st.error_count++;stop('unexpected_disconnect');}});
  const restTask=new Promise(resolve=>setTimeout(resolve,6500)).then(
    ()=>Promise.all(PRODUCTS.map(p=>probeBook(folder,p))));
  const timeout=setTimeout(()=>stop('intentional_rotation'),DURATION_MS);
  const status=await done;
  clearTimeout(timeout);
  const refs=await restTask;
  return {label,folder,status:status.status,status_sha256:sha(fs.readFileSync(path.join(folder,'capture_status.json'))),
    first_receive_utc:status.start_utc,last_receive_utc:status.end_utc,
    messages:status.records,initial_snapshots:Object.fromEntries(PRODUCTS.map(p=>[p,status.counts[p].snapshot])),
    rest:refs};
}
try{
  if(typeof WebSocket!=='function')throw Error('Native WebSocket unavailable on Node');
  summary.segments.push(await capture('segment_before_disconnect'));
  await new Promise(resolve=>setTimeout(resolve,1800));
  summary.segments.push(await capture('segment_after_reconnect'));
  summary.status=summary.segments.every(s=>s.status==='PUBLIC_L2_TEXT_CAPTURED_NOT_RECONSTRUCTED')
    ?'TWO_SEGMENTS_CAPTURED_NOT_AUDITED':'INCOMPLETE';
}catch(e){summary.status='FAILED';summary.error=String(e);}
finally{
  summary.ended_at_utc=iso();
  fs.writeFileSync(path.join(ROOT,'reconnect_summary.json'),JSON.stringify(summary,null,2)+'\n');
  console.log(JSON.stringify(summary));
  if(summary.status!=='TWO_SEGMENTS_CAPTURED_NOT_AUDITED')process.exitCode=1;
}
