/**
 * Shared read-only market-depth integrity gate for historical and future
 * streamed snapshots. Never upgrades provenance or grants trading authority.
 * All quantities are compared with exact decimal arithmetic, not IEEE-754.
 */
export type DepthLevel = {price:string;quantity:string;cumulative_base:string};
export type Depth10 = {bids:DepthLevel[];asks:DepthLevel[];bid_total_base_10:string;ask_total_base_10:string};

type DecimalValue = {coefficient:bigint; scale:number};
const MAX_DECIMAL_CHARACTERS = 96;
const MAX_EXPONENT = 80;
const DECIMAL_PATTERN = /^(?:0|[1-9]\d*)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/;

function decimal(value:unknown):DecimalValue|null {
  if(typeof value!=='string'||value.length===0||value.length>MAX_DECIMAL_CHARACTERS)return null;
  const match=DECIMAL_PATTERN.exec(value);
  if(!match)return null;
  const exponent=match[2]===undefined?0:Number(match[2]);
  if(!Number.isInteger(exponent)||Math.abs(exponent)>MAX_EXPONENT)return null;
  const whole=value.split(/[.eE]/,1)[0];
  const fraction=match[1]??'';
  let coefficient=BigInt(whole+fraction);
  let scale=fraction.length-exponent;
  if(scale<0){coefficient*=10n**BigInt(-scale);scale=0;}
  while(scale>0&&coefficient%10n===0n){coefficient/=10n;scale--;}
  return {coefficient,scale};
}
function positive(value:DecimalValue|null):value is DecimalValue {return value!==null&&value.coefficient>0n;}
function cmp(a:DecimalValue,b:DecimalValue):number {
  const scale=Math.max(a.scale,b.scale);
  const x=a.coefficient*10n**BigInt(scale-a.scale);
  const y=b.coefficient*10n**BigInt(scale-b.scale);
  return x<y?-1:x>y?1:0;
}
function sum(a:DecimalValue,b:DecimalValue):DecimalValue {
  const scale=Math.max(a.scale,b.scale);
  return {coefficient:a.coefficient*10n**BigInt(scale-a.scale)+b.coefficient*10n**BigInt(scale-b.scale),scale};
}
function validateSide(levels:unknown,descending:boolean):{top:DecimalValue;total:DecimalValue}|null {
  if(!Array.isArray(levels)||levels.length!==10)return null;
  let top:DecimalValue|null=null,previous:DecimalValue|null=null;
  let running:DecimalValue={coefficient:0n,scale:0};
  for(const level of levels){
    if(level===null||typeof level!=='object'||Array.isArray(level))return null;
    const p=decimal(level.price),q=decimal(level.quantity),c=decimal(level.cumulative_base);
    if(!positive(p)||!positive(q)||!positive(c))return null;
    if(previous!==null&&(descending?cmp(p,previous)>=0:cmp(p,previous)<=0))return null;
    running=sum(running,q);
    if(cmp(running,c)!==0)return null;
    if(top===null)top=p;
    previous=p;
  }
  return top===null?null:{top,total:running};
}

/** Reject invalid/tampered ten-level books, including contradictory side totals. */
export function validateDepth10(depth:unknown):depth is Depth10 {
  if(depth===null||typeof depth!=='object'||Array.isArray(depth))return false;
  const book=depth as Record<string,unknown>;
  const bids=validateSide(book.bids,true),asks=validateSide(book.asks,false);
  const bidTotal=decimal(book.bid_total_base_10),askTotal=decimal(book.ask_total_base_10);
  return bids!==null&&asks!==null&&positive(bidTotal)&&positive(askTotal)
    &&cmp(bids.top,asks.top)<0
    &&cmp(bids.total,bidTotal)===0&&cmp(asks.total,askTotal)===0;
}
