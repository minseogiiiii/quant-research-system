import React, {useEffect, useMemo, useRef, useState} from 'react';
import {
  HYPOTHESIS_FEATURES, evaluateEvidenceGate, makeResearchDraft, matchingDraft,
  type GateResult, type HypothesisFeature, type Expectation, type Product,
  type ResearchDraft,
} from './researchWorkflow';

const featureNames:Record<HypothesisFeature,string>={
  ofi_l1_rolling_5m_base:'OFI, last 5m (base units)',
  obi_l1:'L1 order-book imbalance (−1 to +1)',
  microprice_displacement_bps:'Microprice displacement (bps)',
};
export default function ResearchWorkflowPanel({product,asof,sourceSha,projectionSha}:{
  product:Product;asof:number|null;sourceSha:string;projectionSha:string;
}) {
  const [feature,setFeature]=useState<HypothesisFeature>('ofi_l1_rolling_5m_base');
  const [threshold,setThreshold]=useState('0');
  const [expectation,setExpectation]=useState<Expectation>('UP');
  const [draft,setDraft]=useState<ResearchDraft|null>(null);
  const [evidence,setEvidence]=useState<{fingerprint:string;gate:GateResult}|null>(null);
  const [error,setError]=useState<string|null>(null);
  const [checking,setChecking]=useState(false);
  const request=useRef<AbortController|null>(null);
  const observed=useMemo(()=>({
    product,asof:asof??-1,sourceSha,projectionSha,
  }),[product,asof,sourceSha,projectionSha]);
  const visibleDraft=matchingDraft(draft,observed)?draft:null;
  const fingerprint=visibleDraft?JSON.stringify(visibleDraft):null;
  const visibleEvidence=evidence&&fingerprint===evidence.fingerprint?evidence.gate:null;
  function invalidate(){
    request.current?.abort();request.current=null;
    setDraft(null);setEvidence(null);setError(null);setChecking(false);
  }
  useEffect(()=>{
    invalidate();
    return()=>request.current?.abort();
  },[product,asof,sourceSha,projectionSha]);
  function createDraft(){
    invalidate();
    try{
      if(asof===null)throw new Error('NO_VERIFIED_ARCHIVED_OBSERVATION');
      setDraft(makeResearchDraft(observed,{feature,threshold,expectation}));
    }catch(e){setError(e instanceof Error?e.message:String(e))}
  }
  async function checkEvidence(){
    if(!visibleDraft||!fingerprint||checking)return;
    request.current?.abort();
    const controller=new AbortController();
    request.current=controller;
    setEvidence(null);setError(null);setChecking(true);
    try{
      const args=new URLSearchParams({
        product:visibleDraft.source.product,
        asof:String(visibleDraft.source.asof_receiver_ms),
        section:'research',
      });
      const response=await fetch('/api/v37/factor-lab?'+args,{
        method:'GET',cache:'no-store',signal:controller.signal,
      });
      if(!response.ok)throw new Error('FACTOR_LAB_UNAVAILABLE_HTTP_'+response.status);
      const raw:unknown=await response.json();
      const gate=evaluateEvidenceGate(visibleDraft,raw);
      if(!controller.signal.aborted&&request.current===controller)
        setEvidence({fingerprint,gate});
    }catch(e){
      if(!controller.signal.aborted&&request.current===controller)
        setError(e instanceof Error?e.message:String(e));
    }finally{
      if(request.current===controller){setChecking(false);request.current=null}
    }
  }
  function exportDraft(){
    if(!visibleDraft)return;
    // This export documents a draft and, optionally, a historical evidence gate.
    // It is deliberately not a preregistration, strategy result or PnL ledger.
    const exportBundle={
      ...visibleDraft,
      evidence_readiness:visibleEvidence??null,
      is_verified_oos_result:false,
      research_artifact_only:true,
    };
    const blob=new Blob([JSON.stringify(exportBundle,null,2)+'\n'],{
      type:'application/json',
    });
    const url=URL.createObjectURL(blob);
    const link=document.createElement('a');
    link.href=url;
    link.download='qrt-research-draft-'+product.toLowerCase()+'-'+observed.asof+'.json';
    document.body.appendChild(link);
    link.click();link.remove();
    window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  return <article className="pro-panel q20-notebook" aria-label="Q20 research workflow notebook">
    <header className="q20-head">
      <div><div className="pro-eyebrow">RESEARCH WORKFLOW / Q20 ENGINEERING CANDIDATE</div>
        <h3>Observe → Hypothesize → Check evidence</h3>
        <p>Research draft only. Previously explored historical archive, never an independent OOS study.</p>
      </div>
      <span className="q20-warning">NO ORDERS · NO ALPHA CLAIM</span>
    </header>
    <div className="q20-steps" aria-label="Research workflow steps">
      <div><span>01 / OBSERVE</span><strong>{product}</strong><small>{asof===null?'No verified cutoff':new Date(asof).toISOString()}</small></div>
      <div><span>02 / HYPOTHESIZE</span><strong>{visibleDraft?'Draft captured':'Draft not captured'}</strong><small>60s future midpoint, exploratory</small></div>
      <div><span>03 / EVIDENCE GATE</span><strong>{visibleEvidence?'OOS BLOCKED':checking?'Checking…':'Not checked'}</strong><small>Read-only immutable Factor Lab</small></div>
    </div>
    <p className="q20-source">Historical source SHA <code>{sourceSha}</code> · Projection SHA <code>{projectionSha}</code></p>
    <div className="q20-editor">
      <label>Feature<select value={feature} onChange={e=>{invalidate();setFeature(e.target.value as HypothesisFeature)}}>
        {HYPOTHESIS_FEATURES.map(f=><option key={f} value={f}>{featureNames[f]}</option>)}
      </select></label>
      <label>Trigger threshold (≥)<input aria-label="Hypothesis trigger threshold" value={threshold}
        onChange={e=>{invalidate();setThreshold(e.target.value)}} inputMode="decimal" maxLength={20}/></label>
      <label>Expected 60s midpoint move<select value={expectation}
        onChange={e=>{invalidate();setExpectation(e.target.value as Expectation)}}>
        <option value="UP">Up</option><option value="DOWN">Down</option>
      </select></label>
    </div>
    <div className="q20-controls">
      <button type="button" disabled={asof===null} onClick={createDraft}>Create exploratory draft</button>
      <button type="button" disabled={!visibleDraft||checking} onClick={()=>void checkEvidence()}>
        {checking?'Checking archived evidence…':'Check evidence readiness'}
      </button>
      <button type="button" disabled={!visibleDraft} onClick={exportDraft}>Export research JSON</button>
    </div>
    {error?<p role="alert" className="pro-alert pro-alert-error">RESEARCH GATE CLOSED · {error}</p>:null}
    {visibleDraft?<div className="q20-draft" aria-label="Captured research hypothesis">
      <strong>Drafted hypothesis:</strong> If {featureNames[visibleDraft.hypothesis.feature]} ≥ {visibleDraft.hypothesis.threshold},
      the next 60-second midpoint move is expected to be {visibleDraft.hypothesis.expected_60s_mid_move.toLowerCase()}.
      <p>This document is tied to the currently selected receiver-asof archive and is not a preregistration.</p>
    </div>:null}
    {visibleEvidence?<div className="q20-result" role="status">
      <strong>INSUFFICIENT EVIDENCE: OOS validation blocked</strong>
      <p>{visibleEvidence.archived_feature_rows} causal feature observations and {visibleEvidence.historically_available_60s_labels} historically available 60s diagnostic labels at this as-of. These are <em>not</em> OOS predictions, independent samples, fitted results or executable PnL.</p>
      <p>Next: obtain untouched multi-day source data, preregister a frozen hypothesis, define chronological train/test boundaries, and test after conservative costs.</p>
      <small>Factor report SHA {visibleEvidence.factor_report_sha256}</small>
    </div>:null}
    <p className="q20-limit">LIVE market observation remains isolated at <a href="/research/v36">/research/v36</a>. This research notebook uses the archived receiver-asof clock. It does not combine a LIVE snapshot with historical outcome labels.</p>
  </article>;
}
