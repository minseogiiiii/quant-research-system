import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FnChart } from '@funnest/charts-react';
import type { Datafeed, Bar, SymbolInfo, BarsResult, Resolution } from '@funnest/charts';

const products = ['BTC-USD', 'ETH-USD'] as const;
type Product = typeof products[number];
type BarRecord = {time:number;available_at:number;open:string;high:string;low:string;close:string;volume:string;observed_trade_count:number};
type Feature = {instrument:Product;decision_receive_utc_ms:number;status:string;best_bid?:string;best_ask?:string;best_bid_qty?:string;best_ask_qty?:string;microprice?:string;best_imbalance?:string;ofi_rolling_5m_base?:string;source_age_ms:number|null;eligible_for_oos_or_trading:boolean};
type Trade = {received_at_ms:number;message_seq:number;trade_id:string;price:string;size:string;side_label_source:string;product:Product};
type Catalog = {schema:string;source_sha256:string;projection_sha256:string;source_message_count:number;source_start_ms:number;source_end_ms:number;source_cohort:string;evidence_status:string;live_feed:false;trading_enabled:false;products:{product:Product;candles:number;diagnostics:number;recent_trades:number;observed_trade_updates:number}[]};
type Series<T> = {product:Product;kind:string;rows:T[];next_offset:number|null;source_sha256:string;projection_sha256:string;oos_approved:false;live_feed:false};

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

