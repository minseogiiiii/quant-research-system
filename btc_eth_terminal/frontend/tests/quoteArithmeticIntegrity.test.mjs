import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {validateQuoteAgainstDepth} from '../src/marketStateIntegrity.ts';
function quote(){
 const bids=Array.from({length:10},(_,i)=>({price:String(100-i),quantity:'2',cumulative_base:String((i+1)*2)}));
 const asks=Array.from({length:10},(_,i)=>({price:String(101+i),quantity:'1',cumulative_base:String(i+1)}));
 return {depth:{bids,asks,bid_total_base_10:'20',ask_total_base_10:'10'},
  best_bid:'100',best_ask:'101',best_bid_qty:'2',best_ask_qty:'1',spread:'1',
  microprice:'100.6666666666666666666666667',best_imbalance:'0.3333333333333333333333333333'};
}
test('derived microprice requires Python Decimal28 arithmetic, not only inside-spread',()=>{
 const r=quote();assert.equal(validateQuoteAgainstDepth(r),true);
 r.microprice='100.5'; // plausible but inconsistent with 2 bid size / 1 ask size
 assert.equal(validateQuoteAgainstDepth(r),false);
});
test('displayed imbalance must equal source best quantities',()=>{
 const r=quote();assert.equal(validateQuoteAgainstDepth(r),true);
 r.best_imbalance='0'; // still inside [-1,1], but mathematically false
 assert.equal(validateQuoteAgainstDepth(r),false);
});
test('numeric reformatting is permitted but nonzero extra decimal is not',()=>{
 const r=quote();r.best_imbalance='0.3333333333333333333333333333000';
 assert.equal(validateQuoteAgainstDepth(r),true);
 r.best_imbalance='0.3333333333333333333333333334';
 assert.equal(validateQuoteAgainstDepth(r),false);
});
test('infinity, NaN, signed out-of-bound imbalance fail closed',()=>{
 for(const value of ['NaN','Infinity','-2','1.000000000000000000000000001']){
  const r=quote();r.best_imbalance=value;
  assert.equal(validateQuoteAgainstDepth(r),false,value);
 }
});
test('verified real Coinbase BTC/ETH archive rows preserve exact Decimal28 derived values',t=>{
 if(!process.env.Q5_REAL_STATE_JSONL){t.skip('Explicit genuine Coinbase state fixture not supplied');return;}
 const rows=readFileSync(process.env.Q5_REAL_STATE_JSONL,'utf8')
   .trim().split('\n').map(JSON.parse);
 const counts={'BTC-USD':0,'ETH-USD':0};
 for(const [i,r] of rows.entries()){
  assert.equal(validateQuoteAgainstDepth(r),true,`archive state #${i} ${r.product}`);
  counts[r.product]++;
 }
 assert.deepEqual(counts,{'BTC-USD':112,'ETH-USD':116});
});
