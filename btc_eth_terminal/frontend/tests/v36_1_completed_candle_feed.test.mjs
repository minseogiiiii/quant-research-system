import test from 'node:test';
import assert from 'node:assert/strict';
import {createCompletedCandleFeed,validateCompletedCandles} from '../src/v36_1_completed_candle_feed.ts';

const start=1794890400000;
function candle(i, overrides={}) {
  return {time:start+i*60000,available_at:start+(i+1)*60000+180,
    open:'100.01',high:'100.05',low:'100.00',close:'100.02',
    volume:'1.00000000000000001',observed_trade_count:3,...overrides};
}
function make(){
  return createCompletedCandleFeed('BTC-USD');
}
const symbol={symbol:'BTC-USD'};

test('exactly completed receiver-time candle admitted, incomplete and future availability rejected',()=>{
 const c=candle(0);
 assert.equal(validateCompletedCandles([c],c.available_at),true);
 assert.equal(validateCompletedCandles([c],c.available_at-1),false);
 assert.equal(validateCompletedCandles([candle(0,{available_at:start+59999})],c.available_at),false);
 assert.equal(validateCompletedCandles([candle(0,{high:'99'})],c.available_at),false);
 assert.equal(validateCompletedCandles([candle(0,{observed_trade_count:0})],c.available_at),false);
});
test('Funnest 0.6.24 getBars UTC ms and countBack semantics',async()=>{
 let served=null;
 const f=createCompletedCandleFeed('BTC-USD',b=>served=b);
 f.publish([candle(0),candle(1)],start+120500);
 const res=await f.datafeed.getBars(symbol,'1',{from:start,to:start+60000});
 assert.deepEqual(res.bars.map(x=>x.time),[start]);
 assert.equal(res.bars[0].volume,1);
 assert.deepEqual(served,res.bars);
 assert.deepEqual((await f.datafeed.getBars(symbol,'1',{from:0,to:start+180000,countBack:1}))
   .bars.map(x=>x.time),[start+60000]);
 f.dispose();
});
test('new completed bars use subscribeBars onBar exactly once, no in-progress ticks',()=>{
 const f=make(),updates=[];
 const un=f.datafeed.subscribeBars(symbol,'1',b=>updates.push(b));
 f.publish([candle(0)],start+60000+180);
 assert.deepEqual(updates.map(x=>x.time),[start]);
 f.publish([candle(0),candle(1)],start+120000+180);
 assert.deepEqual(updates.map(x=>x.time),[start,start+60000]);
 f.publish([candle(0),candle(1)],start+120000+180);
 assert.equal(updates.length,2);
 un();
 f.publish([candle(0),candle(1),candle(2)],start+180000+180);
 assert.equal(updates.length,2);
 f.dispose();
});
test('rewritten, omitted or out-of-order completed history is refused',()=>{
 const f=make();
 f.publish([candle(0),candle(1)],start+120500);
 assert.throws(()=>f.publish([candle(0),candle(1,{close:'100.03'})],start+120500),/REWRITTEN/);
 assert.throws(()=>f.publish([candle(0)],start+120500),/REWRITTEN/);
 assert.throws(()=>f.publish([candle(1),candle(0)],start+120500),/UNVERIFIED/);
 f.dispose();
});
test('product/symbol/resolution isolation and disposed source cannot continue',async()=>{
 const f=make();
 await assert.rejects(()=>f.datafeed.getBars({symbol:'ETH-USD'},'1',{from:0,to:start}),/SOURCE/);
 await assert.rejects(()=>f.datafeed.getBars(symbol,'1D',{from:0,to:start}),/SOURCE/);
 assert.throws(()=>f.datafeed.subscribeBars({symbol:'ETH-USD'},'1',()=>{}),/INVALID/);
 f.dispose();
 assert.throws(()=>f.publish([candle(0)],start+61000),/DISPOSED/);
});
test('malformed, duplicate, non-finite, zero, negative or excessive bars are refused',()=>{
 for(const v of ['NaN','Infinity','-1','1e2000',' ','0100']){
  assert.equal(validateCompletedCandles([candle(0,{open:v})],start+70000),false,v);
 }
 assert.equal(validateCompletedCandles([candle(0),candle(0)],start+70000),false);
 assert.equal(validateCompletedCandles(Array(121).fill(candle(0)),start+70000),false);
});