export default function UnifiedTerminal(){
  const [catalog,setCatalog]=useState<Catalog|null>(null);
  const [product,setProduct]=useState<Product>('BTC-USD');
  const [features,setFeatures]=useState<Feature[]>([]);
  const [loadedProduct,setLoadedProduct]=useState<Product|null>(null);
  const [trades,setTrades]=useState<Trade[]>([]);
  const [candles,setCandles]=useState<BarRecord[]>([]);
  const [asofIndex,setAsofIndex]=useState<number|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [loading,setLoading]=useState(true);
  const [mode,setMode]=useState<'chart'|'microstructure'|'evidence'>('chart');
  useEffect(()=>{let active=true;json<Catalog>('/api/v30/catalog').then(c=>{if(active){if(c.trading_enabled!==false||c.live_feed!==false||c.source_sha256.length!==64||c.projection_sha256.length!==64)throw new Error('Invalid read-only evidence contract');setCatalog(c);setError(null)}}).catch((e:unknown)=>{if(active)setError(String(e))}).finally(()=>{if(active)setLoading(false)});return ()=>{active=false}},[]);
  useEffect(()=>{if(!catalog)return;let active=true;setLoadedProduct(null);setFeatures([]);setTrades([]);setCandles([]);setError(null);setAsofIndex(null);
    Promise.all([
      series<Feature>(catalog,product,'features',catalog.source_start_ms,catalog.source_end_ms+1,null),
      series<Trade>(catalog,product,'trades',catalog.source_start_ms,catalog.source_end_ms+1,null),
      series<BarRecord>(catalog,product,'candles',catalog.source_start_ms,catalog.source_end_ms+1,null),
    ]).then(([f,t,b])=>{if(active){if(f.some(x=>x.instrument!==product)||t.some(x=>x.product!==product)||b.some(x=>x.available_at<x.time+300000))throw new Error('Cross-product or candle availability blocked');setFeatures(f);setTrades(t);setCandles(b);setLoadedProduct(product)}}).catch((e:unknown)=>{if(active)setError(String(e))});return()=>{active=false};
  },[catalog,product]);
  // Never display a previous instrument's features while the newly selected one loads.
  const shownFeatures=loadedProduct===product?features:[];
  const shownTrades=loadedProduct===product?trades:[];
  const selectedFeature=shownFeatures.length?shownFeatures[asofIndex??shownFeatures.length-1]:null;
  const cutoff=selectedFeature?.decision_receive_utc_ms??null;
  const activeTrades=useMemo(()=>shownTrades.filter(t=>cutoff===null||t.received_at_ms<=cutoff).slice(-32).reverse(),[shownTrades,cutoff]);
  const asof=asofIndex===null?null:cutoff;
  const historicalFeed=useMemo(()=>catalog&&loadedProduct===product?feed(catalog,product,asof):null,[catalog,product,loadedProduct,asof]);
  const displayError=useCallback((e:unknown)=>setError(String(e)),[]);
  const current=selectedFeature?.status==='DIAGNOSTIC_CAUSAL_L1'?selectedFeature:null;
  const spread=current?.best_bid&&current?.best_ask?Number(current.best_ask)-Number(current.best_bid):null;
  const shownCandles=loadedProduct===product?candles:[];
  const visibleCandleCount=asof===null?shownCandles.length:shownCandles.filter(c=>c.available_at<=asof).length;
  const onProduct=(p:Product)=>{setProduct(p);setAsofIndex(null)};
  return <section className="uni">
    <div className="uni-heading"><div><p className="uni-kicker">INTEGRATED RESEARCH / AUDITED ARCHIVE</p><h2>Market microstructure workstation</h2><p>Coinbase public spot · Real archived observations · Single source lineage</p></div>
      <div className="uni-top-flags"><span className="uni-live-denied">● HISTORICAL REPLAY</span><span className="uni-warning">NO OOS · NO ORDERS</span></div></div>
    {loading?<p className="uni-note">Verifying immutable projection and source manifest…</p>:null}
    {error?<div className="uni-alert" role="alert">INTEGRITY BLOCK: {error}. Market display withheld. Reload only after source audit.</div>:null}
    {catalog&&!error?<>
      <div className="uni-controls"><div className="uni-switch">{products.map(p=><button type="button" key={p} className={p===product?'active':''} onClick={()=>onProduct(p)}>{p}</button>)}</div>
      <div className="uni-timing"><span>REPLAY AS-OF</span><select aria-label="Replay as-of time" value={asofIndex===null?'latest':String(asofIndex)} onChange={e=>setAsofIndex(e.target.value==='latest'?null:Number(e.target.value))}><option value="latest">Full history / latest diagnostic</option>{shownFeatures.map((f,i)=><option value={i} key={f.decision_receive_utc_ms}>{iso(f.decision_receive_utc_ms)}</option>)}</select></div></div>
      <div className="uni-stats">
        <article><span>BEST BID (L1)</span><strong>{fmt(current?.best_bid)}</strong><small>USD · archived quote</small></article>
        <article><span>BEST ASK (L1)</span><strong>{fmt(current?.best_ask)}</strong><small>USD · archived quote</small></article>
        <article><span>SPREAD</span><strong>{spread!==null?spread.toFixed(2):'N/A'}</strong><small>USD · receiver as-of</small></article>
        <article><span>MICROPRICE</span><strong>{fmt(current?.microprice,3)}</strong><small>Exploratory · not alpha</small></article>
        <article><span>SOURCE AGE</span><strong>{current?.source_age_ms??'N/A'}</strong><small>ms · at diagnostic grid</small></article>
      </div>
      <div className="uni-tabs" role="tablist" aria-label="Unified terminal workspace">{(['chart','microstructure','evidence']as const).map(tab=><button key={tab} role="tab" aria-selected={mode===tab} className={mode===tab?'active':''} onClick={()=>setMode(tab)}>{({chart:'Chart & trade flow',microstructure:'Microstructure',evidence:'Data evidence'})[tab]}</button>)}</div>
      {mode==='chart'?<div className="uni-chart-layout"><article className="uni-chart-panel"><div className="uni-panel-caption"><div><b>Observed trade-price aggregation</b><small>5m receiver-time OHLC · NOT official venue candles</small></div><span>{visibleCandleCount} completed intervals at cutoff</span></div><div className="uni-chart">{historicalFeed&&visibleCandleCount>0?<FnChart key={`${product}-${asof??'all'}`} datafeed={historicalFeed} symbol={product} resolution="5" colorScheme="dark" locale="en" storageKey={null} drawingToolbar={false} onReady={()=>{}} style={{height:'100%'}}/>:<div className="uni-empty-history" role="status">{loadedProduct===product? 'No completed 5-minute receiver-time candles are available at this replay cutoff. Move to a later observation to view completed intervals.' : 'Validating this product’s archived market data…'}</div>}</div></article>
        <article className="uni-tape"><div className="uni-panel-caption"><div><b>Archived trade tape</b><small>Feed-reported side retained unchanged</small></div><span>{activeTrades.length} visible</span></div><div className="uni-tape-scroller"><table><thead><tr><th>Receive UTC</th><th>Side*</th><th>Price</th><th>Size</th></tr></thead><tbody>{activeTrades.map(t=><tr key={t.trade_id+'-'+t.message_seq}><td>{iso(t.received_at_ms)}</td><td className={t.side_label_source==='BUY'?'uni-buy':'uni-sell'}>{t.side_label_source}</td><td>{fmt(t.price,2)}</td><td>{fmt(t.size,5)}</td></tr>)}</tbody></table>{!activeTrades.length?<p className="uni-note">No sampled archived trades at this replay cutoff.</p>:null}</div><p className="uni-note">* Side is source metadata, not an assumed aggressor or fill. Only a bounded trade sample is displayed.</p></article></div>:null}
      {mode==='microstructure'?<div className="uni-two-panel"><article className="uni-flat-panel"><div className="uni-panel-caption"><b>Causal diagnostic observations</b><span>Every 5 minutes</span></div><table><thead><tr><th>UTC decision</th><th>Imbalance</th><th>OFI (base)</th><th>Microprice</th><th>Age</th></tr></thead><tbody>{shownFeatures.filter(f=>asof===null||f.decision_receive_utc_ms<=asof).map(f=><tr key={f.decision_receive_utc_ms}><td>{iso(f.decision_receive_utc_ms)}</td><td>{fmt(f.best_imbalance,4)}</td><td>{fmt(f.ofi_rolling_5m_base,4)}</td><td>{fmt(f.microprice,2)}</td><td>{f.source_age_ms??'N/A'} ms</td></tr>)}</tbody></table></article><article className="uni-flat-panel"><h3>Methodology & limitations</h3><p>Existing v23 causal L1 engine, reusing frozen receiver-asof decisions. OFI measures signed displayed best-level changes during a five-minute rolling window.</p><p>No future return labels enter these features. No fill probabilities, expected returns, or real broker executions are inferred.</p><p>Historical event order is preserved. Session boundaries, stale quotes and incomplete windows are not silently bridged.</p></article></div>:null}
      {mode==='evidence'?<div className="uni-two-panel"><article className="uni-flat-panel"><h3>Source provenance</h3><dl><dt>Venue</dt><dd>Coinbase Advanced public spot</dd><dt>Instruments</dt><dd>BTC-USD · ETH-USD</dd><dt>Cohort</dt><dd>{catalog.source_cohort}</dd><dt>Messages</dt><dd>{catalog.source_message_count.toLocaleString()}</dd><dt>Receive UTC start</dt><dd>{new Date(catalog.source_start_ms).toISOString()}</dd><dt>Receive UTC end</dt><dd>{new Date(catalog.source_end_ms).toISOString()}</dd><dt>Original manifest SHA-256</dt><dd className="uni-hash">{catalog.source_sha256}</dd><dt>Projection SHA-256</dt><dd className="uni-hash">{catalog.projection_sha256}</dd></dl></article><article className="uni-flat-panel"><h3>Research admission</h3><ul className="uni-evidence"><li>✓ Archived source has two passing independent software replays</li><li>✓ Feature diagnostics matched to the frozen v21/v23 reference</li><li>✕ Exchange time not independently calibrated</li><li>✕ Original Feature/Label PIT not verified</li><li>✕ Untouched blind OOS not evaluated</li><li>✕ Real broker execution not connected</li></ul><div className="uni-research-warning">NO CONFIRMED EXECUTABLE ALPHA</div></article></div>:null}
      <footer className="uni-footer">HISTORICAL SOURCE-AUDITED · RECEIVER-ONLY · PIT NOT VERIFIED. Browser is read-only. All 24h/live trading claims are withheld.</footer>
    </>:<p className="uni-note">No verified unified source can be displayed.</p>}
  </section>;
}
