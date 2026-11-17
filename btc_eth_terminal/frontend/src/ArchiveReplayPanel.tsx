import {useEffect,useRef,useState} from 'react';
import {validateDepth10} from './marketStateIntegrity';

type Product = 'BTC-USD'|'ETH-USD';
type SourceHealth = {mode:string;live_feed:boolean;trading_enabled:boolean;source_admission:string;source_manifest_sha256:string;final_state_sha256:string;messages:number};
type MarketRow = {product:string;mode:string;live_feed:boolean;trading_enabled:boolean;receiver_time_ms:number;source_last_l2_receiver_ms:number;source_age_ms:number;state_version:number;source_sequence:number;connection_id:number;status:string;freshness:string;source_manifest_sha256:string;best_bid?:string;best_ask?:string;spread?:string;microprice?:string;best_imbalance?:string;ofi_since_connection_base?:string;depth?:unknown;recent_trades?:Array<{price:string;size:string;side:string;received_at_ms:number}>};
type Page={mode:string;product:string;live_feed:boolean;trading_enabled:boolean;source_manifest_sha256:string;rows:MarketRow[];has_more:boolean;next_after:number};

function verifiedRow(row:MarketRow,product:Product,pin:string):boolean {
 return !!row && row.product===product && row.mode==='ARCHIVE_REPLAY' && row.live_feed===false &&
 row.trading_enabled===false && row.source_manifest_sha256===pin &&
 Number.isSafeInteger(row.state_version) && row.state_version>0 &&
 Number.isSafeInteger(row.receiver_time_ms) && row.receiver_time_ms>0 &&
 Number.isSafeInteger(row.source_last_l2_receiver_ms) &&
 row.source_last_l2_receiver_ms<=row.receiver_time_ms &&
 (row.status==='VALID_ARCHIVED_QUOTE'?validateDepth10(row.depth):row.status==='NO_ADMISSIBLE_QUOTE' && !row.depth);
}
async function checkedJSON<T>(url:string,signal?:AbortSignal):Promise<T> {
 const response=await fetch(url,{signal,cache:'no-store'});
 if(!response.ok)throw new Error('Archive service returned HTTP '+response.status);
 return await response.json() as T;
}
const fmt=(n:number)=>new Date(n).toISOString().replace('T',' ').replace('.000Z','Z');

