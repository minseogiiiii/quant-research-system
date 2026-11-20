/* Isolated public-only BTC/ETH WebSocket chunk audit probe, zero orders. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const products=['BTC-USD','ETH-USD'], channels=['level2','market_trades','heartbeats'];
const endpoint='wss://advanced-trade-ws.coinbase.com';
const seconds=Number(process.env.SOAK_DURATION_SECONDS||'150');
// These limits bound *written JSONL bytes*, not only raw exchange payload bytes.
// A real-data collector must fail closed before filling its host disk.
function nonnegativeSafeInteger(name, defaultValue, maximum) {
 const text=process.env[name];
 const value=text===undefined?defaultValue:Number(text);
 if(!Number.isSafeInteger(value)||value<0||value>maximum)throw Error('Invalid '+name);
 return value;
}
const diskReserveBytes=nonnegativeSafeInteger('SOAK_DISK_RESERVE_BYTES',128*1024*1024,10**12);
const archiveBudgetBytes=nonnegativeSafeInteger('SOAK_ARCHIVE_BUDGET_BYTES',400*1024*1024,800*1024*1024);
if(archiveBudgetBytes<1024)throw Error('SOAK_ARCHIVE_BUDGET_BYTES must be >=1024');
const telemetry={archive_budget_bytes:archiveBudgetBytes,disk_reserve_bytes:diskReserveBytes,
 archived_jsonl_bytes:0,peak_rss_bytes:process.memoryUsage().rss,
 max_write_latency_ns:0,total_write_latency_ns:0,max_fsync_latency_ns:0,
 min_available_disk_bytes:null,disk_checks:0};
if(!Number.isSafeInteger(seconds)||seconds<15||seconds>600)throw Error('duration 15..600 seconds');
fs.mkdirSync('soak_evidence',{recursive:true});
const root=fs.mkdtempSync(path.join('soak_evidence/','coinbase_btc_eth_soak_'));
const chunksDir=path.join(root,'chunks'); fs.mkdirSync(chunksDir);
const hash=x=>crypto.createHash('sha256').update(x).digest('hex'), utc=()=>new Date().toISOString();
const meta={format:'btc_eth_raw_soak_v10',source:endpoint,
 source_authentication:'PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION',
 products,subscribed_channels:channels,orders_enabled:false,strategy_execution_enabled:false,
 point_in_time_historical_availability_verified:false,executable_alpha_verified:false,
 sampled_from_utc:utc(),required_duration_seconds:seconds,max_capture_bytes:300_000_000,
 sequence_scope:'PER_WEBSOCKET_CONNECTION',status:'CAPTURING_UNVERIFIED',ready_for_research:false,
 chunks:[],connections:[],faults:[],bytes:0,messages:0,resource_telemetry:telemetry};
const manifestPath=path.join(root,'manifest.json');
function syncDirectory(dir){const handle=fs.openSync(dir,fs.constants.O_RDONLY);
 try{fs.fsyncSync(handle);}finally{fs.closeSync(handle);}}
function diskAdmission(nextBytes) {
 if(telemetry.archived_jsonl_bytes+nextBytes>archiveBudgetBytes)throw Error('ARCHIVE_BUDGET_EXCEEDED');
 const stat=fs.statfsSync(root,{bigint:true});
 const available=stat.bavail*stat.bsize;
 telemetry.disk_checks++;
 const measured=available>BigInt(Number.MAX_SAFE_INTEGER)?Number.MAX_SAFE_INTEGER:Number(available);
 if(telemetry.min_available_disk_bytes===null||measured<telemetry.min_available_disk_bytes)
  telemetry.min_available_disk_bytes=measured;
 if(available<BigInt(diskReserveBytes)+BigInt(nextBytes))throw Error('DISK_RESERVE_EXCEEDED');
 telemetry.peak_rss_bytes=Math.max(telemetry.peak_rss_bytes,process.memoryUsage().rss);
}
function timedFsync(handle){const start=process.hrtime.bigint();fs.fsyncSync(handle);
 const elapsed=Number(process.hrtime.bigint()-start);
 telemetry.max_fsync_latency_ns=Math.max(telemetry.max_fsync_latency_ns,elapsed);}

function persist(){const temp=manifestPath+'.tmp';const fd=fs.openSync(temp,'wx');
 try{fs.writeSync(fd,JSON.stringify(meta,null,2)+'\n');timedFsync(fd);}finally{fs.closeSync(fd);}fs.renameSync(temp,manifestPath);syncDirectory(root);}
let c=null, fd=null, part=null, chunk=null, ws=null, stopped=false, reconnects=0;
let lastEventMs=Date.now(), timer=null, backoff=null;
function closeChunk(){if(fd===null)return; timedFsync(fd);fs.closeSync(fd);fd=null;
 if(chunk.messages){const final=part.slice(0,-8);fs.renameSync(part,final);syncDirectory(chunksDir);
 chunk.archive_sha256=chunk.digest.digest('hex');delete chunk.digest;meta.chunks.push(chunk);persist();}
 else {fs.unlinkSync(part);syncDirectory(chunksDir);}chunk=null;part=null;}
function openChunk(){const i=meta.chunks.length;
 const base='chunk_'+String(i).padStart(5,'0')+'_conn_'+String(c.id).padStart(3,'0');
 part=path.join(chunksDir,base+'.jsonl.partial');fd=fs.openSync(part,'wx');
 chunk={file:'chunks/'+base+'.jsonl',connection_id:c.id,messages:0,raw_text_bytes:0,
 archived_line_bytes:0,first_sequence:null,last_sequence:null,first_received_utc:null,last_received_utc:null,
 digest:crypto.createHash('sha256')};}
function append(raw,seq,received,monotonic){
 const rawBytes=Buffer.from(raw);const bytes=Buffer.from(JSON.stringify({connection_id:c.id,
 received_utc:received,local_monotonic_ns:monotonic,raw_text:raw,sha256:hash(rawBytes)})+'\n');
 if(rawBytes.length>9_000_000||meta.bytes+rawBytes.length>300_000_000||bytes.length>18_000_000)throw Error('SIZE_CAP');
 diskAdmission(bytes.length);
 if(fd===null)openChunk();if(chunk.messages&&chunk.archived_line_bytes+bytes.length>8_000_000){closeChunk();openChunk();}
 const started=process.hrtime.bigint();
 if(fs.writeSync(fd,bytes)!==bytes.length)throw Error('PARTIAL_DISK_WRITE');
 const elapsed=Number(process.hrtime.bigint()-started);
 telemetry.max_write_latency_ns=Math.max(telemetry.max_write_latency_ns,elapsed);
 telemetry.total_write_latency_ns+=elapsed;
 telemetry.archived_jsonl_bytes+=bytes.length;
 chunk.digest.update(bytes);chunk.messages++;chunk.raw_text_bytes+=rawBytes.length;
 chunk.archived_line_bytes+=bytes.length;chunk.first_sequence??=seq;chunk.last_sequence=seq;
 chunk.first_received_utc??=received;chunk.last_received_utc=received;meta.bytes+=rawBytes.length;
 meta.messages++;c.messages++;}
function finish(reason){if(stopped)return;stopped=true;clearInterval(timer);clearTimeout(backoff);
 try{ws?.close();}catch{};try{if(c&&!c.ended_utc)c.ended_utc=utc();if(c&&c.state==='CONNECTED')c.state='DISCONNECTED';closeChunk();
 meta.finished_at_utc=utc();meta.finish_reason=reason;meta.total_reconnects=reconnects;
 telemetry.peak_rss_bytes=Math.max(telemetry.peak_rss_bytes,process.memoryUsage().rss);
 meta.complete_sequence_per_connection=meta.connections.every(x=>!x.integrity_fault);
 meta.all_connections_have_snapshots=meta.connections.length>0&&meta.connections.every(x=>products.every(p=>x.snapshots[p]));
 const ready=meta.connections.length===1&&meta.connections.every(x=>x.became_ready&&x.heartbeat_count>0&&products.every(p=>x.updates[p]>0));
 if(reason==='DURATION_REACHED'&&ready&&meta.faults.length===0){meta.status='CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH';meta.ready_for_research=true;}
 else if(reason==='DURATION_REACHED'&&meta.messages>0){meta.status='CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS';}
 else meta.status='CAPTURE_INCOMPLETE_OR_INVALID';
 persist();console.log(JSON.stringify({directory:root,status:meta.status,messages:meta.messages,chunks:meta.chunks.length,connections:meta.connections.length,faults:meta.faults.length}));
 if(!meta.ready_for_research)process.exitCode=2;
 }catch(e){console.error('ARCHIVE_FINALIZE_ERROR',String(e));process.exitCode=3;}}
function fail(reason){if(stopped)return;
 meta.faults.push({connection_id:c?.id??null,at_utc:utc(),reason:String(reason).slice(0,280)});
 if(c){c.integrity_fault=true;c.state='QUARANTINED';c.ended_utc=utc();}
 try{ws?.close();}catch(e){meta.faults.push({connection_id:c?.id??null,at_utc:utc(),reason:'SOCKET_CLOSE_ERROR:'+String(e).slice(0,250)});}
 try{closeChunk();}catch(e){
  meta.faults.push({connection_id:c?.id??null,at_utc:utc(),reason:'ARCHIVE_ERROR:'+String(e).slice(0,250)});
  return finish('DISK_FAILURE');}
 if(/ARCHIVE_BUDGET_EXCEEDED|DISK_RESERVE_EXCEEDED|PARTIAL_DISK_WRITE|SIZE_CAP|ARCHIVE_ERROR/.test(String(reason)))return finish('NONRECOVERABLE_STORAGE_LIMIT');
 if(reconnects>=3)return finish('RECONNECT_BUDGET');
 reconnects++;backoff=setTimeout(connect,500*reconnects);}
function connect(){if(stopped)return;c={id:meta.connections.length,state:'CONNECTING',started_utc:utc(),messages:0,
 last_sequence:null,snapshots:{'BTC-USD':false,'ETH-USD':false},updates:{'BTC-USD':0,'ETH-USD':0},
 heartbeat_count:0,became_ready:false,integrity_fault:false};meta.connections.push(c);
 let client;try{client=new WebSocket(endpoint);ws=client;}catch(e){fail('CONNECT:'+e);return;}
 client.addEventListener('open',()=>{if(stopped||ws!==client)return;c.state='CONNECTED';c.connected_utc=utc();
 for(const channel of channels)client.send(JSON.stringify({type:'subscribe',channel,product_ids:products}));});
 client.addEventListener('message',ev=>{if(stopped||ws!==client||c.state!=='CONNECTED')return;
 const received=utc(),mono=process.hrtime.bigint().toString();let raw;
 try{if(typeof ev.data!=='string')throw Error('NON_TEXT');raw=ev.data;
 const m=JSON.parse(raw),seq=m.sequence_num;
 if(!Number.isSafeInteger(seq)||seq<0||(c.last_sequence===null?seq!==0:seq!==c.last_sequence+1))throw Error('SEQUENCE_GAP');
 if(!['subscriptions','heartbeats','l2_data','market_trades'].includes(m.channel)||!Array.isArray(m.events))throw Error('CHANNEL');
 if(m.channel==='l2_data')for(const e of m.events){if(!products.includes(e.product_id)||!Array.isArray(e.updates)||!['snapshot','update'].includes(e.type))throw Error('BAD_BOOK_EVENT');
 if(e.type==='snapshot'){if(c.snapshots[e.product_id])throw Error('DUP_SNAPSHOT');c.snapshots[e.product_id]=true;}
 else {if(!c.snapshots[e.product_id])throw Error('UPDATE_BEFORE_SNAPSHOT');c.updates[e.product_id]++;}}
 if(m.channel==='heartbeats')c.heartbeat_count++;
 append(raw,seq,received,mono);c.last_sequence=seq;c.last_received_utc=received;lastEventMs=Date.now();
 if(!c.became_ready&&products.every(p=>c.snapshots[p])){c.became_ready=true;c.first_ready_utc=received;}
 }catch(e){if(raw){try{fs.writeFileSync(path.join(root,'rejected_'+c.id+'_'+Date.now()+'.txt'),raw,{flag:'wx'});}catch{}}
 fail('SOURCE:'+String(e));}});
 client.addEventListener('error',e=>{if(!stopped&&ws===client&&c.state!=='QUARANTINED')fail('ERROR:'+String(e.message||e));});
 client.addEventListener('close',e=>{if(!stopped&&ws===client&&c.state!=='QUARANTINED')fail('CLOSE:'+e.code);});}
persist();
process.once('SIGTERM',()=>finish('SIGTERM_QUARANTINE'));
process.once('SIGINT',()=>finish('SIGINT_QUARANTINE'));
if(typeof WebSocket!=='function')finish('NO_WEBSOCKET');else{connect();const deadline=Date.now()+seconds*1000;
 timer=setInterval(()=>{if(stopped)return;if(Date.now()>=deadline)finish('DURATION_REACHED');
 else if(c?.state==='CONNECTED'&&Date.now()-lastEventMs>12000)fail('STALE_FEED');},1000);}
