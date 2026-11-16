import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FnChart } from '@funnest/charts-react';
import type { Datafeed, Bar, SymbolInfo, BarsResult, Resolution } from '@funnest/charts';
import { validateDepth10 } from './marketStateIntegrity';

const products = ['BTC-USD', 'ETH-USD'] as const;
type Product = typeof products[number];
type BarRecord = {time:number;available_at:number;open:string;high:string;low:string;close:string;volume:string;observed_trade_count:number};
type Feature = {instrument:Product;decision_receive_utc_ms:number;status:string;best_bid?:string;best_ask?:string;best_bid_qty?:string;best_ask_qty?:string;microprice?:string;best_imbalance?:string;ofi_rolling_5m_base?:string;source_age_ms:number|null;eligible_for_oos_or_trading:boolean};
type Trade = {received_at_ms:number;message_seq:number;trade_id:string;price:string;size:string;side_label_source:string;product:Product};
type Catalog = {schema:string;source_sha256:string;projection_sha256:string;source_message_count:number;source_start_ms:number;source_end_ms:number;source_cohort:string;evidence_status:string;live_feed:false;trading_enabled:false;products:{product:Product;candles:number;diagnostics:number;recent_trades:number;observed_trade_updates:number}[]};
type Series<T> = {product:Product;kind:string;rows:T[];next_offset:number|null;source_sha256:string;projection_sha256:string;oos_approved:false;live_feed:false};
type DepthLevel = {price:string;quantity:string;cumulative_base:string};
type L2Depth = {bids:DepthLevel[];asks:DepthLevel[];bid_total_base_10:string;ask_total_base_10:string};
type L2Row = {product:Product;decision_receiver_ms:number;source_age_ms:number|null;source_sequence:number|null;status:string;depth:L2Depth|null};
type L2Response = {product:Product;asof_receiver_ms:number;row:L2Row;levels_each_side:number;source_sha256:string;base_projection_sha256:string;l2_projection_sha256:string;receiver_time_only:true;historical_only:true;live_feed:false;oos_approved:false;trading_enabled:false;definition:string};
type RecoveryEvent = {product:Product;shock_receiver_ms:number;available_at_ms:number;baseline_30s_median_depth_base:string;shock_depth_base:string;recovery_threshold_base:string;recovery_within_30s:boolean;first_recovery_delay_ms:number|null;censored_at_30s:boolean};
type RecoveryResponse = {product:Product;asof_receiver_ms:number;events:RecoveryEvent[];visible_events_count:number;window_ms:number;source_sha256:string;base_projection_sha256:string;l2_projection_sha256:string;receiver_time_only:true;historical_only:true;live_feed:false;oos_approved:false;trading_enabled:false;eligibility:string;definition:string};
async function json<T>(path:string):Promise<T>{
  const r=await fetch(path,{cache:'no-store'});
  const payload:unknown=await r.json();
  if(!r.ok){let message=`HTTP ${r.status}`;if(payload&&typeof payload==='object'&&'error'in payload&&typeof payload.error==='string')message=payload.error;throw new Error(message)}
  return payload as T;
}
function iso(ms:number){return new Date(ms).toISOString().slice(11,19)+' UTC'}
function fmt(s:string|undefined,digits=2){const v=Number(s);return Number.isFinite(v)?v.toLocaleString('en-US',{maximumFractionDigits:digits,minimumFractionDigits:digits}):'Unavailable'}
async function series<T>(catalog:Catalog,product:Product,kind:string,from:number,to:number,asof:number|null):Promise<T[]>{
  const args=new URLSearchParams({product,kind,from:String(from),to:String(to),limit:'400'});
  if(asof!==null)args.set('asof',String(asof));
  const data=await json<Series<T>>(`/api/v30/series?${args}`);
  if(data.product!==product || data.kind!==kind || data.oos_approved!==false || data.live_feed!==false || data.source_sha256!==catalog.source_sha256 || data.projection_sha256!==catalog.projection_sha256)
    throw new Error('Source projection changed. Refresh and re-audit.');
  if(data.next_offset!==null)throw new Error('Result exceeds bounded view; use a smaller range.');
  return data.rows;
}
function feed(catalog:Catalog,product:Product,asof:number|null):Datafeed{
  const symbol:SymbolInfo={symbol:product,name:product+' · RECEIVER-TIME REPLAY',exchange:'Coinbase Advanced (public spot)',session:'24x7',timezone:'UTC',type:'crypto',tickSize:0.01,pricePrecision:2,supportedResolutions:['5']};
  return {
    async resolveSymbol(name:string){if(name!==product)throw new Error('Unregistered symbol');return symbol},
    async searchSymbols(query:string){return product.toLowerCase().includes(query.toLowerCase())?[symbol]:[]},
    async getBars(info:SymbolInfo,resolution:Resolution,request):Promise<BarsResult>{
      if(info.symbol!==product||resolution!=='5')throw new Error('Invalid historical replay resolution');
      const rows=await series<BarRecord>(catalog,product,'candles',(request.countBack??0)>0?catalog.source_start_ms:Math.max(Math.min(request.from,catalog.source_end_ms),catalog.source_start_ms),Math.max(Math.min(request.to,catalog.source_end_ms+1),catalog.source_start_ms+1),asof);
      const bars:Bar[]=rows.map(r=>({time:r.time,open:Number(r.open),high:Number(r.high),low:Number(r.low),close:Number(r.close),volume:Number(r.volume)}));
      return {bars,noMoreData:true};
    },
    subscribeBars(){return ()=>{}},
  };
}

