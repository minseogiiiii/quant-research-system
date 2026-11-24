import {useRef,useState} from 'react';
import {decodeFlow} from './contract';
import type {Flow} from './contract';
import './orderflow.css';
export default function OrderFlowPanel(){
 const [data,setData]=useState<Flow|null>(null),[error,setError]=useState(''),[selection,setSelection]=useState(0);
 const generation=useRef(0);
 async function load(file:File|undefined){
  const id=++generation.current;setData(null);setError('');setSelection(0);
  if(!file)return;
  try{if(file.size>24*1024*1024)throw Error('File exceeds 24 MiB');const r=decodeFlow(JSON.parse(await file.text()));if(id===generation.current)setData(r);}
  catch(e){if(id===generation.current)setError(e instanceof Error?e.message:'Invalid projection');}
 }
 const row=data?.rows[selection],max=data?.rows.reduce((m,r)=>Math.max(m,Math.abs(Number(r.cvd))),0)??0;
 return <section className="of-panel"><h2>Trade Footprint &amp; Observed CVD</h2><p>Historical receiver-time updates. Local import, structural reconciliation only; source provenance is a claim. Not live, not fills.</p>
 <label>Order flow JSON <input type="file" accept=".json,application/json" onChange={e=>void load(e.target.files?.[0])}/></label>
 {error&&<p role="alert">Rejected: {error}</p>}
 {data?<><p>{data.product} · {data.count} unique updates · {data.empty.length} buckets without observed trades · {data.step} ms</p><p>Manifest claim: <code>{data.pin}</code></p>
 <svg viewBox="0 0 1000 150" role="img" aria-label="Observed cumulative volume delta; unobserved buckets are not connected" preserveAspectRatio="none">
 <line x1="0" y1="75" x2="1000" y2="75" stroke="#52677a"/>
 {data.rows.map((r,i)=>{const x=(r.time-data.start)/(data.end-data.start)*1000,y=75-(max?Number(r.cvd)/max:0)*65;const p=data.rows[i-1];return <g key={r.time}>{p&&r.time-p.time===data.step&&<line x1={(p.time-data.start)/(data.end-data.start)*1000} y1={75-(max?Number(p.cvd)/max:0)*65} x2={x} y2={y} stroke="#58d2b4" strokeWidth="2"/>}<circle cx={x} cy={y} r="2.5" fill="#58d2b4"/></g>;})}</svg>
 {row?<><label>Observed bucket <select value={selection} onChange={e=>setSelection(Number(e.target.value))}>{data.rows.map((r,i)=><option key={r.time} value={i}>{new Date(r.time).toISOString()}</option>)}</select></label>
 <p>Buy {row.buy} · Sell {row.sell} · Delta {row.delta} · CVD {row.cvd} (base units)</p>
 <div className="of-table"><table><thead><tr><th>Price bucket low</th><th>Aggressive sell</th><th>Aggressive buy</th><th>Trades</th></tr></thead><tbody>{row.cells.map(c=><tr key={c.price}><td>{c.price}</td><td className="of-sell">{c.sell}</td><td className="of-buy">{c.buy}</td><td>{c.trades}</td></tr>)}</tbody></table></div></>:<p>No observed trades in this window.</p>}</>:<p>No order flow projection loaded.</p>}
 </section>;
}
