// Imported projection is structurally checked, not independently authenticated.
const SCALE=10n**18n;
const hex=/^[0-9a-f]{64}$/;
const decimal=/^(?:0|[1-9]\d{0,23})(?:\.\d{1,18})?$/;
export function units(x:unknown):bigint{
 if(typeof x!=='string'||!decimal.test(x))throw Error('DECIMAL');
 const [a,b='']=x.split('.');return BigInt(a)*SCALE+BigInt(b.padEnd(18,'0'));
}
const obj=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v);
const integer=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v);
const require=(ok:unknown,label:string)=>{if(!ok)throw Error(label);};
export type Cell={side:'bid'|'offer';bin:number;quantity:string;value:number};
export type Row={time:number;observed:number;seq:number;bid:string;ask:string;midBin:number;cells:Cell[]};
export type Projection={product:'BTC-USD'|'ETH-USD';pin:string;step:number;low:string;high:string;width:string;bins:number;start:number;end:number;rows:Row[];missing:number[]};
export function decode(x:unknown):Projection{
 if(!obj(x))throw Error('OBJECT');
 require(x.schema==='f4_observed_depth_buckets_v1'&&x.mode==='ARCHIVE_REPLAY'&&x.live_feed===false&&x.execution_authorized===false&&x.zero_gap_certified===false&&x.oos_approved===false,'MODE');
 require(x.product==='BTC-USD'||x.product==='ETH-USD','PRODUCT');
 require(typeof x.source_manifest_sha256==='string'&&hex.test(x.source_manifest_sha256)&&x.epoch===x.source_manifest_sha256+':0'&&x.independent_replay_status==='INDEPENDENT_SOURCE_REPLAY_ONLY'&&integer(x.source_messages)&&x.source_messages>0,'PROVENANCE');
 require(x.definition==='LAST_L2_OBSERVATION_PER_OCCUPIED_RECEIVER_BUCKET'&&x.coverage==='ALL_OBSERVED_L2_LEVELS_INSIDE_REQUESTED_PRICE_WINDOW','DEFINITION');
 const step=x.time_bucket_ms;
 if(!integer(step)||step<100||step>60000)throw Error('TIME_STEP');
 const lo=units(x.price_low),hi=units(x.price_high_exclusive),width=units(x.price_width);
 require(lo>0n&&hi>lo&&width>0n&&lo%width===0n&&hi%width===0n&&(hi-lo)/width<=500n,'PRICE_GRID');
 const bins=Number((hi-lo)/width);
 if(!Array.isArray(x.rows)||!Array.isArray(x.missing_buckets_ms)||x.rows.length+x.missing_buckets_ms.length>2000||x.rows.length+x.missing_buckets_ms.length===0)throw Error('ROW_BOUND');
 const rows:Row[]=[],missing:number[]=[];const times=new Set<number>();let previous=-1,previousSeq=-1,total=0;
 for(const raw of x.rows){
  if(!obj(raw))throw Error('ROW');
  const t=raw.bucket_start_ms,observed=raw.observed_receiver_ms,seq=raw.source_sequence;
  if(!integer(t)||!integer(observed)||!integer(seq)||t<=0||t%step||t<=previous||observed<t||observed-t>=step||seq<=previousSeq||seq<0)throw Error('ROW_ORDER');
  previous=t;previousSeq=seq;times.add(t);
  const bid=units(raw.best_bid),ask=units(raw.best_ask);require(bid>0n&&ask>bid,'QUOTE');
  if(!Array.isArray(raw.cells)||raw.cells.length>2*bins)throw Error('CELL_BOUND');
  total+=raw.cells.length;require(total<=200000,'TOTAL_CELLS');
  const cells:Cell[]=[],seen=new Set<string>();
  for(const c of raw.cells){
   if(!obj(c)||(c.side!=='bid'&&c.side!=='offer'))throw Error('CELL_SIDE');
   const p=units(c.price_low),q=units(c.quantity);require(p>=lo&&p<hi&&(p-lo)%width===0n&&q>0n,'CELL_GRID');
   require(c.side==='bid'?p<=bid:p+width>ask,'CELL_QUOTE');
   const bin=Number((p-lo)/width),key=c.side+':'+bin;require(!seen.has(key),'DUPLICATE_CELL');seen.add(key);
   const value=Number(q)/Number(SCALE);require(Number.isFinite(value)&&value>0,'CELL_VALUE');
   cells.push({side:c.side,bin,quantity:c.quantity as string,value});
  }
  rows.push({time:t,observed,seq,bid:raw.best_bid as string,ask:raw.best_ask as string,midBin:Number(bid+ask-2n*lo)/Number(2n*width),cells});
 }
 let last=-1;
 for(const t of x.missing_buckets_ms){if(!integer(t)||t<=0||t%step||t<=last||times.has(t))throw Error('MISSING_GRID');last=t;times.add(t);missing.push(t);}
 const sorted=[...times].sort((a,b)=>a-b);
 require(sorted.every((t,i)=>i===0||t-sorted[i-1]===step),'INCOMPLETE_GRID');
 return {product:x.product as Projection['product'],pin:x.source_manifest_sha256 as string,step,low:x.price_low as string,high:x.price_high_exclusive as string,width:x.price_width as string,bins,start:sorted[0],end:sorted[sorted.length-1]+step,rows,missing};
}
