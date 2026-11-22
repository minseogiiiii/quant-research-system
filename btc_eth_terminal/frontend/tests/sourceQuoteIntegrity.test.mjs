import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {validateQuoteAgainstDepth, validateTradeTape} from '../src/marketStateIntegrity.ts';

function book() {
  let bid=0,ask=0;
  const bids=Array.from({length:10},(_,i)=>({price:(100-i).toFixed(2),quantity:'2.00000000',cumulative_base:String(bid+=2)}));
  const asks=Array.from({length:10},(_,i)=>({price:(101+i).toFixed(2),quantity:'1.00000000',cumulative_base:String(ask+=1)}));
  return {bids,asks,bid_total_base_10:'20',ask_total_base_10:'10'};
}
function quote() {return {depth:book(),best_bid:'100.00',best_ask:'101.00',best_bid_qty:'2',best_ask_qty:'1',spread:'1.00',microprice:'100.6666666666666666666666667',best_imbalance:'0.3333333333333333333333333333'};}
function trades() {return [{price:'100.25',size:'0.00000001',maker_side:'BUY',received_at_ms:1794641293624,trade_id:'12004'},{price:'100.5',size:'1',maker_side:'SELL',received_at_ms:1794641293625,trade_id:null}];}

test('source-backed top-of-book agrees with depth ladder and spread exactly',()=>{
  assert.equal(validateQuoteAgainstDepth(quote()),true);
  for(const [field,value] of [['best_bid','99'],['best_ask','102'],['best_bid_qty','3'],['best_ask_qty','2'],['spread','0.99999999999999999999999999'],['microprice','102'],['microprice','NaN'],['spread','Infinity']]){
    const q=quote();q[field]=value;
    assert.equal(validateQuoteAgainstDepth(q),false,field);
  }
  const q=quote();q.depth.bids[0].quantity='3';
  assert.equal(validateQuoteAgainstDepth(q),false,'changed book quantity must invalidate derived quote');
});

test('trade tape is bounded and parseable before React renders it',()=>{
  const now=1794641293625;
  assert.equal(validateTradeTape([],now),true);
  assert.equal(validateTradeTape(trades(),now),true);
  for(const bad of [null,{},Array(25).fill(trades()[0]),[{...trades()[0],size:'NaN'}],
    [{...trades()[0],maker_side:'INVALID'}],[{...trades()[0],received_at_ms:now+1}],
    [{...trades()[0],received_at_ms:Infinity}],[{...trades()[0],price:'0'}],
    [{...trades()[0],trade_id:{wrong:'type'}}]]){
    assert.equal(validateTradeTape(bad,now),false,JSON.stringify(bad)?.slice(0,100));
  }
});

test('live terminal admits a row only after quote and trade integrity gates',async()=>{
 const source=await readFile(new URL('../src/LiveTerminal.tsx',import.meta.url),'utf8');
 assert.match(source,/validateQuoteAgainstDepth\(r\)/);
 assert.match(source,/validateTradeTape\(r\.recent_trades,r\.receiver_time_ms\)/);
});

test('captured real BTC/ETH source rows satisfy quote and tape integrity',async t=>{
 if(!process.env.Q4_REAL_STATE_JSON){t.skip('Explicit genuine archived-source fixture not supplied');return;}
 const rows=JSON.parse(await readFile(process.env.Q4_REAL_STATE_JSON,'utf8'));
 for(const product of ['BTC-USD','ETH-USD']){
  const row=rows[product];
  assert.equal(validateQuoteAgainstDepth(row),true,product+' derived quote');
  assert.equal(validateTradeTape(row.recent_trades,row.receiver_time_ms),true,product+' tape');
 }
});
