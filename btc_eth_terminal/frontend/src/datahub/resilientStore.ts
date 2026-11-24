import {validateQuoteAgainstDepth,validateTradeTape} from '../marketStateIntegrity.js';

export const PRODUCTS=['BTC-USD','ETH-USD'] as const;
export type Product=typeof PRODUCTS[number];
type Obj=Record<string,unknown>;
export type Quote=Obj & {
  product:Product; session_id:string; run_id:string; connection_id:number;
  state_version:number; source_sequence:number; receiver_time_ms:number;
  source_last_l2_receiver_ms:number; source_raw_sha256:string;
  best_bid:string; best_ask:string;
};
export type Frame={
  gateway_run:string; generation:number; source_session:string;
  active_source:'primary'|'secondary'; states:Record<Product,Quote>;
};
export type View={status:'BLOCKED'|'READY';reason:string;frame:Frame|null};
const rec=(x:unknown):x is Obj=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const int=(x:unknown):x is number=>typeof x==='number'&&Number.isSafeInteger(x);
const hex=(x:unknown,n:number):x is string=>typeof x==='string'&&x.length===n&&/^[0-9a-f]+$/.test(x);
const fail=(reason:string):never=>{throw new Error(reason);};
const fresh=(q:Quote,now:number)=>int(now)&&now>=q.receiver_time_ms&&
  now>=q.source_last_l2_receiver_ms&&now-q.source_last_l2_receiver_ms<=3000;
