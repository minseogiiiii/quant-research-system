import {useEffect,useMemo,useRef,useState} from 'react';
import type {CSSProperties} from 'react';
import {FnChart} from '@funnest/charts-react';
import type {Datafeed, Bar, BarsResult, Resolution, SymbolInfo} from '@funnest/charts';
import {validateDepth10} from './marketStateIntegrity';

type Product='BTC-USD'|'ETH-USD';
type Health={mode:string;live_feed:boolean;trading_enabled:boolean;status:string;reason:string|null;
  session_id:string|null;run_id:string;connection_id:number|null;last_sequence:number;
  last_receiver_time_ms:number|null;subscriptions:string[];snapshots:string[];heartbeats:number;
  failures:number;messages:number;connection_gaps:number};
type Row={schema:string;mode:string;live_feed:boolean;trading_enabled:boolean;session_id:string;run_id:string;
  product:Product;status:string;state_version:number;source_sequence:number;receiver_time_ms:number;
  source_last_l2_receiver_ms:number;connection_id:number;best_bid:string;best_ask:string;spread:string;
  microprice:string;best_imbalance:string;ofi_since_connection_base:string;depth:unknown;
  recent_trades:Array<{price:string;size:string;maker_side:string;received_at_ms:number;trade_id?:string}>};
type Candle={time:number;available_at:number;open:string;high:string;low:string;close:string;volume:string;observed_trade_count:number};
const mono=/^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const healthy=(h:Health|null)=>h?.status==='HEALTHY' && h.trading_enabled===false &&
  (h.mode==='LIVE_SOURCE' ? h.live_feed===true : h.mode==='ARCHIVE_SIMULATION' && h.live_feed===false);
function validRow(r:Row,p:Product,h:Health):boolean {
 if(!r||r.schema!=='v36_source_state_v1'||r.product!==p||r.mode!==h.mode||
    r.live_feed!==h.live_feed||r.trading_enabled!==false||r.status!=='VALID_OBSERVED_QUOTE'||
    r.session_id!==h.session_id||r.run_id!==h.run_id||
    !Number.isSafeInteger(r.state_version)||r.state_version<1||
    !Number.isSafeInteger(r.source_sequence)||r.source_sequence<0||
    !Number.isSafeInteger(r.receiver_time_ms)||r.receiver_time_ms<1||
    !Number.isSafeInteger(r.source_last_l2_receiver_ms)||
    r.receiver_time_ms<r.source_last_l2_receiver_ms||
    !validateDepth10(r.depth))return false;
 return ['best_bid','best_ask','spread','microprice','ofi_since_connection_base']
  .every(k=>typeof r[k as keyof Row]==='string'&&mono.test(r[k as keyof Row] as string) ||
     k==='ofi_since_connection_base' && typeof r.ofi_since_connection_base==='string'&&
     /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(r.ofi_since_connection_base));
}
async function api<T>(url:string,signal?:AbortSignal):Promise<T>{
 const res=await fetch(url,{cache:'no-store',signal});
 if(!res.ok)throw new Error(`${url} HTTP ${res.status}`);
 return await res.json() as T;
}
const cell:CSSProperties={padding:10,background:'#13263b',border:'1px solid #28435a',borderRadius:8};
const formatTime=(ms:number)=>new Date(ms).toISOString();

