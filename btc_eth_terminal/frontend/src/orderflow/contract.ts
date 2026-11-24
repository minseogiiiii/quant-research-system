import {units} from '../heatmap/contract.js';
const obj=(x:unknown):x is Record<string,unknown>=>!!x&&typeof x==='object'&&!Array.isArray(x);
const int=(x:unknown):x is number=>typeof x==='number'&&Number.isSafeInteger(x);
const need=(v:unknown,reason:string)=>{if(!v)throw Error(reason);};
function signed(x:unknown):bigint{
 if(typeof x!=='string')throw Error('SIGNED');
 return x.startsWith('-')?-units(x.slice(1)):units(x);
}
export type Cell={price:string;buy:string;sell:string;trades:number};
export type Row={time:number;buy:string;sell:string;delta:string;cvd:string;trades:number;cells:Cell[]};
export type Flow={product:'BTC-USD'|'ETH-USD';pin:string;start:number;end:number;step:number;priceWidth:string;rows:Row[];empty:number[];count:number};
export function decodeFlow(x:unknown):Flow{
 if(!obj(x))throw Error('OBJECT');
 need(x.schema==='f4_observed_order_flow_v1'&&x.mode==='ARCHIVE_REPLAY'&&x.live_feed===false&&x.execution_authorized===false&&x.zero_gap_certified===false&&x.oos_approved===false,'MODE');
 need(x.product==='BTC-USD'||x.product==='ETH-USD','PRODUCT');
 need(typeof x.source_manifest_sha256==='string'&&/^[0-9a-f]{64}$/.test(x.source_manifest_sha256)&&x.epoch===x.source_manifest_sha256+':0'&&int(x.source_messages)&&x.source_messages>0&&x.independent_replay_status==='INDEPENDENT_SOURCE_REPLAY_ONLY','PROVENANCE');
 need(x.time_basis==='RECEIVER_MS'&&x.side_basis==='INVERT_COINBASE_MAKER_SIDE'&&x.cvd_definition==='CUMULATIVE_OBSERVED_SIGNED_BASE_VOLUME_SINCE_REQUEST_START','DEFINITION');
 const start=x.start_ms,end=x.end_ms,step=x.bucket_ms,first=x.source_first_receiver_ms,last=x.source_last_receiver_ms;
 if(!int(start)||!int(end)||!int(step)||!int(first)||!int(last)||start<=0||end<=start||step<100||step>60000||start%step||end%step||(end-start)/step>2000||first<=0||last<first)throw Error('TIME_GRID');
 const width=units(x.price_width);need(width>0n,'PRICE_GRID');
 if(!Array.isArray(x.rows)||!Array.isArray(x.buckets_without_observed_trades_ms)||x.rows.length+x.buckets_without_observed_trades_ms.length!==(end-start)/step)throw Error('GRID_SIZE');
 const rows:Row[]=[],empty:number[]=[],times=new Set<number>();
 let prior=-1,cvd=0n,count=0,totalCells=0;
 for(const raw of x.rows){
  if(!obj(raw))throw Error('ROW');
  const t=raw.bucket_start_ms,a=raw.first_trade_receiver_ms,b=raw.last_trade_receiver_ms,n=raw.trades;
  if(!int(t)||t<start||t>=end||t%step||t<=prior||!int(a)||!int(b)||a<t||b<a||b>=t+step||a<first||b>last||!int(n)||n<=0)throw Error('ROW_ORDER');
  prior=t;times.add(t);count+=n;need(count<=500000,'TRADE_BOUND');
  const buy=units(raw.buy_base),sell=units(raw.sell_base),delta=signed(raw.delta_base);cvd+=delta;
  need(buy+sell>0n&&delta===buy-sell&&signed(raw.observed_cvd_base)===cvd,'CVD_RECONCILIATION');
  if(!Array.isArray(raw.cells)||raw.cells.length===0)throw Error('CELLS');
  totalCells+=raw.cells.length;need(totalCells<=100000,'CELL_BOUND');
  const cells:Cell[]=[];let bp=0n,sp=0n,np=0,previous=-1n;
  for(const c of raw.cells){
   if(!obj(c)||!int(c.trades)||c.trades<=0)throw Error('CELL');
   const p=units(c.price_low),bv=units(c.buy_base),sv=units(c.sell_base);
   need(p>=0n&&p%width===0n&&p>previous&&bv+sv>0n,'CELL_GRID');
   previous=p;bp+=bv;sp+=sv;np+=c.trades;need(np<=n,'CELL_COUNT');
   cells.push({price:c.price_low as string,buy:c.buy_base as string,sell:c.sell_base as string,trades:c.trades});
  }
  need(bp===buy&&sp===sell&&np===n,'FOOTPRINT_RECONCILIATION');
  rows.push({time:t,buy:raw.buy_base as string,sell:raw.sell_base as string,delta:raw.delta_base as string,cvd:raw.observed_cvd_base as string,trades:n,cells});
 }
 prior=-1;
 for(const t of x.buckets_without_observed_trades_ms){if(!int(t)||t<start||t>=end||t%step||t<=prior||times.has(t))throw Error('EMPTY_GRID');prior=t;times.add(t);empty.push(t);}
 need(count===x.unique_window_update_trades&&times.size===(end-start)/step,'TOTAL_RECONCILIATION');
 return {product:x.product as Flow['product'],pin:x.source_manifest_sha256 as string,start,end,step,priceWidth:x.price_width as string,rows,empty,count};
}
