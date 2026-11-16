import test from 'node:test';
import assert from 'node:assert/strict';
import {validateDepth10} from '../src/marketStateIntegrity.ts';
const make=()=>{
 let bid=0n,ask=0n;
 const bids=Array.from({length:10},(_,i)=>{bid+=BigInt(i+1);return {price:String(100-i),quantity:String(i+1)+'.0000',cumulative_base:String(bid)};});
 const asks=Array.from({length:10},(_,i)=>{ask+=BigInt(i+1);return {price:String(101+i),quantity:String(i+1),cumulative_base:String(ask)+'.0'};});
 return {bids,asks,bid_total_base_10:'55.000',ask_total_base_10:'55'};
};
test('valid ladder and differently formatted equal totals are accepted',()=>assert.equal(validateDepth10(make()),true));
test('corrupted bid or ask total is rejected',()=>{
 const x=make();x.bid_total_base_10='999999999';assert.equal(validateDepth10(x),false);
 const y=make();y.ask_total_base_10='55.00000001';assert.equal(validateDepth10(y),false);
});
test('sub-IEEE-754 quantity discrepancy fails closed',()=>{
 const x=make();x.bids[9].cumulative_base='55.00000000000000000000000001';assert.equal(validateDepth10(x),false);
});
test('invalid ordering, negative or zero size, crossed book rejected',()=>{
 const a=make();a.bids[1].price='100';assert.equal(validateDepth10(a),false);
 const b=make();b.asks[0].price='99';assert.equal(validateDepth10(b),false);
 const c=make();c.asks[0].quantity='0';assert.equal(validateDepth10(c),false);
 const d=make();d.bids[0].quantity='-1';assert.equal(validateDepth10(d),false);
});
test('non-finite, malformed and precision-bomb values rejected',()=>{
 for(const bad of ['NaN','Infinity','1e999999999','0x10','1.2.3','1'.repeat(97),' 55','055']){
  const x=make();x.bid_total_base_10=bad;assert.equal(validateDepth10(x),false,bad);
 }
});
test('both sides must contain exactly 10 levels',()=>{
 const a=make();a.bids.pop();assert.equal(validateDepth10(a),false);
});
test('real pinned v34.1 L2 projection validates without floating-point rounding',async t=>{
 const path=process.env.V34_L2_PROJECTION;
 if(!path){t.skip('No immutable v34.1 source supplied');return;}
 const {readFile}=await import('node:fs/promises');
 const projection=JSON.parse(await readFile(path,'utf8'));
 let checked=0;
 for(const product of ['BTC-USD','ETH-USD']){
  for(const row of projection.products[product].depth_snapshots){
   if(row.depth){assert.equal(validateDepth10(row.depth),true,product+' '+row.decision_receiver_ms);checked++;}
  }
 }
 assert.ok(checked>=2,'Insufficient real L2 snapshots: '+checked);
});
