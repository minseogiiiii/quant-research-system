import test from 'node:test';
import assert from 'node:assert/strict';
import {MarketStateStore,validateFrame} from './compiled/datahub/resilientStore.js';
const now=1794641293625,sid='a'.repeat(32),run='b'.repeat(32),sourceRun='c'.repeat(32);
function row(product,session=sid,version=1){
 const bids=Array.from({length:10},(_,i)=>({price:String(100-i),quantity:'2',cumulative_base:String(2*(i+1))}));
 const asks=Array.from({length:10},(_,i)=>({price:String(101+i),quantity:'1',cumulative_base:String(i+1)}));
 return {schema:'v36_source_state_v1',product,session_id:session,run_id:sourceRun,
 connection_id:0,state_version:version,source_sequence:version,receiver_time_ms:now,
 source_last_l2_receiver_ms:now,source_raw_sha256:'d'.repeat(64),mode:'LIVE_SOURCE',
 live_feed:true,trading_enabled:false,execution_authorized:false,independent_exchange_attestation:false,
 status:'VALID_OBSERVED_QUOTE',depth:{bids,asks,bid_total_base_10:'20',ask_total_base_10:'10'},
 best_bid:'100',best_ask:'101',best_bid_qty:'2',best_ask_qty:'1',spread:'1',
 microprice:'100.6666666666666666666666667',best_imbalance:'0.3333333333333333333333333333',recent_trades:[]};
}
function frame(g=1,s=sid,v=1){return {schema:'q292_resilient_route_v1',
 mode:'LIVE_READ_ONLY_CANDIDATE',status:'HEALTHY',gateway_run:run,generation:g,
 source_session:s,active_source:'primary',real_orders_enabled:false,execution_authorized:false,
 exchange_attested:false,zero_gap_certified:false,historical_gap_recovered:false,
 states:{'BTC-USD':row('BTC-USD',s,v),'ETH-USD':row('ETH-USD',s,v)}};}
const cursor=(f)=>f.gateway_run+':'+f.generation+':'+f.source_session;
test('exact live route and cursor gate',()=>{
 assert.equal(validateFrame(frame(),now,cursor(frame())).generation,1);
 for(const x of [{...frame(),mode:'SYNTHETIC_ONLY'},{...frame(),execution_authorized:true},
 {...frame(),states:{'BTC-USD':row('BTC-USD')}},{...frame(),states:{...frame().states,'ETH-USD':row('ETH-USD','f'.repeat(32))}}])
 assert.throws(()=>validateFrame(x,now));
 assert.throws(()=>validateFrame(frame(),now+3001));
 assert.throws(()=>validateFrame(frame(),now,'wrong'));
});
test('atomic publication, old callbacks rejected, failover fresh handshake required',()=>{
 const s=new MarketStateStore();let count=0;const off=s.subscribe(()=>count++);
 let t=s.begin();assert.equal(s.accept(t,frame(),now,1,'handshake'),true);
 let f=frame(2,'e'.repeat(32));assert.equal(s.accept(t,f,now,2,'snapshot',cursor(f)),false);
 assert.equal(s.getSnapshot().frame,null);
 assert.equal(s.accept(t,frame(),now,3,'snapshot',cursor(frame())),false);
 t=s.begin();assert.equal(s.accept(t,f,now,4,'handshake'),true);
 assert.equal(s.getSnapshot().frame.states['BTC-USD'].session_id,'e'.repeat(32));
 assert.equal(s.accept(t,frame(),now,5,'snapshot',cursor(frame())),false);
 assert.equal(s.getSnapshot().frame,null);
 t=s.begin();assert.equal(s.accept(t,frame(3,sid),now,6,'handshake'),false);
 off();assert.ok(count>=5);
});
test('duplicate conflict and expiry fail closed',()=>{
 const s=new MarketStateStore();let t=s.begin();
 assert.equal(s.accept(t,frame(),now,1,'handshake'),true);
 const changed=frame();changed.states['BTC-USD'].source_raw_sha256='f'.repeat(64);
 assert.equal(s.accept(t,changed,now,2,'snapshot',cursor(changed)),false);
 t=s.begin();assert.equal(s.accept(t,frame(),now,3,'handshake'),true);
 assert.equal(s.check(now+3001,3004),false);assert.equal(s.getSnapshot().frame,null);
});
test('mutable payload cannot change admitted data',()=>{
 const s=new MarketStateStore(),t=s.begin(),f=frame();
 assert.equal(s.accept(t,f,now,1,'handshake'),true);f.states['BTC-USD'].best_bid='1';
 assert.equal(s.getSnapshot().frame.states['BTC-USD'].best_bid,'100');
 assert.equal(Object.isFrozen(s.getSnapshot().frame.states['BTC-USD']),true);
});
