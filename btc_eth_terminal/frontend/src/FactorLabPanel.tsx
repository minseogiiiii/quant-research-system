import React, {useEffect,useState} from 'react';

type Product = 'BTC-USD'|'ETH-USD';
type Section = 'research'|'microstructure'|'execution';
type Summary = {feature_rows:number;primary_60s_future_mid_labels:number;oos_status:string;
  out_of_sample_predictive_effect:null;cost_adjusted_return_bps:null};
type MicroRow = {decision_receiver_ms:number;source_age_ms:number;
  features_asof:Record<string,string|number|null>};
type CostScenario = {scenario:string;side:'BUY'|'SELL';requested_base:string;
  status:string;reference_vwap:string|null;uncovered_base:string;
  fee_bps_assumed:string;extra_non_book_slippage_bps_assumed:string;
  total_indicative_taker_cost_bps_assumed:string|null;displayed_depth_fraction_assumed:string};
type FactorResult = {schema:'q7_factor_lab_v1';product:Product;section:Section;
  source_sha256:string;report_sha256:string;mode:'ARCHIVE_REPLAY';oos_status:string;
  asof_receiver_ms:number;verdict:string;real_orders_enabled:false;
  summary?:Summary;features?:string[];rows?:MicroRow[];
  samples?:{decision_receiver_ms:number;scenarios:CostScenario[]}[];
  twap_status?:string;};
