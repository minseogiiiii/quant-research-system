import React,{useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {decode} from './contract';
import type {Projection,Row} from './contract';
import './heatmap.css';
import Plot from './Plot';
const MAX_BYTES=24*1024*1024;
function App(){
 const [data,setData]=useState<Projection|null>(null),[error,setError]=useState(''),[log,setLog]=useState(true),[row,setRow]=useState<Row|null>(null);
 const generation=useRef(0);
 useEffect(()=>()=>{generation.current++;},[]);
 async function read(file:File|undefined){
  const token=++generation.current;setData(null);setRow(null);setError('');
  if(!file)return;
  try{if(file.size>MAX_BYTES)throw Error('File exceeds 24 MiB');const result=decode(JSON.parse(await file.text()));if(token===generation.current)setData(result);}
  catch(e){if(token===generation.current)setError(e instanceof Error?e.message:'Invalid projection');}
 }
 return <main><header><small>F4-A / ARCHIVE REPLAY CANDIDATE</small><h1>Observed Liquidity Heatmap</h1></header>
 <p>Local projection import. Structural validation only; archive provenance is not independently authenticated by this browser. Never LIVE or executable liquidity.</p>
 <label>Projection JSON <input type="file" accept=".json,application/json" onChange={e=>void read(e.target.files?.[0])}/></label>
 {error&&<p role="alert">Rejected: {error}</p>}
 {data?<><section><h2>{data.product}</h2><p>Manifest claim: <code>{data.pin}</code></p><p>{data.rows.length} observed / {data.missing.length} missing buckets · {data.step} ms · price step {data.width}</p><label><input type="checkbox" checked={log} onChange={e=>setLog(e.target.checked)}/> Log intensity</label><Plot data={data} log={log} select={setRow}/></section>
 <section><h2>Inspector</h2>{row?<><p>Receiver observation: {new Date(row.observed).toISOString()} · sequence {row.seq} · bid {row.bid} / ask {row.ask}</p><table><thead><tr><th>Side</th><th>Price bin index</th><th>Exact base quantity</th></tr></thead><tbody>{row.cells.map(c=><tr key={c.side+':'+c.bin}><td>{c.side}</td><td>{c.bin}</td><td>{c.quantity}</td></tr>)}</tbody></table></>:<p>No selected observed bucket. Missing data is not zero depth.</p>}</section></>:<p>No projection loaded. No fabricated prices.</p>}
 </main>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
