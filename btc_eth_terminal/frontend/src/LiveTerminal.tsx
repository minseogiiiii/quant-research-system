import {useEffect,useMemo,useRef,useState} from 'react';
import type {CSSProperties} from 'react';
import {FnChart} from '@funnest/charts-react';
import type {Bar} from '@funnest/charts';
import {createCompletedCandleFeed, validateCompletedCandles} from './v36_1_completed_candle_feed';
import type {CompletedCandle} from './v36_1_completed_candle_feed';
import {validateQuoteAgainstDepth,validateTradeTape} from './marketStateIntegrity';
import {freshSourceHealth,readOnlyJson} from './v36_client_liveness';

type Product='BTC-USD'|'ETH-USD';
type Health={mode:string;live_feed:boolean;trading_enabled:boolean;status:string;reason:string|null;
  session_id:string|null;run_id:string;connection_id:number|null;last_sequence:number;
  last_receiver_time_ms:number|null;subscriptions:string[];snapshots:string[];heartbeats:number;
  failures:number;messages:number;connection_gaps:number};
type Row={schema:string;mode:string;live_feed:boolean;trading_enabled:boolean;session_id:string;run_id:string;
  product:Product;status:string;state_version:number;source_sequence:number;receiver_time_ms:number;
  source_last_l2_receiver_ms:number;connection_id:number;best_bid:string;best_ask:string;spread:string;
  microprice:string;best_imbalance:string;best_bid_qty:string;best_ask_qty:string;ofi_since_connection_base:string;depth:unknown;
  recent_trades:Array<{price:string;size:string;maker_side:string;received_at_ms:number;trade_id?:string}>};
type Candle=CompletedCandle;
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
    !validateQuoteAgainstDepth(r)||!validateTradeTape(r.recent_trades,r.receiver_time_ms))return false;
 return ['best_bid','best_ask','spread','microprice','ofi_since_connection_base']
  .every(k=>typeof r[k as keyof Row]==='string'&&mono.test(r[k as keyof Row] as string) ||
     k==='ofi_since_connection_base' && typeof r.ofi_since_connection_base==='string'&&
     /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(r.ofi_since_connection_base));
}
const cell:CSSProperties={padding:10,background:'#13263b',border:'1px solid #28435a',borderRadius:8};
const formatTime=(ms:number)=>new Date(ms).toISOString();

