import test from 'node:test';
import assert from 'node:assert/strict';
import {
  makeResearchDraft, evaluateEvidenceGate, matchingDraft,
} from '../src/researchWorkflow.ts';

const a='a'.repeat(64),b='b'.repeat(64),c='c'.repeat(64);
const obs=Object.freeze({
  product:'BTC-USD',asof:1795000000000,sourceSha:a,projectionSha:b,
});
function draft(){return makeResearchDraft(obs,{
  feature:'ofi_l1_rolling_5m_base',threshold:'0',expectation:'UP',
})}
function response(){
  return {
    schema:'q7_factor_lab_v1',product:'BTC-USD',section:'research',
    mode:'ARCHIVE_REPLAY',asof_receiver_ms:obs.asof,
    source_sha256:a,report_sha256:c,verdict:'INSUFFICIENT_EVIDENCE',
    oos_status:'NOT_OPENED_NEW_INDEPENDENT_MULTI_DAY_DATA_REQUIRED',
    real_orders_enabled:false,primary_horizon_ms:60000,
    predictive_effect:null,cost_adjusted_pnl:null,trials_executed_on_blind:0,
    summary:{
      feature_rows:15,primary_60s_future_mid_labels:12,
      oos_status:'INSUFFICIENT_EVIDENCE_ONE_PREVIOUSLY_EXPLORED_HOUR',
      out_of_sample_predictive_effect:null,cost_adjusted_return_bps:null,
    },
  };
}
test('stable immutable-source-bound draft with no market orders or result',()=>{
  const d=draft();
  assert.deepEqual(d,draft());
  assert.equal(d.schema,'q20_research_draft_v1');
  assert.equal(d.status,'EXPLORATORY_DRAFT_NOT_PREREGISTERED');
  assert.equal(d.source.manifest_sha256,a);
  assert.equal(d.source.projection_sha256,b);
  assert.equal(d.source.asof_receiver_ms,obs.asof);
  assert.equal(d.hypothesis.outcome_horizon_ms,60000);
  assert.equal(d.constraints.trading_enabled,false);
  assert.equal(d.constraints.preregistration_proved,false);
  assert.equal(d.constraints.independent_blind_oos,false);
  assert.equal('future_only_diagnostic_labels' in JSON.stringify(d),false);
});
test('draft snapshot rejects changed source product or archived as-of',()=>{
  const d=draft();
  assert.equal(matchingDraft(d,obs),true);
  assert.equal(matchingDraft(d,{...obs,product:'ETH-USD'}),false);
  assert.equal(matchingDraft(d,{...obs,asof:obs.asof+300000}),false);
  assert.equal(matchingDraft(d,{...obs,projectionSha:c}),false);
  assert.equal(matchingDraft(null,obs),false);
});
test('rejects untrusted source hashes, product and invalid timestamps',()=>{
  for(const change of [
    {sourceSha:'bogus'}, {projectionSha:'notsha'}, {product:'SOL-USD'},
    {asof:0},{asof:3.14},{asof:Number.POSITIVE_INFINITY},
  ])assert.throws(()=>makeResearchDraft({...obs,...change},{
    feature:'ofi_l1_rolling_5m_base',threshold:'0',expectation:'UP',
  }));
});
test('rejects malformed and unbounded parameters before any network request',()=>{
  for(const bad of [
    {feature:'secret_label'}, {threshold:''}, {threshold:'Infinity'},
    {threshold:'0; DROP TABLE'}, {threshold:'1e309'}, {threshold:'1001'},
    {threshold:'3.123456789'}, {expectation:'LONG'},
  ])assert.throws(()=>makeResearchDraft(obs,{
    feature:'ofi_l1_rolling_5m_base',threshold:'0',expectation:'UP',...bad,
  }));
  assert.throws(()=>makeResearchDraft(obs,{
    feature:'obi_l1',threshold:'1.01',expectation:'DOWN',
  }));
  assert.equal(makeResearchDraft(obs,{
    feature:'obi_l1',threshold:'-0.5',expectation:'DOWN',
  }).hypothesis.feature,'obi_l1');
});
test('evidence-gate returns only descriptive admission, never OOS alpha',()=>{
  const g=evaluateEvidenceGate(draft(),response());
  assert.equal(g.verdict,'INSUFFICIENT_EVIDENCE');
  assert.equal(g.archived_feature_rows,15);
  assert.equal(g.historically_available_60s_labels,12);
  assert.equal(g.oos_approved,false);
  assert.equal(g.tradeable_alpha_confirmed,false);
  assert.equal(g.orders_enabled,false);
  assert.equal(g.factor_report_sha256,c);
  assert.equal('pnl' in g,false);
  assert.equal('future_only_diagnostic_labels' in g,false);
});
test('aborted prior instrument or as-of response cannot be promoted',()=>{
  for(const change of [
    {product:'ETH-USD'},
    {asof_receiver_ms:obs.asof+60000},
    {source_sha256:b},
    {section:'execution'},
    {mode:'LIVE_SOURCE'},
    {report_sha256:'invalid'},
  ])assert.throws(()=>evaluateEvidenceGate(draft(),{...response(),...change}));
});
test('rejects any invented prediction, PnL, blind OOS or order authorization',()=>{
  const r=response();
  for(const change of [
    {predictive_effect:0.02},{cost_adjusted_pnl:22},
    {trials_executed_on_blind:1},{real_orders_enabled:true},
    {verdict:'VALIDATED_ALPHA'},{primary_horizon_ms:30000},
    {oos_status:'PASSED_INDEPENDENT_OOS'},
    {future_only_diagnostic_labels:[22]},
  ])assert.throws(()=>evaluateEvidenceGate(draft(),{...r,...change}));
});
test('bad historical evidence count and after-cutoff label inflation fail closed',()=>{
  for(const summary of [
    {feature_rows:-1},{feature_rows:1,primary_60s_future_mid_labels:2},
    {primary_60s_future_mid_labels:1.5},
    {out_of_sample_predictive_effect:0},
    {cost_adjusted_return_bps:1.3},
    {oos_status:'VALIDATED'},
  ])assert.throws(()=>evaluateEvidenceGate(draft(),{
    ...response(),summary:{...response().summary,...summary},
  }));
  assert.throws(()=>evaluateEvidenceGate(draft(),null));
  assert.throws(()=>evaluateEvidenceGate(draft(),[]));
});
test('zero available diagnostic rows is still insufficient, never treated as a pass',()=>{
  const gate=evaluateEvidenceGate(draft(),{
    ...response(),summary:{...response().summary,feature_rows:0,primary_60s_future_mid_labels:0},
  });
  assert.equal(gate.archived_feature_rows,0);
  assert.equal(gate.oos_approved,false);
});
