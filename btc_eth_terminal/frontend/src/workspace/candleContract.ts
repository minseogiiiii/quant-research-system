import type {Quote} from '../datahub/resilientStore.js';
import {validateCompletedCandles} from '../v36_1_completed_candle_feed.js';
import type {CompletedCandle} from '../v36_1_completed_candle_feed.js';
export type CandleBundle={rows:CompletedCandle[];asof:number;key:string};
export function admitCandleBundle(q:Quote):CandleBundle|null{
 const x=q.completed_candles;
 if(!x||typeof x!=='object'||Array.isArray(x))return null;
 const b=x as Record<string,unknown>;
 if(b.schema!=='f3_source_completed_candles_v1'||b.mode!=='LIVE_SOURCE'||q.mode!=='LIVE_SOURCE'||q.live_feed!==true||
 b.incomplete_candle_never_published!==true||b.independent_candle_replay_verified!==false||b.execution_authorized!==false||
 b.asof_receiver_ms!==q.receiver_time_ms||!['product','run_id','session_id','connection_id','state_version','source_sequence','source_raw_sha256'].every(k=>b[k]===q[k])||
 !validateCompletedCandles(b.rows,q.receiver_time_ms))return null;
 return {rows:b.rows,asof:q.receiver_time_ms,key:q.run_id+':'+q.session_id+':'+q.connection_id+':'+q.product};
}
const fields=['time','available_at','open','high','low','close','volume','observed_trade_count'] as const;
export function assertCandleAdvance(prior:CandleBundle,next:CandleBundle):void{
 if(prior.key!==next.key||next.asof<prior.asof)throw Error('CANDLE_EPOCH');
 if(!prior.rows.length)return;
 const tail=prior.rows[prior.rows.length-1],at=next.rows.findIndex(c=>c.time===tail.time);
 if(at<0)throw Error('CANDLE_HISTORY_DROPPED');
 const old=new Map(prior.rows.map(c=>[c.time,c]));
 for(const c of next.rows.slice(0,at+1)){
  const p=old.get(c.time);if(!p||!fields.every(k=>p[k]===c[k]))throw Error('CANDLE_HISTORY_REWRITTEN');
 }
 // Require all prior rows in the retained time interval, not just the tail.
 const expected=prior.rows.filter(c=>c.time>=next.rows[0].time);
 if(expected.length!==at+1||expected.some((c,i)=>c.time!==next.rows[i].time))throw Error('CANDLE_HISTORY_HOLE');
}