export default function LiveTerminal(){
 const [product,setProduct]=useState<Product>('BTC-USD');
 const [health,setHealth]=useState<Health|null>(null);
 const [healthProbeAt,setHealthProbeAt]=useState<number|null>(null);
 const [clockNow,setClockNow]=useState(()=>performance.now());
 const [state,setState]=useState<Row|null>(null);
 const [candles,setCandles]=useState<Candle[]>([]);
 const [connection,setConnection]=useState('CONNECTING');
 const [error,setError]=useState<string|null>(null);
 const subscription=useRef<EventSource|null>(null);
 const last=useRef(0);
 const lastFingerprint=useRef<string|null>(null);
 const lastSourceSequence=useRef(-1);
 const [servedBars,setServedBars]=useState<Bar[]>([]);
 const healthFresh=freshSourceHealth(health,healthProbeAt,clockNow);
 const trustedQuote=healthFresh&&state!==null&&state.product===product&&
   state.session_id===health?.session_id&&connection!=='RESYNC_REQUIRED'&&
   connection!=='HEALTH_PROBE_EXPIRED'&&error===null;
 useEffect(()=>{
  const id=window.setInterval(()=>setClockNow(performance.now()),1000);
  return ()=>window.clearInterval(id);
 },[]);
 useEffect(()=>{
  if(healthProbeAt===null||!healthy(health)||healthFresh)return;
  subscription.current?.close();subscription.current=null;
  setState(null);setCandles([]);setConnection('HEALTH_PROBE_EXPIRED');
  setError('HEALTH_PROBE_STALE_OR_CLOCK_ANOMALY');
 },[healthProbeAt,health,healthFresh]);
 const candleFeed=useMemo(()=>createCompletedCandleFeed(product,bars=>setServedBars(bars.map(b=>({...b})))),[product,health?.session_id]);
 useEffect(()=>{setServedBars([]);return ()=>candleFeed.dispose();},[candleFeed]);
 useEffect(()=>{
  if(!state||!freshSourceHealth(health,healthProbeAt,performance.now())||
     state.product!==product||state.session_id!==health?.session_id||!candles.length)return;
  try{candleFeed.publish(candles,health.last_receiver_time_ms??state.receiver_time_ms);}
  catch(e){setState(null);setCandles([]);setConnection('RESYNC_REQUIRED');setError('FUNNEST_CANDLE_INTEGRITY:'+String(e));
   subscription.current?.close();subscription.current=null;}
 },[candleFeed,candles,product,state?.state_version,health?.status,health?.last_receiver_time_ms]);
 useEffect(()=>{
  let active=true;let busy=false;let admittedSession:string|null=null;
  const abort=new AbortController();let pollId:number|null=null;
  subscription.current?.close();subscription.current=null;last.current=0;
  lastFingerprint.current=null;lastSourceSequence.current=-1;
  setHealth(null);setHealthProbeAt(null);setState(null);setCandles([]);setError(null);setConnection('CONNECTING');
  const block=(reason:string)=>{
   if(!active)return;
   setState(null);setCandles([]);setConnection('RESYNC_REQUIRED');setError(reason);
   subscription.current?.close();subscription.current=null;
  };
  const refresh=async()=>{
   if(!active||busy)return;
   busy=true;
   try{
    const h=await readOnlyJson<Health>('/api/v36/health',abort.signal);
    if(!active)return;
    if(healthy(h)&&admittedSession!==null&&admittedSession!==h.session_id){
     // A new authenticated source session is not a continuation.
     subscription.current?.close();subscription.current=null;
     setState(null);setCandles([]);setConnection('RESYNC_REQUIRED');
     last.current=0;lastFingerprint.current=null;lastSourceSequence.current=-1;
    }
    if(healthy(h))admittedSession=h.session_id;
    setHealth(h);setHealthProbeAt(performance.now());setClockNow(performance.now());
    if(!healthy(h)){
     subscription.current?.close();subscription.current=null;
     setState(null);setConnection(h.status);
     return;
    }
    if(!subscription.current){
     const snapshot=await readOnlyJson<{state:Row|null}>('/api/v36/snapshot?product='+product,abort.signal);
     if(!active)return;
     if(!snapshot.state||!validRow(snapshot.state,product,h))throw new Error('UNTRUSTED_LIVE_SNAPSHOT');
     const current=snapshot.state;
     if(current.state_version>=last.current){
      last.current=current.state_version;
      lastFingerprint.current=JSON.stringify(current);
      lastSourceSequence.current=current.source_sequence;
      setState(current);
     }
     const stream=new EventSource('/api/v36/stream?product='+product);
     subscription.current=stream;setConnection('SSE_CONNECTED');setError(null);
     const next=(event:MessageEvent)=>{
      if(!active||subscription.current!==stream)return;
      try{
       const r=JSON.parse(event.data) as Row;
       if(!validRow(r,product,h))throw new Error('SSE_SOURCE_OR_DEPTH_INVALID');
       if(r.state_version<last.current)throw new Error('SSE_STATE_VERSION_REGRESSION');
       const fingerprint=JSON.stringify(r);
       if(r.state_version===last.current){
        if(lastFingerprint.current!==fingerprint)throw new Error('SSE_DUPLICATE_VERSION_CONFLICT');
        return;
       }
       if(r.source_sequence<lastSourceSequence.current)throw new Error('SOURCE_SEQUENCE_REGRESSION');
       last.current=r.state_version;lastFingerprint.current=fingerprint;
       lastSourceSequence.current=r.source_sequence;setState(r);
      }catch(e){block(String(e));}
     };
     stream.addEventListener('snapshot',next);
     stream.addEventListener('state',next);
     stream.addEventListener('resync_required',(event:MessageEvent)=>block('SOURCE_GAP:'+event.data));
     stream.addEventListener('stream_rotation',()=>{if(!active||subscription.current!==stream)return;
      stream.close();subscription.current=null;setConnection('ROTATING_STREAM');});
     stream.onerror=()=>{if(active&&subscription.current===stream)block('SOURCE_SSE_DISCONNECTED_OR_INVALID');};
    }
    const c=await readOnlyJson<{mode:string;session_id:string;rows:Candle[];incomplete_candle_never_published:boolean}>(
      '/api/v36/candles?product='+product,abort.signal);
    if(!active||admittedSession!==h.session_id)return;
    if(c.mode!==h.mode||c.session_id!==h.session_id||c.incomplete_candle_never_published!==true||
       !Array.isArray(c.rows)||!c.rows.every(b=>Number.isSafeInteger(b.time)&&b.time>0&&
       b.time%60000===0&&Number.isSafeInteger(b.available_at)&&b.available_at>=b.time+60000&&
       [b.open,b.high,b.low,b.close,b.volume].every(v=>typeof v==='string'&&mono.test(v)) &&
       Number.isSafeInteger(b.observed_trade_count)&&b.observed_trade_count>0)){
      throw new Error('LIVE_CANDLE_PROVENANCE_INVALID');
    }
    if(!validateCompletedCandles(c.rows,h.last_receiver_time_ms??0))
      throw new Error('INCOMPLETE_OR_UNAVAILABLE_CANDLE');
    setCandles(c.rows);
   }catch(e){if(active&&(e as Error).name!=='AbortError')block(String(e));}
   finally{busy=false;}
  };
  void refresh();pollId=window.setInterval(()=>{void refresh();},3500);
  return()=>{active=false;abort.abort();if(pollId!==null)window.clearInterval(pollId);subscription.current?.close();subscription.current=null;};
 },[product]);

 return <main className="v36-live-terminal" style={{background:'#081523',minHeight:'100vh',color:'#dbeaf2',padding:'20px clamp(10px,3vw,35px)',fontFamily:'system-ui,Arial'}}>
  <header style={{display:'flex',flexWrap:'wrap',alignItems:'center',justifyContent:'space-between',gap:12}}>
   <div><small style={{color:'#8cb4ce'}}>v36 · COINBASE PUBLIC SPOT · READ-ONLY</small>
    <h1 style={{margin:'4px 0'}}>BTC/ETH Live Market Research</h1></div>
   <div style={{display:'flex',gap:8}} role="group" aria-label="Instrument">
    {(['BTC-USD','ETH-USD'] as Product[]).map(p=><button key={p} type="button" onClick={()=>{if(p!==product){setState(null);setCandles([]);setConnection('CONNECTING');setProduct(p);}}}
      aria-pressed={product===p} style={{padding:10,borderRadius:8,color:'#fff',background:product===p?'#185b79':'#1c3448'}}>{p}</button>)}
   </div>
  </header>
  <p><strong style={{color:trustedQuote?'#5be0b4':'#ffb77a'}}>
   {health?.mode==='LIVE_SOURCE'&&trustedQuote?'OBSERVED LIVE SOURCE':
    health?.mode==='ARCHIVE_SIMULATION'?'ARCHIVE SIMULATION · NOT LIVE':'NOT ADMITTED · NO LIVE MARKET DATA'}
  </strong> · {connection} · {health?.status??'CONNECTING'} · orders disabled</p>
  <p style={{color:'#96b3c9',fontSize:12}} aria-label="Source health">Session {health?.session_id??'none'} · messages {health?.messages??0}
   · source seq {health?.last_sequence??'none'} · heartbeat count {health?.heartbeats??0}
   · gap count {health?.connection_gaps??0}</p>
  {error?<p role="alert" style={{color:'#ff9898'}}>MARKET DATA NOT TRUSTED: {error}</p>:null}
  {trustedQuote && state ?<>
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
    <output data-testid="funnest-actual-getbars" data-product={product} data-session={health?.session_id??''} data-bars={JSON.stringify(servedBars)} hidden />
    {candles.length>0&&state?.session_id===health?.session_id?<div style={{height:340}}><FnChart key={product+'-'+String(health?.session_id)}
        datafeed={candleFeed.datafeed} symbol={product} resolution="1" colorScheme="dark"
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
