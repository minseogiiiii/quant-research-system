import React,{useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import WorkspaceCandidate from '../workspace/WorkspaceCandidate';
import Plot from '../heatmap/Plot';
import {pairProjections} from './pair';
import type {Pair} from './pair';
import './terminal.css';
const MAX=24*1024*1024;
function Replay(){
 const [pair,setPair]=useState<Pair|null>(null),[error,setError]=useState(''),[time,setTime]=useState<number|null>(null),[log,setLog]=useState(true);
 const depthFile=useRef<File|null>(null),flowFile=useRef<File|null>(null),generation=useRef(0);
 function change(kind:'depth'|'flow',file:File|null){generation.current++;setPair(null);setTime(null);setError('');(kind==='depth'?depthFile:flowFile).current=file;}
 async function load(){
  const id=++generation.current;setPair(null);setTime(null);setError('');
  try{
   const d=depthFile.current,f=flowFile.current;
   if(!d||!f)throw Error('Select both projections');
   if(d.size>MAX||f.size>MAX)throw Error('Projection exceeds 24 MiB');
   const p=pairProjections(JSON.parse(await d.text()),JSON.parse(await f.text()));
   if(id!==generation.current)return;
   setPair(p);setTime(p.depth.start);
  }catch(e){if(id===generation.current)setError(e instanceof Error?e.message:'Invalid pair');}
 }
 const dr=pair?.depth.rows.find(r=>r.time===time),fr=pair?.flow.rows.find(r=>r.time===time);
 const count=pair?(pair.depth.end-pair.depth.start)/pair.depth.step:0;
 const max=pair?.flow.rows.reduce((v,r)=>Math.max(v,Math.abs(Number(r.cvd))),0)??0;
 return <section className="ms-replay" aria-label="Archive replay workspace"><h1>Historical Microstructure</h1>
 <p>ARCHIVE REPLAY · No network feed · Matching manifest claims, product and receiver-time grid required. Local import is not independent cryptographic authentication.</p>
 <div className="ms-controls"><label>Depth projection <input type="file" accept=".json,application/json" onChange={e=>change('depth',e.target.files?.[0]??null)}/></label><label>Flow projection <input type="file" accept=".json,application/json" onChange={e=>change('flow',e.target.files?.[0]??null)}/></label><button onClick={()=>void load()}>Validate pair</button></div>
 {error&&<p role="alert">{error}</p>}{!pair?<p>No admitted projection pair.</p>:<>
 <p>{pair.depth.product} · {count} aligned buckets · {pair.depth.step} ms · manifest claim <code>{pair.depth.pin}</code></p>
 <label>Receiver-time bucket <select aria-label="Receiver-time bucket" value={time??pair.depth.start} onChange={e=>setTime(Number(e.target.value))}>{Array.from({length:count},(_,i)=>pair.depth.start+i*pair.depth.step).map(t=><option key={t} value={t}>{new Date(t).toISOString()}</option>)}</select></label>
 <div className="ms-grid"><section><h2>Observed Liquidity Heatmap</h2><label><input type="checkbox" checked={log} onChange={e=>setLog(e.target.checked)}/>Log intensity</label><Plot data={pair.depth} log={log} select={r=>{if(r)setTime(r.time);}}/></section>
 <section><h2>Observed CVD</h2><svg viewBox="0 0 1000 160" role="img" aria-label="Observed cumulative delta"><line x1="0" x2="1000" y1="80" y2="80" stroke="#536479"/>
 {pair.flow.rows.map((r,i)=>{const x=(r.time-pair.flow.start)/(pair.flow.end-pair.flow.start)*1000,y=80-(max?Number(r.cvd)/max:0)*65,p=pair.flow.rows[i-1];return <g key={r.time}>{p&&r.time-p.time===pair.flow.step&&<line x1={(p.time-pair.flow.start)/(pair.flow.end-pair.flow.start)*1000} y1={80-(max?Number(p.cvd)/max:0)*65} x2={x} y2={y} stroke="#61d8bd" strokeWidth="2"/>}<circle cx={x} cy={y} r={time===r.time?7:3} fill="#61d8bd"/></g>;})}</svg>
 <h2>Selected bucket</h2><p>{time===null?'N/A':new Date(time).toISOString()}</p><p>{dr?'Observed bid '+dr.bid+' / ask '+dr.ask:'No observed L2 in this bucket'}</p><p>{fr?'Buy '+fr.buy+' · Sell '+fr.sell+' · Delta '+fr.delta+' · CVD '+fr.cvd:'No observed unique trade updates in this bucket'}</p>
 <h2>Price Footprint</h2>{fr?<div className="ms-table"><table><thead><tr><th>Price low</th><th>Sell</th><th>Buy</th><th>Count</th></tr></thead><tbody>{fr.cells.map(c=><tr key={c.price}><td>{c.price}</td><td>{c.sell}</td><td>{c.buy}</td><td>{c.trades}</td></tr>)}</tbody></table></div>:<p>No footprint observation.</p>}</section></div></>}
 </section>;
}
function Terminal(){
 const [mode,setMode]=useState<'LIVE'|'REPLAY'>('REPLAY');
 return <><header className="ms-top"><strong>Quant Research Terminal · Candidate</strong><nav aria-label="Data mode"><button aria-pressed={mode==='LIVE'} onClick={()=>setMode('LIVE')}>LIVE READ-ONLY</button><button aria-pressed={mode==='REPLAY'} onClick={()=>setMode('REPLAY')}>ARCHIVE REPLAY</button></nav><span>Orders disabled · NO CONFIRMED EXECUTABLE ALPHA</span></header>{mode==='LIVE'?<WorkspaceCandidate key="live"/>:<Replay key="replay"/>}</>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Terminal/></React.StrictMode>);
