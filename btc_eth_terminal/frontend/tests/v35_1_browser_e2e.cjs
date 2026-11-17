/* Synthetic contract only. This test cannot verify actual Coinbase source identity. */
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('playwright-core');
const root=path.resolve('dist'),pin='a'.repeat(64),projection='b'.repeat(64);
function row(product,n){
 const base=product==='BTC-USD'?100:200;
 const bids=[],asks=[];
 for(let i=0;i<10;i++){bids.push({price:String(base-i),quantity:'1',cumulative_base:String(i+1)});asks.push({price:String(base+i+1),quantity:'1',cumulative_base:String(i+1)});}
 const ts=1794641297620+n*1000;
 return {product,mode:'ARCHIVE_REPLAY',live_feed:false,trading_enabled:false,source_manifest_sha256:pin,status:'VALID_ARCHIVED_QUOTE',freshness:'ARCHIVED_NOT_LIVE',receiver_time_ms:ts,source_last_l2_receiver_ms:ts,source_age_ms:0,state_version:n,connection_id:0,source_sequence:n,best_bid:String(base),best_ask:String(base+1),spread:'1',microprice:String(base+0.5),best_imbalance:'0',ofi_since_connection_base:'0',depth:{bids,asks,bid_total_base_10:'10',ask_total_base_10:'10'}};
}
function send(res,code,obj){const raw=Buffer.from(JSON.stringify(obj));res.writeHead(code,{'Content-Type':'application/json','Content-Length':raw.length});res.end(raw);}
function serve(quarantined){return http.createServer((req,res)=>{
 const u=new URL(req.url,'http://localhost');
 if(u.pathname==='/api/v30/catalog')return send(res,200,{source_sha256:projection,projection_sha256:pin,source_message_count:0,source_start_ms:1794641297000,source_end_ms:1794641301000,source_cohort:'SYNTHETIC_CI_ONLY',live_feed:false,trading_enabled:false,products:[{product:'BTC-USD',candles:0,diagnostics:0,recent_trades:0,observed_trade_updates:0},{product:'ETH-USD',candles:0,diagnostics:0,recent_trades:0,observed_trade_updates:0}]});
 if(u.pathname==='/api/v30/series')return send(res,200,{product:u.searchParams.get('product'),kind:u.searchParams.get('kind'),rows:[],next_offset:null,source_sha256:projection,projection_sha256:pin,oos_approved:false,live_feed:false});
 if(u.pathname==='/api/v35/health')return send(res,200,{mode:'ARCHIVE_REPLAY',source_admission:quarantined?'QUARANTINED_ARCHIVE_ENVELOPE':'VERIFIED_ARCHIVE_ENVELOPE',source_manifest_sha256:pin,messages:2,live_feed:false,trading_enabled:false});
 if(u.pathname==='/api/v35/history')return send(res,200,{mode:'ARCHIVE_REPLAY',product:u.searchParams.get('product'),source_manifest_sha256:pin,live_feed:false,trading_enabled:false,rows:[row(u.searchParams.get('product'),1),row(u.searchParams.get('product'),2)],next_after:2,has_more:false});
 if(u.pathname==='/api/v35/candles'){
  const product=u.searchParams.get('product'),base=product==='BTC-USD'?100:200;
  return send(res,200,{schema:'v35_1_archived_trade_candles_v1',product,mode:'ARCHIVE_REPLAY',
   live_feed:false,trading_enabled:false,source_manifest_sha256:pin,timeframe_ms:60000,
   source_definition:'RECEIVER_TIME_COMPLETED_BUCKETS_OBSERVED_TRADE_UPDATES_ONLY',
   rows:[{schema:'v35_1_receiver_trade_bar_v1',product,mode:'ARCHIVE_REPLAY',
    live_feed:false,trading_enabled:false,source_manifest_sha256:pin,
    time:1794641220000,available_at:1794641280000,
    open:String(base),high:String(base+3),low:String(base),close:String(base+1),volume:'5',
    observed_trade_count:2,source_definition:'RECEIVER_TIME_COMPLETED_BUCKET_OBSERVED_TRADE_UPDATES_ONLY'}]});
 }
 if(u.pathname==='/api/v35/stream'){
  const lines=['event: source\ndata: {"mode":"ARCHIVE_REPLAY","live_feed":false}\n\n'];
  for(let n=1;n<=2;n++)lines.push('id: '+n+'\nevent: state\ndata: '+JSON.stringify(row(u.searchParams.get('product'),n))+'\n\n');
  lines.push('event: replay_end\ndata: {"next_after":2,"mode":"ARCHIVE_REPLAY","live_feed":false,"has_more":false}\n\n');
  const raw=Buffer.from(lines.join(''));res.writeHead(200,{'Content-Type':'text/event-stream','Content-Length':raw.length});return res.end(raw);
 }
 const file=['/','/research','/research/v35.1'].includes(u.pathname)?'index.html':u.pathname.slice(1);
 if(!(/^index\.html$|^assets\/index-[a-zA-Z0-9_-]+\.(?:js|css)$/.test(file)))return send(res,404,{error:'NOT_FOUND'});
 try{const raw=fs.readFileSync(path.join(root,file));res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html','Content-Length':raw.length});res.end(raw);}
 catch{return send(res,404,{error:'NOT_FOUND'});}
});}
(async()=>{
 const executablePath=process.env.CHROME_PATH||'/usr/bin/google-chrome';
 if(!fs.existsSync(executablePath))throw new Error('Chromium binary unavailable: '+executablePath);
 const browser=await chromium.launch({executablePath,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{
  for(const quarantine of [false,true]){
   const server=serve(quarantine);await new Promise(r=>server.listen(0,'127.0.0.1',r));
   const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
   page.on('pageerror',e=>errors.push(String(e)));
   try{
    await page.goto('http://127.0.0.1:'+server.address().port+'/research/v35.1',{waitUntil:'domcontentloaded'});
    const panel=page.getByRole('region',{name:'v35.1 verified Coinbase archive replay'});
    await panel.waitFor();
    if(!quarantine){
     await panel.getByRole('table',{name:'Verified replay order book'}).waitFor();
     await panel.getByText(/1 candles available at selected replay time/).waitFor();
     await panel.getByRole('button',{name:'Step',exact:true}).click();
     await panel.getByText('Observation 2/2').waitFor();
     await panel.getByRole('button',{name:'Check SSE stream'}).click();
     await panel.getByText(/SSE VERIFIED/).waitFor();
     await page.locator('.pro-symbols button').nth(1).click();
     await panel.getByRole('table',{name:'Verified replay order book'}).waitFor();
     await panel.getByText('Observation 1/2').waitFor();
    }else{
     await panel.getByRole('alert').filter({hasText:'ARCHIVE DATA BLOCKED'}).waitFor();
     assert.equal(await panel.getByRole('table',{name:'Verified replay order book'}).count(),0);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS synthetic fixture browser DOM',quarantine?'quarantine':'valid');
   }finally{await page.close();await new Promise(r=>server.close(r));}
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