// One analytical clock governs the chart, tape and causal diagnostics.
// The default cutoff is the last archived five-minute receiver-asof diagnostic,
// not the later end of the raw source archive. No future-looking bars are shown.
type Diagnostic = Feature & {l2_update_events_rolling_5m?:number};
function numeric(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n=Number(value); return Number.isFinite(n)?n:null;
}
function compact(value: number|null|undefined,decimals=2):string {
  return value===null||value===undefined||!Number.isFinite(value)?'—':value.toLocaleString('en-US',{minimumFractionDigits:decimals,maximumFractionDigits:decimals});
}
function timeOf(ms:number|null|undefined):string{return ms===null||ms===undefined?'—':new Date(ms).toISOString().slice(11,19)}
function fullTime(ms:number):string{return new Date(ms).toISOString().replace('.000Z','Z')}
function statValue(s:string|null|undefined,dp=2){return s?fmt(s,dp):'—'}
function PanelTitle({eyebrow,title,tag,children}:{eyebrow:string,title:string,tag?:string,children?:React.ReactNode}){
 return <div className="pro-panel-title"><div><div className="pro-eyebrow">{eyebrow}</div><h3>{title}</h3></div><div className="pro-panel-end">{children}{tag?<span className="pro-panel-tag">{tag}</span>:null}</div></div>
}
function Trace({data,kind,active}:{data:Diagnostic[],kind:'ofi'|'micro',active:number|null}){
  const get=(f:Diagnostic):number|null=>{
    if(kind==='ofi')return numeric(f.ofi_rolling_5m_base);
    const bid=numeric(f.best_bid),ask=numeric(f.best_ask),micro=numeric(f.microprice);
    return bid!==null&&ask!==null&&micro!==null&&bid+ask>0?(micro-(bid+ask)/2)/((bid+ask)/2)*10000:null;
  };
  const rows=data.map(f=>({t:f.decision_receive_utc_ms,v:get(f)})).filter((x):x is {t:number;v:number}=>x.v!==null);
  if(!rows.length)return <div className="pro-no-data">No admissible receiver-asof observations.</div>;
  const vals=rows.map(x=>x.v),lowest=Math.min(0,...vals),highest=Math.max(0,...vals);
  const range=Math.max(highest-lowest,Math.abs(highest)*0.08,Math.abs(lowest)*0.08,0.00001);
  const min=lowest-range*.08,max=highest+range*.08,scale=(v:number)=>94-(v-min)/(max-min)*78;
  const xAt=(i:number)=>rows.length===1?260:12+i*(516/(rows.length-1));
  const points=rows.map((r,i)=>`${xAt(i)},${scale(r.v)}`).join(' ');
  const zeroY=scale(0);
  const current=active===null?rows[rows.length-1]:[...rows].reverse().find(r=>r.t<=active);
  const match=rows.findIndex(r=>r.t===current?.t);
  return <div className="pro-trace"><svg viewBox="0 0 540 122" preserveAspectRatio="none" role="img" aria-label={kind==='ofi'?'Rolling five-minute order flow imbalance from archived diagnostics':'Archived microprice deviation from the midpoint in basis points'}>
    {[20,48,76,104].map(y=><line key={y} x1="10" y1={y} x2="530" y2={y} stroke="#223346" strokeDasharray="2 6" strokeWidth="1"/>)}
    <line x1="10" x2="530" y1={zeroY} y2={zeroY} stroke="#5f7489" strokeDasharray="4 5" strokeWidth="1"/>
    <polyline points={points} fill="none" stroke={kind==='ofi'?'#42d3bb':'#93aaff'} strokeWidth="2.3" vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round"/>
    {match>=0?<circle cx={xAt(match)} cy={scale(rows[match].v)} r="4.8" fill={kind==='ofi'?'#42d3bb':'#a1afff'} stroke="#09131d" strokeWidth="2"/>:null}
    <text x="12" y="13" fill="#6f859c" fontSize="11">{compact(highest,kind==='ofi'?3:4)}</text><text x="12" y="119" fill="#6f859c" fontSize="11">{compact(lowest,kind==='ofi'?3:4)}</text>
  </svg><div className="pro-spark-foot"><span>{timeOf(rows[0].t)} UTC</span><span>{kind==='ofi'?'SIGNED BEST-LEVEL FLOW · BASE':'MICROPRICE – MIDPOINT · bps'}</span><span>{timeOf(rows[rows.length-1].t)} UTC</span></div></div>;
}
function StateRow({text,status}:{text:string,status:'pass'|'warn'|'fail'}){return <div className="pro-check-row"><span className={'pro-check-dot '+status}>{status==='pass'?'✓':status==='warn'?'!':'×'}</span><span>{text}</span></div>}

