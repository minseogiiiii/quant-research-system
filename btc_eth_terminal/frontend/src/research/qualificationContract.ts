type Obj=Record<string,unknown>;
const need=(ok:unknown,reason:string)=>{if(!ok)throw Error(reason);};
function obj(v:unknown):Obj{need(v!==null&&typeof v==='object'&&!Array.isArray(v),'Invalid qualification object');return v as Obj;}
function hash(v:unknown):string{need(typeof v==='string'&&/^[0-9a-f]{64}$/.test(v),'Invalid evidence digest');return v as string;}
function count(v:unknown,max:number):number{need(typeof v==='number'&&Number.isSafeInteger(v)&&v>=0&&v<=max,'Invalid count');return v as number;}
const mandatory=['SAME_POLICY_HOLDOUT_UNVERIFIED','REAL_FILL_COST_UNCALIBRATED','LIVE_RESILIENCE_UNVERIFIED','MANUAL_APPROVAL_UNAVAILABLE'] as const;
const known=new Set<string>([...mandatory,'SYNTHETIC_ONLY','INCOMPLETE_TRIAL_INVENTORY','TRIAL_FAILED','NO_COMPLETED_TRADES','NONPOSITIVE_DEVELOPMENT_RETURN','POSITIVE_DEVELOPMENT_ONLY']);
export type Trial={id:string,product:'BTC-USD'|'ETH-USD',hold:number,status:'COMPLETE'|'FAILED',verdict:'REJECTED_DEVELOPMENT'|'BLOCKED',mean:number|null,trades:number,reasons:string[]};
export type Qualification={source:'SYNTHETIC_TEST_ONLY'|'DEVELOPMENT',plan:string,head:string,report:string,catalog:string,sessions:number,trials:Trial[]};
export function decodeQualification(value:unknown):Qualification{
 const outer=obj(value),x=outer.schema==='h6_registered_strategy_run_v1'?obj(outer.qualification):outer;
 need(x.schema==='h8_strategy_qualification_v1'&&x.status==='NO_APPROVAL'&&x.registry_verified===true,'Unsupported qualification report');
 for(const k of ['same_policy_oos_verified','statistical_qualification','shadow_authorized','paper_authorized','oos_approved','alpha_confirmed','execution_authorized'])need(x[k]===false,'Approval claim rejected');
 need(x.source_kind==='DEVELOPMENT'||x.source_kind==='SYNTHETIC_TEST_ONLY','Unsupported source kind');
 const source=x.source_kind as Qualification['source'],plan=hash(x.plan_sha256),head=hash(x.registry_head_sha256),report=hash(x.development_report_sha256),catalog=hash(x.dataset_catalog_sha256);
 if(outer!==x){need(outer.plan_sha256===plan&&outer.registry_head_sha256===head,'Wrapper evidence mismatch');for(const k of ['oos_approved','alpha_confirmed','execution_authorized','actual_fills_verified'])need(outer[k]===false,'Wrapper approval claim rejected');}
 const sessions=count(x.session_count,16),n=count(x.trial_count,8);need(sessions>0&&n>0&&Array.isArray(x.trials)&&x.trials.length===n,'Invalid inventory');
 const seen=new Set<string>();
 const trials=(x.trials as unknown[]).map((v):Trial=>{
  const t=obj(v),id=hash(t.trial_id),p=obj(t.parameters);need(!seen.has(id),'Duplicate trial');seen.add(id);
  need(p.product==='BTC-USD'||p.product==='ETH-USD','Invalid product');need(typeof p.hold_ms==='number'&&[5000,10000,30000,60000].includes(p.hold_ms),'Invalid horizon');
  need(t.trial_status==='COMPLETE'||t.trial_status==='FAILED','Invalid trial status');
  const status=t.trial_status as Trial['status'],trades=count(t.closed_roundtrips,3200000);
  need(status==='FAILED'?t.mean_session_net_return===null&&trades===0:typeof t.mean_session_net_return==='number'&&Number.isFinite(t.mean_session_net_return)&&t.mean_session_net_return>-1,'Invalid return');
  const mean=t.mean_session_net_return as number|null;
  need(Array.isArray(t.reasons)&&t.reasons.length<=known.size&&t.reasons.every(r=>typeof r==='string'&&known.has(r)),'Invalid reasons');
  const reasons=t.reasons as string[];need(new Set(reasons).size===reasons.length&&mandatory.every(r=>reasons.includes(r)),'Missing blocker');
  need(reasons.includes('SYNTHETIC_ONLY')===(source==='SYNTHETIC_TEST_ONLY'),'Source blocker mismatch');
  const expected=status==='FAILED'?'TRIAL_FAILED':trades===0?'NO_COMPLETED_TRADES':mean!<=0?'NONPOSITIVE_DEVELOPMENT_RETURN':'POSITIVE_DEVELOPMENT_ONLY';
  need(reasons.includes(expected),'Economic reason mismatch');
  const verdict=source==='DEVELOPMENT'&&status==='COMPLETE'&&trades>0&&mean!<=0?'REJECTED_DEVELOPMENT':'BLOCKED';
  need(t.verdict===verdict,'Verdict mismatch');
  return {id,product:p.product as Trial['product'],hold:p.hold_ms as number,status,verdict,mean,trades,reasons};
 });
 need(trials.every(t=>t.reasons.includes('INCOMPLETE_TRIAL_INVENTORY')===trials.some(a=>a.status==='FAILED')),'Incomplete trial inventory mismatch');
 return {source,plan,head,report,catalog,sessions,trials};
}
