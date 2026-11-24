import {useLayoutEffect,useRef,useState} from 'react';
import {FnChart} from '@funnest/charts-react';
import {createCompletedCandleFeed} from '../v36_1_completed_candle_feed';
import {assertCandleAdvance} from './candleContract';
import type {CandleBundle} from './candleContract';
type Product='BTC-USD'|'ETH-USD';
type Feed=ReturnType<typeof createCompletedCandleFeed>;
export default function SourceCandleChart({product,bundle}:{product:Product;bundle:CandleBundle}){
 const current=useRef<Feed|null>(null),previous=useRef<CandleBundle|null>(null),failed=useRef(false);
 const [feed,setFeed]=useState<Feed|null>(null),[error,setError]=useState('');
 useLayoutEffect(()=>{
  const f=createCompletedCandleFeed(product);current.current=f;previous.current=null;failed.current=false;setError('');setFeed(null);
  return()=>{f.dispose();current.current=null;previous.current=null;};
 },[product,bundle.key]);
 useLayoutEffect(()=>{
  const f=current.current;if(!f||failed.current)return;
  try{if(previous.current)assertCandleAdvance(previous.current,bundle);f.publish(bundle.rows,bundle.asof);previous.current=bundle;setFeed(f);}
  catch(e){failed.current=true;f.dispose();setFeed(null);setError(e instanceof Error?e.message:'CANDLE_REJECTED');}
 },[bundle]);
 return <><p>Observed receiver-time trade updates · completed minutes only · independent candle replay not verified</p>{error?<p role="alert">Chart blocked: {error}</p>:feed&&bundle.rows.length?<div style={{height:300}}><FnChart datafeed={feed.datafeed} symbol={product} resolution="1" colorScheme="dark" locale="en" storageKey={null} drawingToolbar={false} onReady={()=>{}} style={{height:'100%'}}/></div>:<p>No completed trade-derived minute is available yet.</p>}</>;
}
