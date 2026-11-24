const obj=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v);
const n=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>=0;
const need=(v:unknown,r:string)=>{if(!v)throw Error(r);};
const finite=(v:unknown):v is number=>typeof v==='number'&&Number.isFinite(v);
const hex=(v:unknown):v is string=>typeof v==='string'&&/^[0-9a-f]{64}$/.test(v);
type Group={n:number;mean:number|null};
export type Metrics={n:number;ic:number|null;rank:number|null;direction:number|null;directionPairs:number;groups:Group[]};
export type Result={horizon:number;grid:number;causal:number;paired:number;missing:number;disjoint:number;features:{name:string;all:Metrics;separated:Metrics}[]};
export type Diagnostic={product:'BTC-USD'|'ETH-USD';pin:string;hash:string;results:Result[]};
const names=['ofi_base','imbalance','microprice_edge_bps'];
function metric(x:unknown,total:number):Metrics{
 if(!obj(x)||x.n!==total||!n(x.nonzero_direction_pairs)||x.nonzero_direction_pairs>total)throw Error('METRIC_COUNTS');
 function bounded(v:unknown,min:number,max:number,eligible:boolean):number|null{
  if(v===null)return null;
  if(!eligible||!finite(v)||v<min||v>max)throw Error('METRIC_RANGE');
  return v;
 }
 const ic=bounded(x.pearson_ic,-1,1,total>=20),rank=bounded(x.rank_ic,-1,1,total>=20),direction=bounded(x.direction_agreement,0,1,x.nonzero_direction_pairs>=20);
 if(!obj(x.conditional_markout))throw Error('GROUPS');
 const groups:Group[]=[];
 for(const k of ['negative','zero','positive']){
  const g=x.conditional_markout[k];
  if(!obj(g)||!n(g.n)||g.n>total||(g.n===0?g.mean_markout_bps!==null:!finite(g.mean_markout_bps)))throw Error('GROUP_DOMAIN');
  groups.push({n:g.n,mean:g.mean_markout_bps as number|null});
 }
 need(groups.reduce((sum,g)=>sum+g.n,0)===total,'GROUP_TOTAL');
 return {n:total,ic,rank,direction,directionPairs:x.nonzero_direction_pairs,groups};
}
export function decodeDiagnostic(x:unknown):Diagnostic{
 if(!obj(x))throw Error('OBJECT');
 need(x.schema==='f5_predictive_diagnostic_v1'&&x.status==='DESCRIPTIVE_ONLY'&&x.minimum_ic_pairs===20
  &&['p_values_reported','independence_claim','model_selected','oos_approved','alpha_confirmed','execution_authorized'].every(k=>x[k]===false),'RESEARCH_MODE');
 if(!hex(x.source_manifest_sha256)||!hex(x.source_report_sha256)||x.epoch!==x.source_manifest_sha256+':0'||(x.product!=='BTC-USD'&&x.product!=='ETH-USD'))throw Error('SOURCE');
 if(!obj(x.policy)||!Array.isArray(x.policy.horizons_ms)||!Array.isArray(x.results)||x.results.length!==x.policy.horizons_ms.length||x.results.length<1||x.results.length>6)throw Error('HORIZONS');
 const w=x.independent_source_witness;
 if(!obj(w)||w.independent_software_parity!==true||w.oos_approved!==false||w.alpha_confirmed!==false||!n(w.decisions)||!n(w.valid_features)||!n(w.labels)||!n(w.observed_labels)||!n(w.source_messages)||w.source_messages===0)throw Error('WITNESS_CLAIM');
 need(x.disjoint_policy==='GREEDY_EARLIEST_NONOVERLAPPING_FEATURE_TO_LABEL_INTERVAL','DISJOINT_POLICY');
 const results:Result[]=[];let prior=0;
 for(let i=0;i<x.results.length;i++){
  const r=x.results[i];
  if(!obj(r)||!n(r.horizon_ms)||r.horizon_ms<100||r.horizon_ms>60000||r.horizon_ms<=prior||r.horizon_ms!==x.policy.horizons_ms[i]||!n(r.decision_grid_count)||r.decision_grid_count>2000||!n(r.causal_features)||!n(r.paired_labels)||!n(r.missing_labels)||!n(r.disjoint_pairs)||r.disjoint_pairs>r.paired_labels||r.paired_labels+r.missing_labels!==r.causal_features||r.causal_features>r.decision_grid_count||r.decision_grid_count!==w.decisions||r.causal_features!==w.valid_features||!Array.isArray(r.features)||r.features.length!==3)throw Error('HORIZON_COUNTS');
  prior=r.horizon_ms;
  const features=r.features.map((f,j)=>{if(!obj(f)||f.feature!==names[j])throw Error('FEATURE_INVENTORY');return {name:names[j],all:metric(f.all_pairs,r.paired_labels as number),separated:metric(f.disjoint_intervals,r.disjoint_pairs as number)};});
  results.push({horizon:r.horizon_ms,grid:r.decision_grid_count,causal:r.causal_features,paired:r.paired_labels,missing:r.missing_labels,disjoint:r.disjoint_pairs,features});
 }
 need(results.reduce((a,r)=>a+r.causal,0)===w.labels&&results.reduce((a,r)=>a+r.paired,0)===w.observed_labels,'WITNESS_COUNTS');
 return {product:x.product,pin:x.source_manifest_sha256,hash:x.source_report_sha256,results};
}
export function decodeResearch(x:unknown):Diagnostic[]{
 if(obj(x)&&x.schema==='f5_real_research_diagnostic_v1'){
  if(x.status!=='PASS'||x.synthetic!==false||x.raw_data_uploaded!==false||x.oos_approved!==false||x.alpha_confirmed!==false||x.execution_authorized!==false||!obj(x.diagnostics))throw Error('WRAPPER');
  const d=['BTC-USD','ETH-USD'].map(p=>decodeDiagnostic((x.diagnostics as Record<string,unknown>)[p]));
  need(d.every((r,i)=>r.pin===x.source_manifest_sha256&&r.product===['BTC-USD','ETH-USD'][i]),'WRAPPER_SOURCE');
  return d;
 }
 return [decodeDiagnostic(x)];
}