function quote(x:unknown,p:Product,session:string,now:number):Quote{
  if(!rec(x)||x.schema!=='v36_source_state_v1'||x.product!==p||
    x.session_id!==session||!hex(x.run_id,32)||
    x.mode!=='LIVE_SOURCE'||x.live_feed!==true||
    x.trading_enabled!==false||x.execution_authorized!==false||
    x.independent_exchange_attestation!==false||x.status!=='VALID_OBSERVED_QUOTE'||
    !int(x.connection_id)||x.connection_id<0||!int(x.state_version)||x.state_version<1||
    !int(x.source_sequence)||x.source_sequence<0||!int(x.receiver_time_ms)||x.receiver_time_ms<1||
    !int(x.source_last_l2_receiver_ms)||x.source_last_l2_receiver_ms<1||
    x.receiver_time_ms<x.source_last_l2_receiver_ms||!hex(x.source_raw_sha256,64)||
    !validateQuoteAgainstDepth(x)||!validateTradeTape(x.recent_trades,x.receiver_time_ms))fail('ROW_INVALID');
  const q=x as Quote;
  if(!fresh(q,now))fail('ROW_STALE');
  return q;
}
export function validateFrame(x:unknown,now:number,cursor?:string):Frame{
  if(!int(now)||now<1||!rec(x)||x.schema!=='q292_resilient_route_v1'||
    x.mode!=='LIVE_READ_ONLY_CANDIDATE'||x.status!=='HEALTHY'||
    x.real_orders_enabled!==false||x.execution_authorized!==false||
    x.exchange_attested!==false||x.zero_gap_certified!==false||x.historical_gap_recovered!==false||
    !hex(x.gateway_run,32)||!int(x.generation)||x.generation<1||!hex(x.source_session,32)||
    (x.active_source!=='primary'&&x.active_source!=='secondary')||
    !rec(x.states)||Object.keys(x.states).length!==2)fail('ROUTE_INVALID');
  const v=x as Obj,run=v.gateway_run as string,generation=v.generation as number;
  const session=v.source_session as string,states=v.states as Obj;
  if(cursor!==undefined&&cursor!==run+':'+generation+':'+session)fail('CURSOR_MISMATCH');
  const btc=quote(states['BTC-USD'],'BTC-USD',session,now),eth=quote(states['ETH-USD'],'ETH-USD',session,now);
  if(btc.run_id!==eth.run_id||btc.connection_id!==eth.connection_id)fail('PAIR_IDENTITY');
  return {gateway_run:run,generation,source_session:session,
    active_source:v.active_source as Frame['active_source'],states:{'BTC-USD':btc,'ETH-USD':eth}};
}
const key=(f:Frame)=>f.gateway_run+':'+f.generation+':'+f.source_session;
function stable(x:unknown):string{
  if(Array.isArray(x))return '['+x.map(stable).join(',')+']';
  if(rec(x))return '{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+stable(x[k])).join(',')+'}';
  return JSON.stringify(x)??'undefined';
}
function freeze(v:unknown):void{
  if(v&&typeof v==='object'){Object.values(v).forEach(freeze);Object.freeze(v);}
}
export class MarketStateStore{
  private view:View=Object.freeze({status:'BLOCKED',reason:'BOOT',frame:null});
  private listeners=new Set<()=>void>();
  private last:Frame|null=null;
  private admission=0;
  private expiry=0;
  private clock=-1;
  private retiredRuns=new Set<string>();
  private retiredSessions=new Set<string>();
  getSnapshot=()=>this.view;
  subscribe=(fn:()=>void)=>{this.listeners.add(fn);return ()=>{this.listeners.delete(fn);};};
  private publish(v:View){this.view=Object.freeze(v);for(const fn of [...this.listeners])fn();}
  begin(){this.admission++;this.publish({status:'BLOCKED',reason:'HANDSHAKE',frame:null});return this.admission;}
  block(token:number,reason:string){
    if(token!==this.admission)return;
    this.admission++;this.publish({status:'BLOCKED',reason,frame:null});
  }
  check(now:number,mono:number){
    if(!Number.isFinite(mono)||mono<this.clock||this.view.frame&&
      (mono>this.expiry||!PRODUCTS.every(p=>fresh(this.view.frame!.states[p],now)))){
      this.block(this.admission,'FRESHNESS_EXPIRED');return false;
    }
    this.clock=mono;return this.view.status==='READY';
  }
  accept(token:number,raw:unknown,now:number,mono:number,kind:'handshake'|'snapshot',cursor?:string):boolean{
    if(token!==this.admission)return false;
    try{
      if(!Number.isFinite(mono)||mono<this.clock)fail('MONOTONIC_REGRESSION');
      if(kind==='snapshot'&&typeof cursor!=='string')fail('MISSING_CURSOR');
      const frame=validateFrame(raw,now,cursor),prior=this.last;
      if(kind==='snapshot'&&(!this.view.frame||key(frame)!==key(this.view.frame)))fail('HANDSHAKE_REQUIRED');
      if(this.retiredRuns.has(frame.gateway_run)||this.retiredSessions.has(frame.source_session))fail('RETIRED_IDENTITY');
      if(prior&&prior.gateway_run===frame.gateway_run){
        if(frame.generation<prior.generation)fail('GENERATION_REGRESSION');
        if(frame.generation===prior.generation){
          if(key(frame)!==key(prior)||frame.active_source!==prior.active_source)fail('IDENTITY_CONFLICT');
          for(const p of PRODUCTS){
            const a=prior.states[p],b=frame.states[p];
            if(a.run_id!==b.run_id||a.connection_id!==b.connection_id||
              b.state_version<a.state_version||b.source_sequence<a.source_sequence||
              b.receiver_time_ms<a.receiver_time_ms||
              (b.state_version===a.state_version&&stable(a)!==stable(b)))fail('VERSION_CONFLICT');
          }
        }else{
          if(frame.source_session===prior.source_session)fail('GENERATION_SESSION_REUSE');
          if(this.retiredSessions.size>=512)fail('SESSION_HISTORY_LIMIT');
          this.retiredSessions.add(prior.source_session);
        }
      }else if(prior){
        if(kind!=='handshake'||frame.source_session===prior.source_session)fail('HANDSHAKE_REQUIRED');
        if(this.retiredRuns.size>=128||this.retiredSessions.size>=512)fail('HISTORY_LIMIT');
        this.retiredRuns.add(prior.gateway_run);this.retiredSessions.add(prior.source_session);
      }
      const copy=structuredClone(frame);freeze(copy);
      this.last=copy;this.clock=mono;
      this.expiry=mono+Math.min(...PRODUCTS.map(p=>3000-(now-copy.states[p].source_last_l2_receiver_ms)));
      this.publish({status:'READY',reason:'VALIDATED',frame:copy});return true;
    }catch(e){this.block(token,e instanceof Error?e.message:'INVALID_FRAME');return false;}
  }
}
