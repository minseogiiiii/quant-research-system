import {useRef,useState} from 'react';
import {decodeResearch} from './diagnosticContract';
import type {Diagnostic} from './diagnosticContract';
const fmt=(v:number|null)=>v===null?'N/A':v.toFixed(4);
export default function ResearchPanel(){
 const [data,setData]=useState<Diagnostic[]>([]),[product,setProduct]=useState(0),[error,setError]=useState(''),[disjoint,setDisjoint]=useState(false);
 const token=useRef(0);
 async function load(file:File|undefined){
  const id=++token.current;setData([]);setProduct(0);setError('');
  if(!file)return;
  try{if(file.size>2*1024*1024)throw Error('Report exceeds 2 MiB');const v=decodeResearch(JSON.parse(await file.text()));if(id===token.current)setData(v);}
  catch(e){if(id===token.current)setError(e instanceof Error?e.message:'Rejected');}
 }
 const d=data[product];
 return <section className="ms-replay" aria-label="Research diagnostic workspace"><h1>Predictive Research</h1><p>DESCRIPTIVE ONLY · Local report import · Provenance and independent witness are report claims. Not an OOS result, p-value, or executable return.</p>
 <label>Research diagnostic JSON <input type="file" accept=".json,application/json" onChange={e=>void load(e.target.files?.[0])}/></label>
 {error&&<p role="alert">Rejected: {error}</p>}{!d?<p>No admitted research report.</p>:<>
 <p>Manifest claim <code>{d.pin}</code></p><label>Product <select aria-label="Research product" value={product} onChange={e=>setProduct(Number(e.target.value))}>{data.map((r,i)=><option key={r.product} value={i}>{r.product}</option>)}</select></label>
 <label><input type="checkbox" checked={disjoint} onChange={e=>setDisjoint(e.target.checked)}/> Disjoint feature-to-label intervals only</label>
 <p>IC requires 20 pairs; disjoint intervals do not prove independence. N/A is not zero.</p>
 {d.results.map(r=><section key={r.horizon} className="ms-grid" style={{display:'block'}}><h2>Horizon {r.horizon} ms</h2><p>{r.paired} paired / {r.causal} causal features / {r.grid} decisions · {r.missing} missing labels · {r.disjoint} disjoint</p>
 <div className="ms-table"><table><thead><tr><th>Feature</th><th>N</th><th>Pearson IC</th><th>Rank IC</th><th>Direction agreement</th><th>Negative mean bps (N)</th><th>Positive mean bps (N)</th></tr></thead><tbody>{r.features.map(f=>{const m=disjoint?f.separated:f.all;return <tr key={f.name}><td>{f.name}</td><td>{m.n}</td><td>{fmt(m.ic)}</td><td>{fmt(m.rank)}</td><td>{fmt(m.direction)}</td><td>{fmt(m.groups[0].mean)} ({m.groups[0].n})</td><td>{fmt(m.groups[2].mean)} ({m.groups[2].n})</td></tr>;})}</tbody></table></div></section>)}
 </>}
 </section>;
}
