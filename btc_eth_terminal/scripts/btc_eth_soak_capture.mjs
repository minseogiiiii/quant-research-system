/*
 * BTC/ETH read-only Coinbase public WebSocket bounded soak capture.
 * No API keys, strategy, orders, fills or automated trading permission.
 * Standard Node 22+ only. Evidence is never overwritten.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const PRODUCTS = ['BTC-USD', 'ETH-USD'];
const CHANNELS = ['level2', 'market_trades', 'heartbeats'];
const ENDPOINT = 'wss://advanced-trade-ws.coinbase.com';
const MAX_MESSAGE = 9_000_000;
const MAX_CHUNK = 8_000_000;
const MAX_CAPTURE = 300_000_000;
const MAX_RECONNECTS = 4;
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const now = () => new Date().toISOString();
const monotonic = () => process.hrtime.bigint().toString();
function envDuration() {
  const value = Number(process.env.SOAK_DURATION_SECONDS || '120');
  if (!Number.isInteger(value) || value < 10 || value > 3600) {
    throw Error('SOAK_DURATION_SECONDS must be an integer between 10 and 3600');
  }
  return value;
}
function mkdirNew(base) {
  fs.mkdirSync(base, {recursive: true});
  return fs.mkdtempSync(path.join(base, 'coinbase_btc_eth_soak_'));
}
const durationSeconds = envDuration();
const directory = mkdirNew(process.env.SOAK_OUTPUT_ROOT || 'soak_evidence');
const manifestPath = path.join(directory, 'manifest.json');
const chunksDir = path.join(directory, 'chunks');
fs.mkdirSync(chunksDir);
const manifest = {
  format: 'btc_eth_raw_soak_v10',
  source: ENDPOINT,
  source_authentication: 'PUBLIC_WEBSOCKET_NO_SIGNED_EXCHANGE_ATTESTATION',
  products: PRODUCTS,
  subscribed_channels: CHANNELS,
  source_payload_format: 'UTF8_WEBSOCKET_TEXT_NOT_ORIGINAL_WIRE_FRAMES',
  receiver_clock: 'LOCAL_WALL_UTC_AND_PROCESS_MONOTONIC',
  source_event_receipt_time_provenance: 'LOCAL_CALLBACK_ONLY',
  sampled_from_utc: now(),
  required_duration_seconds: durationSeconds,
  max_capture_bytes: MAX_CAPTURE,
  sequence_scope: 'PER_WEBSOCKET_CONNECTION',
  status: 'CAPTURING_UNVERIFIED',
  orders_enabled: false,
  strategy_execution_enabled: false,
  point_in_time_historical_availability_verified: false,
  executable_alpha_verified: false,
  chunks: [],
  connections: [],
  faults: [],
  bytes: 0,
  messages: 0,
  ready_for_research: false,
};
function persist() {
  const temp = manifestPath + '.tmp';
  const fd = fs.openSync(temp, 'wx');
  try {
    fs.writeFileSync(fd, JSON.stringify(manifest, null, 2) + '\n');
    fs.fsyncSync(fd);
  } finally { fs.closeSync(fd); }
  fs.renameSync(temp, manifestPath);
  // Each finalized manifest supersedes its previous durable version.
}
function safePersist() {
  try { persist(); } catch (err) {
    // Persistent failure becomes a failure; never treat the archive as complete.
    process.stderr.write('manifest write failed: ' + String(err) + '\n');
    throw err;
  }
}
let currentFd = null, currentPath = null, current = null;
let currentConn = null, ws = null;
let stopped = false, nextConnection = 0, reconnects = 0;
let deadline = Date.now() + durationSeconds * 1000;
let reconnectTimer = null;
function openChunk() {
  if (!currentConn) throw Error('No source connection');
  const index = manifest.chunks.length;
  const basename = `chunk_${String(index).padStart(5, '0')}_conn_${String(currentConn.id).padStart(3, '0')}`;
  currentPath = path.join(chunksDir, basename + '.jsonl.partial');
  currentFd = fs.openSync(currentPath, 'wx');
  current = {file: 'chunks/' + basename + '.jsonl', connection_id: currentConn.id,
    messages: 0, raw_text_bytes: 0, archived_line_bytes: 0,
    first_sequence: null, last_sequence: null,
    first_received_utc: null, last_received_utc: null};
  current._hash = crypto.createHash('sha256');
}
function closeChunk() {
  if (currentFd === null) return;
  fs.fsyncSync(currentFd);
  fs.closeSync(currentFd);
  currentFd = null;
  if (!current.messages) {
    fs.unlinkSync(currentPath);
  } else {
    const final = currentPath.slice(0, -'.partial'.length);
    fs.renameSync(currentPath, final);
    current.archive_sha256 = current._hash.digest('hex');
    delete current._hash;
    manifest.chunks.push(current);
    safePersist();
  }
  current = null; currentPath = null;
}
function append(raw, seq, wallUtc, monoNs) {
  if (currentFd === null) openChunk();
  const payload = Buffer.from(raw, 'utf8');
  const record = JSON.stringify({connection_id: currentConn.id,
    received_utc: wallUtc, local_monotonic_ns: monoNs,
    raw_text: raw, sha256: sha(payload)}) + '\n';
  const bytes = Buffer.from(record, 'utf8');
  if (payload.length > MAX_MESSAGE || bytes.length > MAX_MESSAGE * 2 ||
      manifest.bytes + payload.length > MAX_CAPTURE) throw Error('SIZE_LIMIT');
  if (current.messages && current.archived_line_bytes + bytes.length > MAX_CHUNK) {
    closeChunk(); openChunk();
  }
  const wrote = fs.writeSync(currentFd, bytes);
  if (wrote !== bytes.length) throw Error('PARTIAL_WRITE');
  current._hash.update(bytes);
  current.messages++;
  current.raw_text_bytes += payload.length;
  current.archived_line_bytes += bytes.length;
  current.first_sequence ??= seq;
  current.last_sequence = seq;
  current.first_received_utc ??= wallUtc;
  current.last_received_utc = wallUtc;
  manifest.bytes += payload.length;
  manifest.messages++;
  currentConn.messages++;
}
function fault(reason) {
  manifest.faults.push({connection_id: currentConn?.id ?? null, at_utc: now(), reason: String(reason).slice(0, 300)});
}
function finalize(reason) {
  if (stopped) return;
  stopped = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  try { ws?.close(1000, 'capture complete'); } catch {}
  try {
    if (currentConn && currentConn.state === 'CONNECTED') currentConn.state = 'DISCONNECTED';
    if (currentConn && !currentConn.ended_utc) currentConn.ended_utc = now();
    closeChunk();
    manifest.finished_at_utc = now();
    manifest.finish_reason = reason;
    manifest.total_reconnects = reconnects;
    manifest.all_connections_have_snapshots = manifest.connections.length > 0 &&
      manifest.connections.every(c => c.snapshots['BTC-USD'] && c.snapshots['ETH-USD']);
    manifest.complete_sequence_per_connection = manifest.connections.every(c => !c.integrity_fault);
    const allReady = manifest.connections.length > 0 && manifest.connections.every(c => c.became_ready && c.heartbeat_count > 0 && PRODUCTS.every(p => c.updates[p] > 0));
    if (manifest.faults.length === 0 && allReady && manifest.messages > 0 && reason === 'DURATION_REACHED') {
      manifest.status = 'CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH';
      manifest.ready_for_research = true; // only data-admission, never profitability
    } else if (allReady && manifest.messages > 0 && reason === 'DURATION_REACHED') {
      manifest.status = 'CAPTURE_COMPLETED_WITH_QUARANTINED_GAPS';
      manifest.ready_for_research = false;
    } else {
      manifest.status = 'CAPTURE_INCOMPLETE_OR_INVALID';
      manifest.ready_for_research = false;
    }
    manifest.manifest_integrity = 'CHUNK_BYTES_SHA256_VALIDATION_REQUIRED';
    safePersist();
    process.stdout.write(JSON.stringify({directory, status: manifest.status,
      messages: manifest.messages, chunks: manifest.chunks.length,
      connections: manifest.connections.length, faults: manifest.faults.length}) + '\n');
    if (manifest.status !== 'CAPTURE_COMPLETED_NO_GAPS_UNVERIFIED_MARKET_TRUTH') process.exitCode = 2;
  } catch (err) {
    process.stderr.write('finalization failed: ' + String(err) + '\n');
    process.exitCode = 3;
  }
}
function scheduleReconnect(reason) {
  if (stopped || Date.now() >= deadline) return finalize('DURATION_REACHED');
  fault(reason);
  if (currentConn) {
    currentConn.state = 'QUARANTINED';
    currentConn.integrity_fault = true;
    currentConn.ended_utc = now();
  }
  try { ws?.close(); } catch {}
  try { closeChunk(); } catch (e) { return finalize('ARCHIVE_FAILURE'); }
  if (reconnects >= MAX_RECONNECTS) return finalize('RECOVERY_BUDGET_EXHAUSTED');
  reconnects++;
  const delay = Math.min(500 * 2 ** (reconnects - 1), 4000);
  reconnectTimer = setTimeout(openConnection, delay);
}
function openConnection() {
  if (stopped || Date.now() >= deadline) return finalize('DURATION_REACHED');
  currentConn = {id:nextConnection++, state:'CONNECTING', started_utc:now(),
    messages:0, last_sequence:null, snapshots:{'BTC-USD':false,'ETH-USD':false},
    updates:{'BTC-USD':0,'ETH-USD':0}, heartbeat_count:0,
    became_ready:false, integrity_fault:false};
  manifest.connections.push(currentConn);
  try {ws = new WebSocket(ENDPOINT);} catch(e) {return scheduleReconnect('CREATE_SOCKET: '+e);}
  let ownWs=ws;
  ownWs.addEventListener('open',()=>{
    if (stopped || ws !== ownWs) return;
    currentConn.state='CONNECTED';
    currentConn.connected_utc=now();
    for(const channel of CHANNELS) ownWs.send(JSON.stringify({type:'subscribe',channel,product_ids:PRODUCTS}));
  });
  ownWs.addEventListener('message',ev=>{
    if (stopped || ws!==ownWs || currentConn.state!=='CONNECTED') return;
    const received = now();
    const localNs = monotonic();
    let raw;
    try {
      if(typeof ev.data!=='string')throw Error('NON_TEXT_WEBSOCKET_MESSAGE');
      raw=ev.data;
      const obj=JSON.parse(raw);
      const seq=obj.sequence_num;
      if(!Number.isSafeInteger(seq) || seq<0 || (currentConn.last_sequence===null && seq!==0)
        || (currentConn.last_sequence!==null && seq!==currentConn.last_sequence+1))throw Error('SEQUENCE_GAP_REPLAY_OR_INVALID');
      if(!['l2_data','market_trades','heartbeats','subscriptions'].includes(obj.channel)||!Array.isArray(obj.events)) throw Error('UNEXPECTED_MESSAGE');
      if(obj.channel==='l2_data')for(const event of obj.events){
        const p=event.product_id, k=event.type;
        if(!PRODUCTS.includes(p)||!['snapshot','update'].includes(k)||!Array.isArray(event.updates))throw Error('INVALID_BOOK_EVENT');
        if(k==='snapshot'){
          if(currentConn.snapshots[p])throw Error('DUPLICATE_SNAPSHOT');
          currentConn.snapshots[p]=true;
        } else {
          if(!currentConn.snapshots[p])throw Error('UPDATE_BEFORE_SNAPSHOT');
          currentConn.updates[p]++;
        }
      }
      if(obj.channel==='heartbeats')currentConn.heartbeat_count++;
      // Raw text always retained even if this packet causes a sequence fault.
      append(raw, seq, received, localNs);
      currentConn.last_sequence=seq;
      currentConn.last_received_utc=received;
      if(!currentConn.became_ready && PRODUCTS.every(p=>currentConn.snapshots[p])){
        currentConn.became_ready=true;currentConn.first_ready_utc=received;
      }
    }catch(e){
      if(raw!==undefined){
        // Archive any rejected source packet separately; do not place it in an admitted chunk.
        const bad = path.join(directory,`rejected_${currentConn.id}_${Date.now()}.txt`);
        try{fs.writeFileSync(bad,raw,{flag:'wx'});}catch{}
      }
      scheduleReconnect('INVALID_SOURCE_OR_STORAGE: '+String(e));
    }
  });
  ownWs.addEventListener('error',ev=>{
    if(!stopped && ws===ownWs && currentConn.state!=='QUARANTINED')scheduleReconnect('SOCKET_ERROR: '+String(ev.message||'unknown'));
  });
  ownWs.addEventListener('close',ev=>{
    if(!stopped && ws===ownWs && currentConn.state!=='QUARANTINED')scheduleReconnect('UNEXPECTED_CLOSE: '+ev.code);
  });
}

safePersist();
if(typeof WebSocket!=='function') {
  fault('NO_NATIVE_WEBSOCKET');
  finalize('NO_WEBSOCKET');
}else{
  openConnection();
  const deadlineTimer=setInterval(()=>{
    if(stopped){clearInterval(deadlineTimer);return;}
    if(Date.now()>=deadline){clearInterval(deadlineTimer);finalize('DURATION_REACHED');return;}
    if(currentConn?.state==='CONNECTED' && currentConn.last_received_utc &&
       Date.now()-Date.parse(currentConn.last_received_utc)>12_000) scheduleReconnect('FEED_STALE_OVER_12S');
  },1000);
}