const sections:Section[]=['research','microstructure','execution'];
const label:Record<Section,string>={research:'Research',microstructure:'Microstructure',execution:'Execution analysis'};
function val(value:unknown){return value===null||value===undefined?'—':String(value)}
export default function FactorLabPanel({product,asof,sourceSha}:{product:Product;asof:number|null;sourceSha:string}){
 const [section,setSection]=useState<Section>('research');
 const [result,setResult]=useState<FactorResult|null>(null);
 const [error,setError]=useState<string|null>(null);
 useEffect(()=>{
   setResult(null);setError(null);
   if(asof===null)return;
   const controller=new AbortController();
   const params=new URLSearchParams({product,asof:String(asof),section});
   fetch(`/api/v37/factor-lab?${params}`,{cache:'no-store',signal:controller.signal})
    .then(async r=>{if(!r.ok)throw new Error(`Factor Lab unavailable (HTTP ${r.status})`);return r.json() as Promise<FactorResult>})
    .then(d=>{
      if(controller.signal.aborted)return;
      if(d.schema!=='q7_factor_lab_v1'||d.product!==product||d.section!==section||d.mode!=='ARCHIVE_REPLAY'||
         d.source_sha256!==sourceSha||d.asof_receiver_ms!==asof||d.real_orders_enabled!==false||
         !/^[a-f0-9]{64}$/.test(d.report_sha256)||d.verdict!=='INSUFFICIENT_EVIDENCE')
        throw new Error('Factor Lab evidence identity mismatch');
      setResult(d);
    }).catch(e=>{if(!controller.signal.aborted)setError(String(e))});
   return()=>controller.abort();
 },[product,asof,sourceSha,section]);
 // Never render a response from an earlier product, as-of cutoff, source or tab,
 // even for a single React commit before the effect cleanup runs.
 const visible=result&&result.product===product&&result.section===section&&
   result.asof_receiver_ms===asof&&result.source_sha256===sourceSha?result:null;
 return <article className="pro-panel pro-detail-panel q7-factor" aria-label="Q7 research Factor Lab">
  <header><div className="pro-eyebrow">Q7 / EXPERIMENT Q7-OFI-LIQUIDITY-RECOVERY-001</div>
  <h3>OFI & liquidity recovery research</h3>
  <p>ARCHIVE REPLAY ONLY · ALREADY EXPLORED 1H · NOT BLIND OOS · NO EXECUTABLE ALPHA</p></header>
  <div className="q7-tabs" role="tablist" aria-label="Factor Lab study views">
   {sections.map(x=><button type="button" role="tab" aria-selected={section===x} key={x}
     onClick={()=>setSection(x)}>{label[x]}</button>)}
  </div>
  {error?<p className="pro-alert pro-alert-error" role="alert">RESEARCH EVIDENCE BLOCKED · {error}</p>:null}
  {!error&&!visible?<p>Validating receiver-time research evidence…</p>:null}
  {visible&&<>
    {section==='research'&&<div>
      <p><strong>Verdict:</strong> INSUFFICIENT_EVIDENCE. Untouched independent multi-day OOS not available.</p>
      <table><tbody><tr><th>Primary horizon</th><td>60 seconds after decision availability</td></tr>
      <tr><th>Past-only feature rows</th><td>{visible.summary?.feature_rows??0}</td></tr>
      <tr><th>Future 60s mid labels, descriptive</th><td>{visible.summary?.primary_60s_future_mid_labels??0}</td></tr>
      <tr><th>Registered comparison</th><td>Momentum / OBI / microprice vs incremental L1 OFI</td></tr>
      <tr><th>OOS predictive gain</th><td>NOT EVALUATED</td></tr>
      <tr><th>After-cost PnL</th><td>NOT EVALUATED</td></tr>
      <tr><th>Blind final test</th><td>NOT OPENED</td></tr></tbody></table>
      <p>Six archived diagnostic fields: {(visible.features??[]).join(', ') || 'Not available'}. Existing v23 draft registration remains unpromoted.</p>
    </div>}
    {section==='microstructure'&&<div className="pro-detail-scroll">
      <p>Past-only, receiver-asof five-minute L1 OFI. No future labels exposed to this view.</p>
      <table><thead><tr><th>UTC</th><th>OFI 5m</th><th>OBI</th><th>Spread bps</th><th>Momentum 60s</th><th>Age ms</th></tr></thead>
      <tbody>{(visible.rows??[]).map(r=><tr key={r.decision_receiver_ms}>
       <td>{new Date(r.decision_receiver_ms).toISOString().slice(11,19)}</td>
       <td>{val(r.features_asof.ofi_l1_rolling_5m_base)}</td><td>{val(r.features_asof.obi_l1)}</td>
       <td>{val(r.features_asof.spread_bps)}</td><td>{val(r.features_asof.momentum_previous_60s_bps)}</td>
       <td>{r.source_age_ms}</td></tr>)}</tbody></table>
      <p>Liquidity recovery outcomes remain separate post-hoc v34 diagnostics, not entry-time predictors.</p>
    </div>}
    {section==='execution'&&<div className="pro-detail-scroll">
      <p>Frozen displayed ten-level book walk. Fees and non-book slippage are explicit unverified assumptions; spread is already in VWAP. No realized fills.</p>
      <table><thead><tr><th>Scenario</th><th>Side</th><th>Size</th><th>Indicative VWAP</th><th>Uncovered</th><th>Fee bps</th><th>Extra bps</th><th>Est. total bps</th><th>Book fraction</th></tr></thead>
      <tbody>{(visible.samples??[]).flatMap(x=>x.scenarios).map((s,i)=><tr key={i}>
       <td>{s.scenario}</td><td>{s.side}</td><td>{s.requested_base}</td>
       <td>{s.status.startsWith('CENSORED')?'CENSORED':val(s.reference_vwap)}</td>
       <td>{s.uncovered_base}</td><td>{s.fee_bps_assumed}</td><td>{s.extra_non_book_slippage_bps_assumed}</td><td>{val(s.total_indicative_taker_cost_bps_assumed)}</td><td>{s.displayed_depth_fraction_assumed}</td>
      </tr>)}</tbody></table>
      <p>{visible.twap_status?.replaceAll('_',' ')??'TWAP not evaluated'}. This is NOT executable PnL.</p>
    </div>}
    <p className="q7-provenance">EXPERIMENT Q7-OFI-LIQUIDITY-RECOVERY-001 · SOURCE {visible.source_sha256.slice(0,16)}… · REPORT {visible.report_sha256.slice(0,16)}… · AS-OF {new Date(visible.asof_receiver_ms).toISOString()}</p>
  </>}
 </article>;
}