export default function ArchiveReplayPanel({product}:{product:Product}){
 const [health,setHealth]=useState<SourceHealth|null>(null);
 const [rows,setRows]=useState<MarketRow[]>([]);
 const [cursor,setCursor]=useState(0);
 const [hasMore,setHasMore]=useState(false);
 const [playing,setPlaying]=useState(false);
 const [error,setError]=useState<string|null>(null);
 const [loading,setLoading]=useState(true);
 const [streamResult,setStreamResult]=useState<string>('NOT STARTED');
 const stream=useRef<EventSource|null>(null);
 useEffect(()=>{
   const ctrl=new AbortController();let mounted=true;
   setRows([]);setCursor(0);setHasMore(false);setPlaying(false);setError(null);
   setHealth(null);setLoading(true);setStreamResult('NOT STARTED');
   stream.current?.close();stream.current=null;
   (async()=>{
     const h=await checkedJSON<SourceHealth>('/api/v35/health',ctrl.signal);
     if(h.mode!=='ARCHIVE_REPLAY'||h.live_feed!==false||h.trading_enabled!==false||
        h.source_admission!=='VERIFIED_ARCHIVE_ENVELOPE'||!/^[0-9a-f]{64}$/.test(h.source_manifest_sha256)){
       throw new Error('Source admission contract invalid');
     }
     const p=await checkedJSON<Page>('/api/v35/history?product='+product+'&limit=250',ctrl.signal);
     if(p.mode!=='ARCHIVE_REPLAY'||p.live_feed!==false||p.trading_enabled!==false||
        p.product!==product||p.source_manifest_sha256!==h.source_manifest_sha256||
        !Array.isArray(p.rows)||!p.rows.every(r=>verifiedRow(r,product,h.source_manifest_sha256))||
        p.rows.some((r,i)=>i>0&&
          (r.state_version<=p.rows[i-1].state_version||r.receiver_time_ms<p.rows[i-1].receiver_time_ms))){
       throw new Error('Market state or source mismatch');
     }
     if(mounted){setHealth(h);setRows(p.rows);setHasMore(p.has_more);setCursor(0);}
   })().catch(e=>{if(mounted&&e?.name!=='AbortError')setError(String(e));})
     .finally(()=>{if(mounted)setLoading(false);});
   return()=>{mounted=false;ctrl.abort();stream.current?.close();stream.current=null;};
 },[product]);
 useEffect(()=>{
   if(!playing||!rows.length)return;
   const timer=window.setInterval(()=>{
     setCursor(i=>{if(i>=rows.length-1){setPlaying(false);return i;}return i+1;});
   },450);
   return()=>window.clearInterval(timer);
 },[playing,rows.length]);
 const row=rows[cursor]??null;
 async function loadMore(){
   if(!health||!hasMore||!rows.length)return;
   setPlaying(false);
   try{
     const previous=rows[rows.length-1];
     const p=await checkedJSON<Page>('/api/v35/history?product='+product+'&after='+previous.state_version+'&limit=250');
     if(p.product!==product||p.mode!=='ARCHIVE_REPLAY'||p.live_feed!==false||
        p.trading_enabled!==false||p.source_manifest_sha256!==health.source_manifest_sha256||
        !p.rows.length||p.rows.some((r,i)=>!verifiedRow(r,product,health.source_manifest_sha256)||
          r.state_version<=(i===0?previous.state_version:p.rows[i-1].state_version)||
          r.receiver_time_ms<(i===0?previous.receiver_time_ms:p.rows[i-1].receiver_time_ms))){
       throw new Error('Incremental history integrity violation');
     }
     setRows(old=>old.concat(p.rows));setHasMore(p.has_more);
   }catch(e){setError(String(e));setPlaying(false);}
 }
 function testSSE(){
   stream.current?.close();stream.current=null;setStreamResult('CONNECTING');
   const source=new EventSource('/api/v35/stream?product='+product+'&limit=20');
   stream.current=source;
   let last=0,count=0;
   source.addEventListener('state',(ev:MessageEvent)=>{
     try{
       const next=JSON.parse(ev.data) as MarketRow;
       if(!health||!verifiedRow(next,product,health.source_manifest_sha256)||
          next.state_version<=last)throw new Error('SSE source/state violation');
       last=next.state_version;count++;
       setStreamResult(count+' VERIFIED ARCHIVE STATES');
     }catch(e){setStreamResult('SSE INTEGRITY ERROR '+String(e));source.close();}
   });
   source.addEventListener('replay_end',(ev:MessageEvent)=>{
     try{
       const end=JSON.parse(ev.data) as {next_after:number;mode:string;live_feed:boolean};
       if(end.mode!=='ARCHIVE_REPLAY'||end.live_feed!==false||end.next_after!==last)
         throw new Error('SSE terminal cursor mismatch');
       setStreamResult('SSE VERIFIED · '+count+' states · finite replay');
     }catch(e){setStreamResult('SSE INTEGRITY ERROR '+String(e));}
     source.close();if(stream.current===source)stream.current=null;
   });
   source.onerror=()=>{if(stream.current===source){setStreamResult('SSE CONNECTION FAILED');source.close();stream.current=null;}};
 }
 return <section aria-label="v35.1 verified Coinbase archive replay" style={{margin:'16px 0',padding:16,border:'1px solid #2b4963',borderRadius:12,background:'#0b1828',color:'#e1e9f3'}}>
  <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',flexWrap:'wrap',gap:8}}>
   <div><div style={{fontSize:11,letterSpacing:2,color:'#77b9d5'}}>v35.1 / EXTERNALLY PINNED SOURCE</div><h3 style={{margin:'5px 0'}}>Coinbase archive market-state replay</h3></div>
   <b style={{fontSize:12,color:'#54d7b0'}}>ARCHIVE_REPLAY · NOT LIVE</b>
  </div>
  {loading?<p role="status">Checking archive admission…</p>:null}
  {error?<p role="alert" style={{color:'#ff9494'}}>ARCHIVE DATA BLOCKED: {error}. Existing historical view remains separate.</p>:null}
  {health&&!error?<>
   <p style={{fontSize:12,color:'#8aa9bf'}}>Source SHA-256: <code style={{overflowWrap:'anywhere'}}>{health.source_manifest_sha256}</code> · {health.messages.toLocaleString()} source messages · {health.source_admission}</p>
   <div style={{display:'flex',flexWrap:'wrap',gap:8,alignItems:'center'}} role="group" aria-label="Archive replay controls">
     <button type="button" onClick={()=>{setPlaying(false);setCursor(0);}}>Start</button>
     <button type="button" onClick={()=>setPlaying(true)} disabled={!rows.length||playing||cursor>=rows.length-1}>Play</button>
     <button type="button" onClick={()=>setPlaying(false)} disabled={!playing}>Pause</button>
     <button type="button" onClick={()=>{setPlaying(false);setCursor(i=>Math.min(rows.length-1,i+1));}} disabled={!rows.length}>Step</button>
     <button type="button" onClick={()=>{setPlaying(false);setCursor(rows.length-1);}} disabled={!rows.length}>Latest loaded</button>
     <button type="button" onClick={loadMore} disabled={!hasMore}>Load next 250</button>
     <button type="button" onClick={testSSE}>Check SSE stream</button>
     <span aria-live="polite" style={{fontSize:12}}>{streamResult}</span>
   </div>
   {row?<div>
     <p style={{fontSize:12,color:'#a6c3d8'}}>Observation {cursor+1}/{rows.length} · {fmt(row.receiver_time_ms)} · seq {row.source_sequence} · conn {row.connection_id} · v{row.state_version} · {row.freshness}</p>
     {row.status==='VALID_ARCHIVED_QUOTE'&&row.depth&&validateDepth10(row.depth)?
       <div>
        <div style={{display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(140px,1fr))',gap:8}}>
         {(['best_bid','best_ask','spread','microprice','best_imbalance','ofi_since_connection_base'] as const).map(key=>
          <div key={key} style={{padding:10,background:'#14283e',borderRadius:6}}><small style={{display:'block',color:'#8faac1'}}>{key.replaceAll('_',' ').toUpperCase()}</small><strong style={{fontVariantNumeric:'tabular-nums',overflowWrap:'anywhere'}}>{row[key]}</strong></div>)}
        </div>
        <div style={{overflowX:'auto',marginTop:10}}>
        <table style={{width:'100%',fontSize:12,fontVariantNumeric:'tabular-nums'}} aria-label="Verified replay order book">
         <thead><tr><th>BID PRICE</th><th>BID SIZE</th><th>ASK PRICE</th><th>ASK SIZE</th></tr></thead>
         <tbody>{Array.from({length:10},(_,i)=>{
           const book=row.depth as {bids:Array<{price:string;quantity:string}>;asks:Array<{price:string;quantity:string}>};
           return <tr key={i}><td>{book.bids[i].price}</td><td>{book.bids[i].quantity}</td><td>{book.asks[i].price}</td><td>{book.asks[i].quantity}</td></tr>;
         })}</tbody>
        </table></div>
       </div>:
       <p role="alert" style={{color:'#ffae83'}}>STALE OR UNAVAILABLE ARCHIVE QUOTE. Prices withheld.</p>}
    </div>:<p>No approved archive state available.</p>}
    <p style={{fontSize:11,color:'#a4bac9'}}>Historical receiver clock only. Periodic quotes are sampled, not every L2 event. OFI is connection-cumulative and descriptive, not a return predictor. Existing Funnest chart above remains on its separately pinned historical cohort. NO CONFIRMED EXECUTABLE ALPHA. No order path.</p>
  </>:null}
 </section>;
}