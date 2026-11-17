import type {Bar, BarsRequest, BarsResult, Datafeed, Resolution, SymbolInfo} from '@funnest/charts';

/** Only fully closed, receiver-time Coinbase trade bars are publishable. */
export type CompletedCandle = {
  time:number; available_at:number; open:string; high:string; low:string; close:string;
  volume:string; observed_trade_count:number
};
type Subscriber={onBar:(bar:Bar)=>void; onConnection?:((state:{status:'connected'|'disconnected'})=>void)};
const DECIMAL=/^(?:0|[1-9]\d*)(?:\.\d+)?$/;
const isDecimal=(v:unknown)=>typeof v==='string'&&v.length>0&&v.length<=96&&DECIMAL.test(v);

export function validateCompletedCandles(rows:unknown, observedAsOfMs:number):rows is CompletedCandle[] {
  if(!Array.isArray(rows)||rows.length>120||!Number.isSafeInteger(observedAsOfMs))return false;
  let prior=-1;
  for(const c of rows){
    if(c===null||typeof c!=='object'||Array.isArray(c))return false;
    if(!Number.isSafeInteger(c.time)||c.time<1||c.time%60000!==0||c.time<=prior||
       !Number.isSafeInteger(c.available_at)||c.available_at<c.time+60000||
       c.available_at>observedAsOfMs||
       !Number.isSafeInteger(c.observed_trade_count)||c.observed_trade_count<1||
       ![c.open,c.high,c.low,c.close,c.volume].every(isDecimal))return false;
    const open=Number(c.open),high=Number(c.high),low=Number(c.low),
      close=Number(c.close),volume=Number(c.volume);
    if(![open,high,low,close,volume].every(Number.isFinite)||open<=0||close<=0||
       low<=0||high<Math.max(open,close,low)||low>Math.min(open,close)||volume<=0)return false;
    prior=c.time;
  }
  return true;
}
const asBar=(c:CompletedCandle):Bar=>({
  time:c.time,open:Number(c.open),high:Number(c.high),low:Number(c.low),
  close:Number(c.close),volume:Number(c.volume)
});
const same=(a:Bar,b:Bar)=>a.time===b.time&&a.open===b.open&&a.high===b.high&&a.low===b.low&&
  a.close===b.close&&a.volume===b.volume;

/**
 * Funnest 0.6.24 Datafeed: UTC milliseconds, getBars and onBar callbacks.
 * Funnest's documented subscribeBars callback can append completed bars.
 * No fabricated ticks, no in-progress candle, no chart remount per minute.
 */
export function createCompletedCandleFeed(product:'BTC-USD'|'ETH-USD',
  onGetBars?:(bars:readonly Bar[])=>void) {
  const info:SymbolInfo={symbol:product,name:product+' observed public spot trade updates',
    exchange:'Coinbase Advanced receiver clock',session:'24x7',timezone:'UTC',type:'crypto',
    tickSize:0.01,pricePrecision:2,supportedResolutions:['1']};
  let bars:Bar[]=[];
  let terminal=false;
  const subscribers=new Set<Subscriber>();
  const datafeed:Datafeed={
    async resolveSymbol(symbol:string){if(symbol!==product)throw Error('WRONG_SOURCE_PRODUCT');return info;},
    async searchSymbols(query:string){return product.toLowerCase().includes(query.toLowerCase())?[info]:[];},
    async getBars(symbol:SymbolInfo,resolution:Resolution,req:BarsRequest):Promise<BarsResult>{
      if(terminal||symbol.symbol!==product||resolution!=='1')throw Error('SOURCE_OR_RESOLUTION_INVALID');
      if(!Number.isSafeInteger(req.to)||!Number.isSafeInteger(req.from)||req.from>req.to)
        throw Error('UNBOUNDED_OR_INVALID_CANDLE_REQUEST');
      let selected=bars.filter(b=>b.time>=req.from&&b.time<req.to);
      if(req.countBack!==undefined){
        if(!Number.isSafeInteger(req.countBack)||req.countBack<0||req.countBack>500)
          throw Error('INVALID_COUNT_BACK');
        selected=bars.filter(b=>b.time<req.to).slice(-req.countBack);
      }
      const copy=selected.map(b=>({...b}));
      onGetBars?.(copy);
      return {bars:copy,noMoreData:true};
    },
    subscribeBars(symbol:SymbolInfo,resolution:Resolution,onBar:(bar:Bar)=>void,
                  _onResetCache?:()=>void,onConnection?:(status:{status:'connecting'|'connected'|'reconnecting'|'disconnected'})=>void){
      if(terminal||symbol.symbol!==product||resolution!=='1')throw Error('INVALID_SUBSCRIPTION');
      const sub:Subscriber={onBar,onConnection};
      subscribers.add(sub);onConnection?.({status:'connected'});
      return ()=>{subscribers.delete(sub);onConnection?.({status:'disconnected'});};
    }
  };
  function publish(rows:CompletedCandle[],observedAsOfMs:number){
    if(terminal)throw Error('FEED_DISPOSED');
    if(!validateCompletedCandles(rows,observedAsOfMs))throw Error('UNVERIFIED_COMPLETED_CANDLES');
    const next=rows.map(asBar),oldTail=bars.at(-1);
    if(oldTail){
      const matches=next.find(b=>b.time===oldTail.time);
      if(!matches||!same(matches,oldTail))throw Error('CANDLE_HISTORY_REWRITTEN_OR_DROPPED');
    }
    bars=next;
    for(const bar of next){
      if(oldTail&&bar.time<=oldTail.time)continue;
      for(const sub of subscribers)sub.onBar({...bar});
    }
  }
  function dispose(){terminal=true;bars=[];subscribers.clear();}
  return {datafeed,publish,dispose,getCompleted:()=>bars.map(b=>({...b}))};
}