export default function UnifiedTerminal(){
 const [catalog,setCatalog]=useState<Catalog|null>(null);
 const [product,setProduct]=useState<Product>('BTC-USD');
 const [features,setFeatures]=useState<Diagnostic[]>([]);
 const [loadedProduct,setLoadedProduct]=useState<Product|null>(null);
 const [trades,setTrades]=useState<Trade[]>([]);
 const [candles,setCandles]=useState<BarRecord[]>([]);
 const [asofIndex,setAsofIndex]=useState<number|null>(null);
 const [depthRow,setDepthRow]=useState<L2Row|null>(null);
 const [recovery,setRecovery]=useState<RecoveryResponse|null>(null);
 const [depthError,setDepthError]=useState<string|null>(null);
 const [playing,setPlaying]=useState(false);
 const [mode,setMode]=useState<'chart'|'microstructure'|'evidence'>('chart');
 const [error,setError]=useState<string|null>(null);
 const [loading,setLoading]=useState(true);
 useEffect(()=>{
   let active=true;
   json<Catalog>('/api/v30/catalog').then(c=>{
     if(!active)return;
     if(c.trading_enabled!==false||c.live_feed!==false||!(/^[0-9a-f]{64}$/.test(c.source_sha256))||!(/^[0-9a-f]{64}$/.test(c.projection_sha256)))throw new Error('Invalid read-only source evidence contract');
     setCatalog(c);setError(null);
   }).catch(e=>{if(active)setError(String(e))}).finally(()=>{if(active)setLoading(false)});
   return()=>{active=false};
 },[]);
 useEffect(()=>{
   if(!catalog)return;
   let active=true;
   setLoadedProduct(null);setFeatures([]);setTrades([]);setCandles([]);setError(null);setAsOfSafely(null);
   Promise.all([
     series<Diagnostic>(catalog,product,'features',catalog.source_start_ms,catalog.source_end_ms+1,null),
     series<Trade>(catalog,product,'trades',catalog.source_start_ms,catalog.source_end_ms+1,null),
     series<BarRecord>(catalog,product,'candles',catalog.source_start_ms,catalog.source_end_ms+1,null),
   ]).then(([f,t,b])=>{
      if(!active)return;
      if(f.some(x=>x.instrument!==product||x.eligible_for_oos_or_trading!==false)||t.some(x=>x.product!==product)||b.some(x=>x.available_at<x.time+300000))throw new Error('Market source, causal flags or candle availability invalid');
      if(f.some((x,i)=>i>0&&x.decision_receive_utc_ms<=f[i-1].decision_receive_utc_ms))throw new Error('Non-monotonic feature observation');
      setFeatures(f);setTrades(t);setCandles(b);setLoadedProduct(product);
   }).catch(e=>{if(active)setError(String(e))});
   return()=>{active=false};
 },[catalog,product]);
 function setAsOfSafely(i:number|null){setPlaying(false);setAsofIndex(i)}
 // Explicit source isolation: no previous instrument's data can survive a switch.
 const shownFeatures=loadedProduct===product?features:[];
 const shownTrades=loadedProduct===product?trades:[];
 const shownCandles=loadedProduct===product?candles:[];
 useEffect(()=>{
   if(!playing||loadedProduct!==product||!features.length)return;
   const timer=window.setInterval(()=>setAsofIndex(i=>i===null?0:Math.min(i+1,features.length-1)),900);
   return()=>window.clearInterval(timer);
 },[playing,loadedProduct,product,features.length]);
 useEffect(()=>{
   if(playing&&asofIndex!==null&&asofIndex>=shownFeatures.length-1)setPlaying(false);
 },[playing,asofIndex,shownFeatures.length]);
 const activeIndex=asofIndex===null?shownFeatures.length-1:Math.min(asofIndex,shownFeatures.length-1);
 const selectedFeature=activeIndex>=0?shownFeatures[activeIndex]:null;
 const cutoff=selectedFeature?.decision_receive_utc_ms??null;
 // The chart, trade tape and features all share this exact archived receiver-asof cutoff.
 const asof=cutoff;
 const visibleFeatures=useMemo(()=>shownFeatures.filter(f=>asof!==null&&f.decision_receive_utc_ms<=asof),[shownFeatures,asof]);
 const visibleCandles=useMemo(()=>shownCandles.filter(c=>asof!==null&&c.available_at<=asof),[shownCandles,asof]);
 // Keep this explicit expression for the retained v31 causality contract.
 const visibleCandleCount=asof===null?0:shownCandles.filter(c=>c.available_at<=asof).length;
 const activeTrades=useMemo(()=>shownTrades.filter(t=>asof!==null&&t.received_at_ms<=asof).slice(-21).reverse(),[shownTrades,asof]);
 const historicalFeed=useMemo(()=>catalog&&loadedProduct===product&&asof!==null?feed(catalog,product,asof):null,[catalog,product,loadedProduct,asof]);
 // The new full L2 projection is loaded for the SAME historical receiver-asof
 // decision as all existing panels. Late responses are ignored on product/time change.
 useEffect(()=>{
   setDepthRow(null);setRecovery(null);setDepthError(null);
   if(!catalog||loadedProduct!==product||cutoff===null)return;
   let active=true;
   const params=new URLSearchParams({product,asof:String(cutoff)});
   Promise.all([
     json<L2Response>(`/api/v34/depth?${params}`),
     json<RecoveryResponse>(`/api/v34/recovery?${params}`)
   ]).then(([d,r])=>{
     if(!active)return;
     if(d.product!==product||r.product!==product||d.row.product!==product||d.asof_receiver_ms!==cutoff||r.asof_receiver_ms!==cutoff||d.row.decision_receiver_ms!==cutoff
        ||d.source_sha256!==catalog.source_sha256||r.source_sha256!==catalog.source_sha256
        ||d.base_projection_sha256!==catalog.projection_sha256||r.base_projection_sha256!==catalog.projection_sha256
        ||d.l2_projection_sha256!==r.l2_projection_sha256||!(/^[a-f0-9]{64}$/.test(d.l2_projection_sha256))
        ||d.live_feed!==false||r.live_feed!==false||d.trading_enabled!==false||r.trading_enabled!==false
        ||d.oos_approved!==false||r.oos_approved!==false||d.historical_only!==true||r.historical_only!==true
        ||d.levels_each_side!==10||r.eligibility!=='POST_HOC_DESCRIPTIVE_ONLY_NO_ALPHA'
        ||r.events.some(e=>e.product!==product||e.available_at_ms>cutoff||e.available_at_ms<e.shock_receiver_ms+30000))
       throw new Error('L2/recovery source and causal contract mismatch');
     const levels=d.row.depth;
     if(levels&&!validateDepth10(levels))
       throw new Error('Reconstructed depth levels invalid');
     if(levels&&selectedFeature?.status==='DIAGNOSTIC_CAUSAL_L1'
        &&(levels.bids[0].price!==selectedFeature.best_bid||levels.asks[0].price!==selectedFeature.best_ask||levels.bids[0].quantity!==selectedFeature.best_bid_qty||levels.asks[0].quantity!==selectedFeature.best_ask_qty))
       throw new Error('L2/L1 reference divergence');
     setDepthRow(d.row);setRecovery(r);
   }).catch(e=>{if(active)setDepthError(String(e))});
   return()=>{active=false};
 },[catalog,product,loadedProduct,cutoff,selectedFeature]);
 const validDepth=depthRow?.decision_receiver_ms===cutoff&&depthRow?.product===product&&depthRow?.status==='HISTORICAL_L2_TEN_LEVEL'?depthRow.depth:null;
 const validRecovery=recovery?.asof_receiver_ms===cutoff&&recovery?.product===product?recovery:null;
 const recovering=validRecovery?.events.filter(e=>e.recovery_within_30s)||[];
 const recoveryRate=validRecovery&&validRecovery.events.length>0?100*recovering.length/validRecovery.events.length:null;
 const current=selectedFeature?.status==='DIAGNOSTIC_CAUSAL_L1'&&selectedFeature.source_age_ms!==null&&selectedFeature.source_age_ms<=5000?selectedFeature:null;
 const bid=numeric(current?.best_bid),ask=numeric(current?.best_ask),bidQty=numeric(current?.best_bid_qty),askQty=numeric(current?.best_ask_qty);
 const mid=bid!==null&&ask!==null?(bid+ask)/2:null;
 const spread=bid!==null&&ask!==null?ask-bid:null;
 const imbalance=numeric(current?.best_imbalance);
 const micro=numeric(current?.microprice);
 const ofi=numeric(current?.ofi_rolling_5m_base);
 const sampledPrice=activeTrades.length?numeric(activeTrades[0].price):null;
 const buyCount=activeTrades.filter(t=>t.side_label_source==='BUY').length;
 const sellCount=activeTrades.filter(t=>t.side_label_source==='SELL').length;
 const sampleTotal=buyCount+sellCount;
 const bidWidth=bidQty!==null&&askQty!==null&&bidQty+askQty>0?bidQty/(bidQty+askQty)*100:null;
 const askWidth=bidWidth===null?null:100-bidWidth;
 const displayError=useCallback((e:unknown)=>setError(String(e)),[]);
 const onProduct=(p:Product)=>{setPlaying(false);setProduct(p);setAsofIndex(null)};
 const play=()=>{if(loadedProduct!==product||!shownFeatures.length)return;setAsofIndex(i=>i===null||i>=shownFeatures.length-1?0:i);setPlaying(true)};
 const pause=()=>setPlaying(false);
 const step=()=>{if(loadedProduct!==product||!shownFeatures.length)return;setPlaying(false);setAsofIndex(i=>i===null?0:Math.min(i+1,shownFeatures.length-1))};
 const reset=()=>{setPlaying(false);setAsofIndex(null)};
 const hasData=Boolean(catalog&&!error&&loadedProduct===product&&selectedFeature);
 return <div className="pro-terminal">
   <div className="pro-topbar">
     <div className="pro-brand"><div className="pro-brand-mark"><span/><span/><span/><span/></div><div><strong>QRT<span> / </span>RESEARCH</strong><small>MARKET MICROSTRUCTURE LAB</small></div></div>
     <div className="pro-global-nav"><span className="selected">Terminal</span><span>Research Workbench</span><span>Data Integrity</span></div>
     <div className="pro-system"><span className="pro-led"/> <strong>ARCHIVED SOURCE</strong><span className="pro-top-sep"/> <span>NO LIVE FEED</span><span className="pro-top-sep"/><span>NO ORDER ROUTING</span></div>
   </div>
   <div className="pro-workspace-title">
     <div><div className="pro-breadcrumb">WORKSPACE <span>/</span> BTC–ETH <span>/</span> COINBASE PUBLIC SPOT</div><h1>Microstructure Intelligence <span>Terminal</span></h1></div>
     <div className="pro-session-label"><span>DATASET</span><strong>V16 · 1H SINGLE-CONNECTION ARCHIVE</strong><small>{catalog?`RECEIVED ${fullTime(catalog.source_start_ms).slice(0,10)} UTC`:'VERIFYING SOURCE'}</small></div>
   </div>
   {loading?<div className="pro-alert">Validating immutable archived market evidence…</div>:null}
   {error?<div className="pro-alert pro-alert-error" role="alert">INTEGRITY BLOCK · {error}. All market panels withheld until source validation succeeds.</div>:null}
   {catalog&&!error?<>
     <div className="uni-controls pro-command-row">
       <div className="pro-symbols uni-switch" role="group" aria-label="Archived Coinbase instruments">
        {products.map(p=><button type="button" key={p} className={p===product?'active':''} onClick={()=>onProduct(p)}><span className="pro-symbol-mark">{p==='BTC-USD'?'₿':'Ξ'}</span>{p}<small>{p==='BTC-USD'?'BITCOIN':'ETHEREUM'}</small></button>)}
       </div>
       <div className="pro-command-meta"><span className="pro-mode-dot"/> HISTORICAL MARKET REPLAY <span className="pro-inline-sep">/</span> 5 MIN RECEIVER-ASOF GRID</div>
       <div className="uni-timing pro-select-wrap"><label htmlFor="pro-asof">OBSERVATION TIME</label><select id="pro-asof" aria-label="Replay as-of time" value={asofIndex===null?'latest':String(asofIndex)} onChange={e=>setAsOfSafely(e.target.value==='latest'?null:Number(e.target.value))}><option value="latest">Latest validated diagnostic</option>{shownFeatures.map((f,i)=><option value={i} key={f.decision_receive_utc_ms}>{fullTime(f.decision_receive_utc_ms).slice(11,19)} UTC</option>)}</select></div>
     </div>
     <div className="uni-replay-controller pro-replay" role="group" aria-label="Historical replay controls">
       <div className="pro-replay-left"><span className="pro-replay-icon">▸</span><div><b>ARCHIVE REPLAY</b><small>NOT LIVE · 5m observation steps / 900ms</small></div></div>
       <div className="pro-transport"><button type="button" aria-label="Play historical replay" disabled={playing||!hasData} onClick={play}>▶ <span>Play</span></button><button type="button" aria-label="Pause historical replay" disabled={!playing} onClick={pause}>Ⅱ <span>Pause</span></button><button type="button" aria-label="Step historical replay" disabled={!hasData} onClick={step}>↦ <span>Step</span></button><button type="button" aria-label="Reset historical replay" disabled={!hasData||(asofIndex===null&&!playing)} onClick={reset}>↺ <span>Latest</span></button></div>
       <div className="pro-progress"><div className="pro-progress-rail"><div className="pro-progress-fill" style={{width:shownFeatures.length?`${((activeIndex+1)/shownFeatures.length)*100}%`:'0%'}}/></div><span>{Math.max(0,activeIndex+1)} / {shownFeatures.length} SLICES</span></div>
       <div className="pro-utc">{cutoff!==null?fullTime(cutoff):'NO SNAPSHOT'}</div>
     </div>
     <div className="uni-stats pro-metrics" aria-label="Receiver-asof quote statistics">
       <article><div className="pro-metric-top">MIDPRICE <span>L1 / USD</span></div><strong>{compact(mid,2)}</strong><small>{current?'RECEIVER-ASOF QUOTE':'INVALID OR STALE L1'}</small></article>
       <article><div className="pro-metric-top">BEST BID <span>USD</span></div><strong className="pro-positive">{compact(bid,2)}</strong><small>{bidQty!==null?`${compact(bidQty,6)} BTC/ETH BASE`:'NO VALID SIZE'}</small></article>
       <article><div className="pro-metric-top">BEST ASK <span>USD</span></div><strong className="pro-negative">{compact(ask,2)}</strong><small>{askQty!==null?`${compact(askQty,6)} BTC/ETH BASE`:'NO VALID SIZE'}</small></article>
       <article><div className="pro-metric-top">SPREAD <span>USD</span></div><strong>{compact(spread,2)}</strong><small>{mid&&spread!==null?`${compact(spread/mid*10000,2)} BASIS POINTS`:'NOT MEASURABLE'}</small></article>
       <article><div className="pro-metric-top">MICROPRICE <span>USD</span></div><strong className="pro-blue">{compact(micro,3)}</strong><small>CAUSAL L1 · NOT FORECAST</small></article>
       <article><div className="pro-metric-top">QUOTE AGE <span>MS</span></div><strong className={current?'pro-positive':'pro-negative'}>{current?.source_age_ms??'—'}</strong><small>{current?'RECEIVER-TIME DIAGNOSTIC':'5 SECOND SAFETY GATE'}</small></article>
     </div>
     <div className="pro-main-grid">
       <div className="pro-main-left">
         <article className="pro-panel pro-chart-panel uni-chart-panel">
           <PanelTitle eyebrow="01  /  MARKET STRUCTURE" title="Observed trade-price candles" tag="5M · RECEIVER TIME"><span className="pro-candle-total">{visibleCandleCount} completed intervals at cutoff</span></PanelTitle>
           <div className="pro-chart-subhead"><span className="pro-ref-tag">COINBASE PUBLIC SPOT</span><span>{product}</span><span className="pro-sub-note">OBSERVED TRADE UPDATES · NOT OFFICIAL OHLCV</span></div>
           <div className="uni-chart pro-funnest-chart">{historicalFeed&&visibleCandleCount>0?<FnChart key={`${product}-${asof??'blocked'}`} datafeed={historicalFeed} symbol={product} resolution="5" colorScheme="dark" locale="en" storageKey={null} drawingToolbar={false} onReady={()=>{}} style={{height:'100%'}}/>:<div className="uni-empty-history" role="status">{loadedProduct===product?'No completed 5-minute receiver-time candles at this observation. Advance to a later diagnostic grid point.':'Validating this instrument’s original market source…'}</div>}</div>
           <div className="pro-chart-footer"><span><b>OBSERVATION</b> {timeOf(cutoff)} UTC</span><span><b>CANDLES</b> {visibleCandles.length} / {shownCandles.length}</span><span><b>SAMPLED TRADE</b> {compact(sampledPrice,2)}</span><span className="pro-limitation">NO SYNTHETIC BARS · NO FUTURE DATA</span></div>
         </article>
         <div className="pro-analytics-row">
           <article className="pro-panel pro-analytics-card"><PanelTitle eyebrow="02  /  ORDER FLOW" title="OFI · five-minute rolling" tag="BASE UNITS"/><div className="pro-analytics-number"><strong className={ofi!==null&&ofi>=0?'pro-positive':'pro-negative'}>{compact(ofi,4)}</strong><small>BEST-LEVEL SIGNED DEPTH CHANGE</small></div><Trace data={visibleFeatures} kind="ofi" active={cutoff}/></article>
           <article className="pro-panel pro-analytics-card"><PanelTitle eyebrow="03  /  PRICE DISCOVERY" title="Microprice deviation" tag="BASIS POINTS"/><div className="pro-analytics-number"><strong className="pro-blue">{mid!==null&&micro!==null?compact((micro-mid)/mid*10000,4):'—'}</strong><small>MICROPRICE RELATIVE TO L1 MIDPOINT</small></div><Trace data={visibleFeatures} kind="micro" active={cutoff}/></article>
         </div>
       </div>
       <div className="pro-main-middle">
         <article className="pro-panel pro-book-panel"><PanelTitle eyebrow="04 / ORDER-BOOK DEPTH" title="Coinbase L2 · top 10" tag="AUDITED HISTORICAL"/>
           <div className="pro-book-legend"><span>PRICE (USD)</span><span>SIZE (BASE) / CUMULATIVE</span></div>
           {validDepth?<div className="pro-l2-ladder" aria-label="Historical ten-level L2 order book">
             <div className="pro-l2-side">{[...validDepth.asks].reverse().map((l,i)=><div className="pro-l2-level ask" key={l.price}><span>{compact(numeric(l.price),2)}</span><span>{compact(numeric(l.quantity),5)}</span><span>{compact(numeric(l.cumulative_base),4)}</span><div className="pro-l2-fill" style={{width:`${Math.min(100,100*Number(l.cumulative_base)/Math.max(0.0000001,Number(validDepth.ask_total_base_10)))}%`}}/></div>)}</div>
             <div className="pro-book-spread">L1 SPREAD <b>{compact(spread,2)} USD</b> · RECEIVER-ASOF</div>
             <div className="pro-l2-side">{validDepth.bids.map(l=><div className="pro-l2-level bid" key={l.price}><span>{compact(numeric(l.price),2)}</span><span>{compact(numeric(l.quantity),5)}</span><span>{compact(numeric(l.cumulative_base),4)}</span><div className="pro-l2-fill" style={{width:`${Math.min(100,100*Number(l.cumulative_base)/Math.max(0.0000001,Number(validDepth.bid_total_base_10)))}%`}}/></div>)}</div>
             <div className="pro-l2-totals"><span>BID 10L <strong>{statValue(validDepth.bid_total_base_10,5)}</strong></span><span>ASK 10L <strong>{statValue(validDepth.ask_total_base_10,5)}</strong></span></div>
           </div>:<div className="pro-no-data" role="status">{depthError?`L2 FAIL-CLOSED: ${depthError}`:depthRow?'No admissible 10-level quote at selected decision':'Loading hash-pinned 10-level historical L2 quote…'}</div>}
           <div className="pro-limit-message">Replayed full L2 deltas and snapshots. Displayed quantities are not guaranteed executable depth or queue priority. No exchange-clock calibration.</div>
         </article>
         <article className="pro-panel pro-recovery-panel"><PanelTitle eyebrow="05 / RECEIVER-EVENT RESEARCH" title="Liquidity replenishment" tag="POST-HOC ONLY"/>
           <div className="pro-recovery-state"><span className="pro-recovery-icon">↻</span><div><strong>{validRecovery?`${validRecovery.visible_events_count} qualified historical shocks`:'Evaluating archived recovery evidence'}</strong><small>Past 30s median · 50% drop · 80% within 30s · receiver-second sampling</small></div></div>
           {validRecovery&&validRecovery.events.length?<><div className="pro-l2-recovery-stat"><strong>{compact(recoveryRate,1)}%</strong><span>recovered in 30 seconds among last {validRecovery.events.length} displayed shock events</span></div><div className="pro-recovery-events">{validRecovery.events.slice(-5).reverse().map(e=><div key={e.shock_receiver_ms}><span>{timeOf(e.shock_receiver_ms)}</span><strong className={e.recovery_within_30s?'pro-positive':'pro-negative'}>{e.first_recovery_delay_ms===null?'CENSORED':(e.first_recovery_delay_ms/1000).toFixed(0)+'s'}</strong><small>{compact(numeric(e.shock_depth_base),3)} / {compact(numeric(e.baseline_30s_median_depth_base),3)} BASE</small></div>)}</div></>:<div className="pro-no-data">{depthError?'Recovery evidence blocked with L2 source':validRecovery?'No qualified shock completed by this observation.':'Awaiting independently pinned post-hoc evidence…'}</div>}
           <div className="pro-limit-message">Future recovery outcomes are withheld until the full 30s window completes. Descriptive cohort only. Not a forecast, execution signal, independent OOS or alpha.</div>
         </article>
       </div>
       <div className="pro-main-right">
         <article className="pro-panel pro-tape-panel uni-tape"><PanelTitle eyebrow="06  /  EXECUTIONS" title="Recorded trade tape" tag="SAMPLED"/><div className="pro-tape-sub"><span>RECEIVED UTC</span><span>{activeTrades.length} ROWS</span></div>
           <div className="uni-tape-scroller pro-trades-scroll"><table><thead><tr><th>TIME</th><th>LABEL*</th><th>PRICE</th><th>SIZE</th></tr></thead><tbody>{activeTrades.map(t=><tr key={`${t.trade_id}-${t.message_seq}`}><td>{timeOf(t.received_at_ms)}</td><td className={t.side_label_source==='BUY'?'uni-buy':'uni-sell'}>{t.side_label_source}</td><td>{statValue(t.price)}</td><td>{statValue(t.size,5)}</td></tr>)}</tbody></table>{!activeTrades.length?<div className="pro-no-data">No sampled archived trade at this cutoff.</div>:null}</div>
           <div className="pro-tape-distribution"><div className="pro-book-meter-title"><span>FEED SIDE FIELD, SAMPLED ROWS</span><b>{sampleTotal}</b></div><div className="pro-queue"><span className="pro-bid-volume" style={{width:`${sampleTotal?100*buyCount/sampleTotal:0}%`}}/><span className="pro-ask-volume" style={{width:`${sampleTotal?100*sellCount/sampleTotal:0}%`}}/></div><div className="pro-queue-label"><span>BUY-LABEL {buyCount}</span><span>SELL-LABEL {sellCount}</span></div></div>
           <p className="pro-tape-warning">* Exchange-reported side field is preserved verbatim, not treated as verified aggressor direction. Bounded per-bucket sample, not exhaustive executed volume.</p>
         </article>
       </div>
     </div>
     <div className="uni-tabs pro-section-selector" role="tablist" aria-label="Unified terminal workspace"><button role="tab" aria-selected={mode==='chart'} className={mode==='chart'?'active':''} onClick={()=>setMode('chart')}>OVERVIEW</button><button role="tab" aria-selected={mode==='microstructure'} className={mode==='microstructure'?'active':''} onClick={()=>setMode('microstructure')}>DIAGNOSTIC HISTORY</button><button role="tab" aria-selected={mode==='evidence'} className={mode==='evidence'?'active':''} onClick={()=>setMode('evidence')}>PROVENANCE & AUDIT</button></div>
     {mode==='chart'?<div className="pro-bottom-grid">
       <article className="pro-panel pro-quality"><PanelTitle eyebrow="07  /  SOURCE RELIABILITY" title="Data quality & admission" tag="REPLAY-ONLY"/>
         <StateRow status="pass" text="Dual internal source replays agree"/><StateRow status="pass" text="Receiver-asof L1 features from frozen reference"/>
         <StateRow status="warn" text="Exchange clock not independently calibrated"/><StateRow status="warn" text="24-hour continuous capture not authenticated"/>
         <StateRow status="fail" text="Original Feature/Label PIT & blind OOS unavailable"/>
         <div className="pro-quality-foot"><span>SOURCE MESSAGES</span><strong>{catalog.source_message_count.toLocaleString('en-US')}</strong><span>COHORT</span><strong>{catalog.source_cohort.replaceAll('_',' ')}</strong></div>
       </article>
       <article className="pro-panel pro-research"><PanelTitle eyebrow="08  /  HYPOTHESIS REVIEW" title="Research results" tag="NOT OOS-APPROVED"/>
         <div className="pro-research-item"><span className="pro-research-symbol">OFI</span><div><strong>Order-flow imbalance</strong><small>Receiver-asof signal diagnostics available</small></div><b>NO ALPHA CLAIM</b></div>
         <div className="pro-research-item"><span className="pro-research-symbol">μP</span><div><strong>Microprice displacement</strong><small>Observed L1 weighted quote, not forward return</small></div><b>NO ALPHA CLAIM</b></div>
         <div className="pro-research-item"><span className="pro-research-symbol">LR</span><div><strong>Liquidity recovery</strong><small>Event-time replay and cost-aware labels missing</small></div><b>BLOCKED</b></div>
         <div className="pro-no-alpha">NO CONFIRMED EXECUTABLE ALPHA <span>·</span> 0 LIVE ORDERS</div>
       </article>
       <article className="pro-panel pro-evidence-compact"><PanelTitle eyebrow="09  /  LINEAGE" title="Evidence fingerprint" tag="SHA-256"/><div className="pro-hash-entry"><span>ORIGINAL SOURCE MANIFEST</span><code>{catalog.source_sha256}</code></div><div className="pro-hash-entry"><span>RECEIVER-ASOF PROJECTION</span><code>{catalog.projection_sha256}</code></div><p>Both hashes bind this view to the same audited historical cohort. They do not certify exchange-clock or trading validity.</p></article>
     </div>:null}
     {mode==='microstructure'?<article className="pro-panel pro-detail-panel"><PanelTitle eyebrow="RECEIVER-ASOF RESEARCH TABLE" title="Every five-minute causal diagnostic" tag={`${visibleFeatures.length} OBSERVATIONS`}/><div className="pro-detail-scroll"><table><thead><tr><th>DECISION UTC</th><th>BID</th><th>ASK</th><th>BID QTY</th><th>ASK QTY</th><th>IMBALANCE</th><th>OFI 5M</th><th>MICROPRICE</th><th>AGE MS</th></tr></thead><tbody>{visibleFeatures.map(f=><tr key={f.decision_receive_utc_ms}><td>{fullTime(f.decision_receive_utc_ms).slice(11,19)}</td><td>{statValue(f.best_bid)}</td><td>{statValue(f.best_ask)}</td><td>{statValue(f.best_bid_qty,5)}</td><td>{statValue(f.best_ask_qty,5)}</td><td>{statValue(f.best_imbalance,4)}</td><td>{statValue(f.ofi_rolling_5m_base,4)}</td><td>{statValue(f.microprice,3)}</td><td>{f.source_age_ms??'—'}</td></tr>)}</tbody></table></div><p>These historical observations are not independent samples for statistical inference and have no validated future return labels.</p></article>:null}
     {mode==='evidence'?<div className="pro-bottom-grid"><article className="pro-panel pro-evidence-compact"><PanelTitle eyebrow="FULL AUDIT RECORD" title="Original source identity"/><dl><dt>Venue</dt><dd>Coinbase Advanced public spot</dd><dt>Products</dt><dd>BTC-USD · ETH-USD</dd><dt>Cohort</dt><dd>{catalog.source_cohort}</dd><dt>Message count</dt><dd>{catalog.source_message_count.toLocaleString('en-US')}</dd><dt>First observed</dt><dd>{fullTime(catalog.source_start_ms)}</dd><dt>Archive end</dt><dd>{fullTime(catalog.source_end_ms)}</dd></dl></article><article className="pro-panel pro-quality"><PanelTitle eyebrow="VALIDATION CONDITIONS" title="Research admission gates"/><StateRow status="pass" text="Archived market source software double-replayed"/><StateRow status="pass" text="Causal L1 grid crosschecked with frozen reports"/><StateRow status="warn" text="Host clock independence unverified"/><StateRow status="fail" text="Untouched blind OOS unavailable"/><StateRow status="fail" text="Execution and PnL not validated"/></article><article className="pro-panel pro-evidence-compact"><PanelTitle eyebrow="IMMUTABLE HASHES" title="Source and projection"/><div className="pro-hash-entry"><span>MANIFEST SHA-256</span><code>{catalog.source_sha256}</code></div><div className="pro-hash-entry"><span>PROJECTION SHA-256</span><code>{catalog.projection_sha256}</code></div><div className="pro-no-alpha">NO CONFIRMED EXECUTABLE ALPHA</div></article></div>:null}
     <div className="pro-footer">HISTORICAL COINBASE SPOT · RECEIVER CLOCK ONLY · NOT A VENUE-ATTESTED LIVE MARKET · ORIGINAL PIT NOT VERIFIED <span>READ-ONLY RESEARCH / NO ORDER PATH</span></div>
   </>:<div className="pro-empty">No verified market evidence can be displayed.</div>}
 </div>;
}
