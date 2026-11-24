import {useRef,useState} from 'react';
import {decodeQualification} from './qualificationContract';
import type {Qualification} from './qualificationContract';
const descriptions:Record<string,string>={SYNTHETIC_ONLY:'Synthetic engineering fixture',INCOMPLETE_TRIAL_INVENTORY:'Trial inventory incomplete',TRIAL_FAILED:'Trial failed',NO_COMPLETED_TRADES:'No completed trades',NONPOSITIVE_DEVELOPMENT_RETURN:'Nonpositive development return',POSITIVE_DEVELOPMENT_ONLY:'Positive development only',SAME_POLICY_HOLDOUT_UNVERIFIED:'Same-policy holdout not verified',REAL_FILL_COST_UNCALIBRATED:'Real fill and cost calibration absent',LIVE_RESILIENCE_UNVERIFIED:'Live resilience not verified',MANUAL_APPROVAL_UNAVAILABLE:'Manual authorization unavailable'};
export default function QualificationPanel(){
 const [data,setData]=useState<Qualification|null>(null),[error,setError]=useState(''),[index,setIndex]=useState(0);const token=useRef(0);
 async function load(file:File|undefined){const id=++token.current;setData(null);setError('');setIndex(0);if(!file)return;
  try{if(file.size>4*1024*1024)throw Error('Report exceeds 4 MiB');const v=decodeQualification(JSON.parse(await file.text()));if(id===token.current)setData(v);}
  catch(e){if(id===token.current)setError(e instanceof Error?e.message:'Invalid report');}
 }
 const selected=data?.trials[index];
 return <section className="ms-replay" aria-label="Qualification workspace"><h1>Strategy Qualification</h1><p>READ-ONLY · Local diagnostic import. Evidence and registry verification are report claims, not browser authentication. No shadow, paper or real orders authorized.</p>
 <label>Qualification JSON <input type="file" accept=".json,application/json" onChange={e=>void load(e.target.files?.[0])}/></label>
 {error&&<p role="alert">Rejected: {error}</p>}{!data?<p>No admitted qualification report.</p>:<>
 <h2>NO APPROVAL</h2><p>{data.source} · {data.sessions} development session(s) · {data.trials.length} registered trial(s)</p>
 <div className="ms-table"><table><thead><tr><th>Product</th><th>Holding period</th><th>Trial</th><th>Mean session return</th><th>Closed roundtrips</th><th>Verdict</th></tr></thead><tbody>{data.trials.map(t=><tr key={t.id}><td>{t.product}</td><td>{t.hold/1000}s</td><td>{t.status}</td><td>{t.mean===null?'N/A':(t.mean*100).toFixed(5)+'%'}</td><td>{t.trades}</td><td>{t.verdict}</td></tr>)}</tbody></table></div>
 <label>Inspect trial <select aria-label="Inspect trial" value={index} onChange={e=>setIndex(Number(e.target.value))}>{data.trials.map((t,i)=><option key={t.id} value={i}>{t.product} · {t.hold/1000}s</option>)}</select></label>
 {selected&&<section><h2>Blocking evidence</h2><ul>{selected.reasons.map(r=><li key={r}>{descriptions[r]}</li>)}</ul></section>}
 <details><summary>Evidence identifiers (claims)</summary><p>Plan <code>{data.plan}</code></p><p>Registry head <code>{data.head}</code></p><p>Development report <code>{data.report}</code></p><p>Catalog <code>{data.catalog}</code></p></details></>}
 </section>;
}
