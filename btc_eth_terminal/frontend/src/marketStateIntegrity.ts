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

/** Best bid/ask size imbalance is mathematically bounded to [-1, 1].
 * Do not use Number() for boundary validation because fractional excess over
 * 1 can round to exactly 1 in IEEE-754. This is a UI admission check only.
 */
export function validateBestImbalance(value:unknown):boolean {
  if(typeof value!=='string'||value.length===0||value.length>96)return false;
  const match=/^-?(0|[1-9]\d*)(?:\.(\d+))?$/.exec(value);
  if(!match)return false;
  return match[1]==='0'||(match[1]==='1' && (match[2]===undefined||/^0+$/.test(match[2])));
}


/** Reproduce the Python Decimal context used by the frozen v35 _features().
 * Python rounds every arithmetic operation to 28 significant digits using
 * ROUND_HALF_EVEN. Reconstructing that contract avoids a float-based tolerance
 * and rejects mismatched but superficially in-spread microprices/imbalances.
 * This verifies internal source arithmetic only, not exchange truth.
 */
const PYTHON_DECIMAL_PRECISION = 28;
function rounded28(v:DecimalValue):DecimalValue {
  const digits=(v.coefficient<0n?-v.coefficient:v.coefficient).toString().length;
  if(v.coefficient===0n||digits<=PYTHON_DECIMAL_PRECISION)return v;
  const step=10n**BigInt(digits-PYTHON_DECIMAL_PRECISION);
  const sign=v.coefficient<0n?-1n:1n;
  const absolute=v.coefficient<0n?-v.coefficient:v.coefficient;
  let quotient=absolute/step;
  const rem=absolute%step;
  if(2n*rem>step||(2n*rem===step&&quotient%2n===1n))quotient++;
  return {coefficient:sign*quotient,scale:v.scale-(digits-PYTHON_DECIMAL_PRECISION)};
}
function pythonAdd(a:DecimalValue,b:DecimalValue):DecimalValue{return rounded28(sum(a,b));}
function pythonSubtract(a:DecimalValue,b:DecimalValue):DecimalValue {
  return pythonAdd(a,{coefficient:-b.coefficient,scale:b.scale});
}
function pythonMultiply(a:DecimalValue,b:DecimalValue):DecimalValue {
  return rounded28({coefficient:a.coefficient*b.coefficient,scale:a.scale+b.scale});
}
function pythonDivide(a:DecimalValue,b:DecimalValue):DecimalValue|null {
  if(b.coefficient===0n)return null;
  if(a.coefficient===0n)return {coefficient:0n,scale:0};
  const sign=(a.coefficient<0n)!==(b.coefficient<0n)?-1n:1n;
  const abs=(v:bigint)=>v<0n?-v:v;
  const numerator=abs(a.coefficient)*10n**BigInt(Math.max(0,b.scale));
  const denominator=abs(b.coefficient)*10n**BigInt(Math.max(0,a.scale));
  // The source parser yields nonnegative scales; rounded intermediates may
  // have negative scales, so transfer their negative power to the other side.
  const n=numerator*10n**BigInt(Math.max(0,-a.scale));
  const d=denominator*10n**BigInt(Math.max(0,-b.scale));
  let magnitude=n.toString().length-d.toString().length;
  if(magnitude>=0){if(n<d*10n**BigInt(magnitude))magnitude--;}
  else if(n*10n**BigInt(-magnitude)<d)magnitude--;
  const scale=PYTHON_DECIMAL_PRECISION-1-magnitude;
  const expandedNum=n*10n**BigInt(Math.max(0,scale));
  const expandedDen=d*10n**BigInt(Math.max(0,-scale));
  let quotient=expandedNum/expandedDen;
  const rem=expandedNum%expandedDen;
  if(2n*rem>expandedDen||(2n*rem===expandedDen&&quotient%2n===1n))quotient++;
  return {coefficient:sign*quotient,scale};
}
function signedDecimal(value:unknown):DecimalValue|null {
  if(typeof value==='string'&&value.startsWith('-')){
    const v=decimal(value.slice(1));
    return v===null?null:{coefficient:-v.coefficient,scale:v.scale};
  }
  return decimal(value);
}

/** Cross-check the source's displayed quote with its independently validated
 * ten-level price ladder. Decimal comparisons are exact; no floats or future
 * source records are consulted. This detects tampered or inconsistent fields,
 * not an incorrect but internally self-consistent exchange feed.
 */
export function validateQuoteAgainstDepth(row:unknown):boolean {
  if(row===null||typeof row!=='object'||Array.isArray(row))return false;
  const r=row as Record<string,unknown>;
  if(!validateDepth10(r.depth))return false;
  const book=r.depth as Depth10;
  const bid=decimal(r.best_bid),ask=decimal(r.best_ask),
    bidQty=decimal(r.best_bid_qty),askQty=decimal(r.best_ask_qty),
    spread=decimal(r.spread),micro=decimal(r.microprice);
  const ladderBid=decimal(book.bids[0].price),ladderAsk=decimal(book.asks[0].price),
    ladderBidQty=decimal(book.bids[0].quantity),ladderAskQty=decimal(book.asks[0].quantity);
  if(!positive(bid)||!positive(ask)||!positive(bidQty)||!positive(askQty)||
     !positive(spread)||!positive(micro)||
     !positive(ladderBid)||!positive(ladderAsk)||
     !positive(ladderBidQty)||!positive(ladderAskQty))return false;
  const imbalance=signedDecimal(r.best_imbalance);
  if(!validateBestImbalance(r.best_imbalance)||imbalance===null)return false;
  const total=pythonAdd(bidQty,askQty);
  const expectedMicro=pythonDivide(pythonAdd(pythonMultiply(ask,bidQty),
    pythonMultiply(bid,askQty)),total);
  const expectedImbalance=pythonDivide(pythonSubtract(bidQty,askQty),total);
  return expectedMicro!==null&&expectedImbalance!==null&&
    cmp(bid,ladderBid)===0&&cmp(ask,ladderAsk)===0&&
    cmp(bidQty,ladderBidQty)===0&&cmp(askQty,ladderAskQty)===0&&
    cmp(sum(bid,spread),ask)===0&&
    cmp(micro,bid)>=0&&cmp(micro,ask)<=0&&
    cmp(micro,expectedMicro)===0&&cmp(imbalance,expectedImbalance)===0;
}

/** Prevent malformed or forged trade-tape fields from reaching React's
 * .slice(), .reverse() and Date.toISOString() render path. Trades are the
 * collector's last <=24 observed prints, not aggressor-side classifications.
 */
export function validateTradeTape(trades:unknown,observedAsOfMs:unknown):boolean {
  const MAX_JS_DATE_MS=8_640_000_000_000_000;
  if(!Number.isSafeInteger(observedAsOfMs)||
     (observedAsOfMs as number)<1||(observedAsOfMs as number)>MAX_JS_DATE_MS||
     !Array.isArray(trades)||trades.length>24)return false;
  for(const t of trades){
    if(t===null||typeof t!=='object'||Array.isArray(t))return false;
    const r=t as Record<string,unknown>;
    if(!positive(decimal(r.price))||!positive(decimal(r.size))||
       (r.maker_side!=='BUY'&&r.maker_side!=='SELL')||
       !Number.isSafeInteger(r.received_at_ms)||(r.received_at_ms as number)<1||
       (r.received_at_ms as number)>(observedAsOfMs as number)||
       (r.trade_id!==undefined&&r.trade_id!==null&&
        (typeof r.trade_id!=='string'||!/^\d{1,64}$/.test(r.trade_id))))return false;
  }
  return true;
}
