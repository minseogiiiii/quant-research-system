import React,{useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {decode} from './contract';
import type {Projection,Row} from './contract';
import './heatmap.css';
const MAX_BYTES=24*1024*1024;
function Plot({data,log,select}:{data:Projection;log:boolean;select:(row:Row|null)=>void}){
 const canvas=useRef<HTMLCanvasElement>(null);
 const count=(data.end-data.start)/data.step;
 const max=data.rows.reduce((m,r)=>r.cells.reduce((v,c)=>Math.max(v,c.value),m),0);
 useEffect(()=>{
  const el=canvas.current;if(!el)return;const ctx=el.getContext('2d');if(!ctx)return;
  const w=count*2,h=data.bins;el.width=w;el.height=h;
  ctx.fillStyle='#101c28';ctx.fillRect(0,0,w,h);
  for(const t of data.missing){const x=(t-data.start)/data.step;ctx.fillStyle='#3a4250';ctx.fillRect(x*2,0,2,h);}
  for(const r of data.rows){
   const x=(r.time-data.start)/data.step;
   for(const c of r.cells){
    const y=h-1-c.bin;const ratio=max===0?0:log?Math.log1p(c.value)/Math.log1p(max):c.value/max;
    ctx.fillStyle=c.side==='bid'?'rgba(54,211,170,'+(0.15+0.85*ratio)+')':'rgba(243,118,112,'+(0.15+0.85*ratio)+')';
    ctx.fillRect(x*2+(c.side==='offer'?1:0),y,1,1);
   }
   if(r.midBin>=0&&r.midBin<h){ctx.fillStyle='#f4df87';ctx.fillRect(x*2,h-1-Math.floor(r.midBin),2,1);}
  }
 },[data,count,max,log]);
 return <><div className="axis">Price high (exclusive): {data.high}</div><canvas ref={canvas} aria-label="Historical observed liquidity heatmap" onPointerMove={e=>{const box=e.currentTarget.getBoundingClientRect();const i=Math.min(count-1,Math.max(0,Math.floor((e.clientX-box.left)/box.width*count)));select(data.rows.find(r=>r.time===data.start+i*data.step)??null);}} onPointerLeave={()=>select(null)}/><div className="axis">Price low: {data.low} · {new Date(data.start).toISOString()} to {new Date(data.end).toISOString()}</div><p>Green bid / red offer / yellow midpoint / gray no L2 observation. Peak cell quantity: {max} base units. Bid and offer are drawn in separate half-columns; exact quantities remain in the inspector.</p></>;
}
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