export default function LiveTerminal(){
 const [product,setProduct]=useState<Product>('BTC-USD');
 const [health,setHealth]=useState<Health|null>(null);
 const [state,setState]=useState<Row|null>(null);
 const [candles,setCandles]=useState<Candle[]>([]);
 const [connection,setConnection]=useState('CONNECTING');
 const [error,setError]=useState<string|null>(null);
 const subscription=useRef<EventSource|null>(null);
 const last=useRef(0);
 useEffect(()=>{
  let active=true;const abort=new AbortController();let pollId:number|null=null;
  subscription.current?.close();subscription.current=null;last.current=0;
  setHealth(null);setState(null);setCandles([]);setError(null);setConnection('CONNECTING');
  const block=(reason:string)=>{
   if(!active)return;
   setState(null);setConnection('RESYNC_REQUIRED');setError(reason);
   subscription.current?.close();subscription.current=null;
  };
  const refresh=async()=>{
   try{
    const h=await api<Health>('/api/v36/health',abort.signal);
    if(!active)return;
    setHealth(h);
    if(!healthy(h)){
     subscription.current?.close();subscription.current=null;
     setState(null);setConnection(h.status);
     return;
    }
    if(!subscription.current){
     const snapshot=await api<{state:Row|null}>('/api/v36/snapshot?product='+product,abort.signal);
     if(!active)return;
     if(!snapshot.state||!validRow(snapshot.state,product,h))throw new Error('UNTRUSTED_LIVE_SNAPSHOT');
     const current=snapshot.state;
     last.current=current.state_version;setState(current);
     const stream=new EventSource('/api/v36/stream?product='+product);
     subscription.current=stream;setConnection('SSE_CONNECTED');setError(null);
     const next=(event:MessageEvent)=>{
      try{
       const r=JSON.parse(event.data) as Row;
       if(!validRow(r,product,h))throw new Error('SSE_SOURCE_OR_DEPTH_INVALID');
       if(r.state_version<last.current)throw new Error('SSE_STATE_VERSION_REGRESSION');
       if(r.state_version===last.current)return; // benign first snapshot duplicate
       last.current=r.state_version;setState(r);
      }catch(e){block(String(e));}
     };
     stream.addEventListener('snapshot',next);
     stream.addEventListener('state',next);
     stream.addEventListener('resync_required',(event:MessageEvent)=>block('SOURCE_GAP:'+event.data));
     stream.addEventListener('stream_rotation',()=>{stream.close();if(subscription.current===stream)subscription.current=null;});
     stream.onerror=()=>block('SOURCE_SSE_DISCONNECTED_OR_INVALID');
    }
    const c=await api<{mode:string;session_id:string;rows:Candle[];incomplete_candle_never_published:boolean}>(
      '/api/v36/candles?product='+product,abort.signal);
    if(!active)return;
    if(c.mode!==h.mode||c.session_id!==h.session_id||c.incomplete_candle_never_published!==true||
       !Array.isArray(c.rows)||!c.rows.every(b=>Number.isSafeInteger(b.time)&&b.time>0&&
       b.time%60000===0&&Number.isSafeInteger(b.available_at)&&b.available_at>=b.time+60000&&
       [b.open,b.high,b.low,b.close,b.volume].every(v=>typeof v==='string'&&mono.test(v)) &&
       Number.isSafeInteger(b.observed_trade_count)&&b.observed_trade_count>0)){
      throw new Error('LIVE_CANDLE_PROVENANCE_INVALID');
    }
    setCandles(c.rows);
   }catch(e){if(active&&(e as Error).name!=='AbortError')block(String(e));}
  };
  void refresh();pollId=window.setInterval(()=>{void refresh();},3500);
  return()=>{active=false;abort.abort();if(pollId!==null)window.clearInterval(pollId);subscription.current?.close();subscription.current=null;};
 },[product]);
 const chartDatafeed=useMemo(():Datafeed|null=>{
  if(!candles.length||!state)return null;
  const symbol:SymbolInfo={symbol:product,name:`${product} · observed public spot trade updates`,
    exchange:'Coinbase Advanced · receiver clock',session:'24x7',timezone:'UTC',type:'crypto',
    tickSize:0.01,pricePrecision:2,supportedResolutions:['1']};
  const visible=candles.filter(c=>c.available_at<=state.receiver_time_ms);
  return {
   async resolveSymbol(name:string){if(name!==product)throw Error('UNKNOWN_SYMBOL');return symbol;},
   async searchSymbols(q:string){return product.toLowerCase().includes(q.toLowerCase())?[symbol]:[];},
   async getBars(info:SymbolInfo,resolution:Resolution,request):Promise<BarsResult>{
    if(info.symbol!==product||resolution!=='1')throw Error('UNSUPPORTED_LIVE_RESOLUTION');
    const bars:Bar[]=visible.filter(b=>b.time>=request.from&&b.time<request.to)
      .map(b=>({time:b.time,open:Number(b.open),high:Number(b.high),low:Number(b.low),
                close:Number(b.close),volume:Number(b.volume)}));
    return {bars,noMoreData:true};
   },subscribeBars(){return()=>{};}
  };
 },[candles,state,product]);
 return <main className="v36-live-terminal" style={{background:'#081523',minHeight:'100vh',color:'#dbeaf2',padding:'20px clamp(10px,3vw,35px)',fontFamily:'system-ui,Arial'}}>
  <header style={{display:'flex',flexWrap:'wrap',alignItems:'center',justifyContent:'space-between',gap:12}}>
   <div><small style={{color:'#8cb4ce'}}>v36 · COINBASE PUBLIC SPOT · READ-ONLY</small>
    <h1 style={{margin:'4px 0'}}>BTC/ETH Live Market Research</h1></div>
   <div style={{display:'flex',gap:8}} role="group" aria-label="Instrument">
    {(['BTC-USD','ETH-USD'] as Product[]).map(p=><button key={p} type="button" onClick={()=>setProduct(p)}
      aria-pressed={product===p} style={{padding:10,borderRadius:8,color:'#fff',background:product===p?'#185b79':'#1c3448'}}>{p}</button>)}
   </div>
  </header>
  <p><strong style={{color:healthy(health)?'#5be0b4':'#ffb77a'}}>
   {health?.mode==='LIVE_SOURCE' && healthy(health)?'OBSERVED LIVE SOURCE':
    health?.mode==='ARCHIVE_SIMULATION'?'ARCHIVE SIMULATION · NOT LIVE':'NOT ADMITTED · NO LIVE MARKET DATA'}
  </strong> · {connection} · {health?.status??'CONNECTING'} · orders disabled</p>
  <p style={{color:'#96b3c9',fontSize:12}} aria-label="Source health">Session {health?.session_id??'none'} · messages {health?.messages??0}
   · source seq {health?.last_sequence??'none'} · heartbeat count {health?.heartbeats??0}
   · gap count {health?.connection_gaps??0}</p>
  {error?<p role="alert" style={{color:'#ff9898'}}>MARKET DATA NOT TRUSTED: {error}</p>:null}
  {state && healthy(health) && connection!=='RESYNC_REQUIRED'?<>
   <p role="status" style={{fontSize:12,color:'#92b6c9'}}>Observed {formatTime(state.receiver_time_ms)} · conn {state.connection_id}
     · source sequence {state.source_sequence} · state version {state.state_version} · {state.mode}</p>
   <section aria-label="Validated top of book" style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(150px,1fr))',gap:8}}>
     {(['best_bid','best_ask','spread','microprice','best_imbalance','ofi_since_connection_base'] as const).map(k=>
     <div key={k} style={cell}><small style={{display:'block',color:'#7da9c8'}}>{k.toUpperCase().replaceAll('_',' ')}</small>
       <strong style={{fontVariantNumeric:'tabular-nums',overflowWrap:'anywhere'}}>{state[k]}</strong></div>)}
   </section>
   <section aria-label="Live source-backed Funnest chart" style={{marginTop:16,...cell}}>
    <h2 style={{fontSize:16}}>Funnest Charts · completed receiver-time trade candles</h2>
    <p style={{fontSize:12,color:'#9db6c5'}}>No incomplete candle is labelled final. Visual chart numbers use JavaScript display precision; backend financial arithmetic remains Decimal.</p>
    {chartDatafeed?<div style={{height:340}}><FnChart key={product+'-'+String(candles.at(-1)?.time)}
        datafeed={chartDatafeed} symbol={product} resolution="1" colorScheme="dark"
        locale="en" storageKey={null} drawingToolbar={false} onReady={()=>{}} style={{height:'100%'}} /></div>:
     <p>No completed trade-derived minute is available yet.</p>}
   </section>
   <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(295px,1fr))',gap:12,marginTop:14}}>
    <section style={cell} aria-label="Live order book 10 levels"><h2 style={{fontSize:16}}>Order Book · 10 levels</h2>
     <table style={{width:'100%',fontVariantNumeric:'tabular-nums',fontSize:12}}>
      <thead><tr><th>Bid price</th><th>Bid qty</th><th>Ask price</th><th>Ask qty</th></tr></thead>
      <tbody>{Array.from({length:10},(_,i)=>{
       const book=state.depth as {bids:Array<{price:string;quantity:string}>;asks:Array<{price:string;quantity:string}>};
       return <tr key={i}><td>{book.bids[i].price}</td><td>{book.bids[i].quantity}</td>
        <td>{book.asks[i].price}</td><td>{book.asks[i].quantity}</td></tr>;
      })}</tbody>
     </table>
    </section>
    <section style={cell} aria-label="Observed trade tape"><h2 style={{fontSize:16}}>Trade Tape</h2>
      <p style={{fontSize:11,color:'#9eb8ca'}}>Coinbase side refers to reported maker/resting side. It is NOT aggressor classification.</p>
      {state.recent_trades.slice(-12).reverse().map((t,i)=><p key={t.trade_id??i} style={{fontVariantNumeric:'tabular-nums',fontSize:12}}>
       {t.price} · {t.size} · maker {t.maker_side} · {formatTime(t.received_at_ms)}
      </p>)}
    </section>
   </div>
  </>:<section style={{...cell,padding:25}} aria-label="Source not admitted">
   <h2>No currently trusted market quote</h2>
   <p>{health?.reason??'Awaiting Coinbase collector, dual snapshots, subscriptions and heartbeats.'}</p>
   <p>Stale, disconnected or quarantined sources never retain trusted displayed Bid/Ask.</p>
  </section>}
  <p style={{marginTop:24,fontSize:12,color:'#9cb7c7'}}>Historical research: <a href="/research">/research</a> · Archive replay: <a href="/research/v35.1">/research/v35.1</a> · NO CONFIRMED EXECUTABLE ALPHA. No exchange orders.</p>
 </main>;
}
