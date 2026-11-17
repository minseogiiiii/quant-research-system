"""Actual LIVE_SOURCE Chromium market-state and Funnest Datafeed observation.

Requires an already-running authorized local read-only Coinbase collector and
v36 gateway. Never substitutes archived replay or synthetic market data.
Every DOM market quote is paired with the exact session/version market state.
Only completed candle bars actually returned to Funnest getBars are compared.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError

PRODUCTS=('BTC-USD','ETH-USD')
VERSION=re.compile(r'state version ([1-9][0-9]*)')
METRICS=('best_bid','best_ask','spread','microprice','best_imbalance','ofi_since_connection_base')
def read(origin:str,route:str)->dict:
    with urlopen(origin+route,timeout=3) as reply:
        if reply.status!=200:raise AssertionError('HTTP_SOURCE_UNAVAILABLE')
        return json.load(reply)

def fail(reason:str):raise AssertionError(reason)

def audit_browser(origin:str,browser_executable:str,seconds:int,min_versions:int,
                  min_bars_per_product:int,report:Path)->dict:
    from playwright.sync_api import sync_playwright
    results={'schema':'v36_1_actual_live_browser_contract_v1',
       'evidence':'REAL_PUBLIC_COINBASE_LIVE_SOURCE_NOT_REPLAY',
       'requested_seconds':seconds,'minimum_versions_per_product':min_versions,
       'minimum_completed_bars_per_product':min_bars_per_product,
       'source_sessions':[],'trading_enabled':False,
       'products':{p:{'versions':[],'actual_funnest_getbars_matched':0,
                      'backend_completed_candles':0} for p in PRODUCTS},
       'status':'BLOCKED','canvas_pixel_numeric_equality':'NOT_VERIFIED'}
    errors=[]
    session=None
    started=time.monotonic()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=browser_executable,headless=True)
        try:
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.goto(origin+'/research/v36',wait_until='domcontentloaded',timeout=20000)
            page.get_by_role('heading',name='BTC/ETH Live Market Research').wait_for(timeout=12000)
            until=started+seconds
            while time.monotonic()<until:
                health=read(origin,'/api/v36/health')
                if health.get('mode')!='LIVE_SOURCE' or health.get('live_feed') is not True:
                    fail('NOT_GENUINE_LIVE_SOURCE')
                if health.get('status')!='HEALTHY' or health.get('trading_enabled') is not False:
                    fail('LIVE_SOURCE_NOT_HEALTHY')
                new_session=health['session_id']
                if session is None:
                    session=new_session;results['source_sessions'].append(new_session)
                elif new_session!=session:
                    fail('SOURCE_SESSION_CHANGED_UNDER_UNINTERRUPTED_GATE')
                for product in PRODUCTS:
                    if time.monotonic()>=until:break
                    page.get_by_role('button',name=product).click()
                    # Allow full source-backed initial admission and at least
                    # several L2 updates per product before switching again.
                    # The original 1-snapshot-per-switch harness undersampled
                    # the LIVE DOM despite correct source and archived replay.
                    dwell_stop=min(until,time.monotonic()+10.0)
                    while time.monotonic()<dwell_stop:
                        page.wait_for_timeout(750)
                        # One browser JS event-loop turn captures the same React
                        # committed DOM. Sequential Playwright selectors can cross
                        # a 1-second book update and create a false depth mismatch.
                        dom=page.evaluate("""() => {
                          const root=document.querySelector('main.v36-live-terminal');
                          const top=root?.querySelector('[aria-label="Validated top of book"]');
                          const depth=root?.querySelector('[aria-label="Live order book 10 levels"]');
                          const cards={};
                          for(const c of (top?.querySelectorAll('div')||[])){
                            const label=c.querySelector(':scope > small')?.textContent?.trim()||'';
                            const value=c.querySelector(':scope > strong')?.textContent?.trim();
                            if(label&&value!==undefined)cards[label]=value;
                          }
                          return {
                            status:root?.querySelector('[role="status"]')?.textContent||'',
                            sourceHealth:root?.querySelector('[aria-label="Source health"]')?.textContent||'',
                            selected:root?.querySelector('button[aria-pressed="true"]')?.textContent||'',
                            liveLabel:root?.querySelector('header + p > strong')?.textContent||'',
                            cards,
                            depth:Array.from(depth?.querySelectorAll('tbody tr')||[],
                              tr=>Array.from(tr.querySelectorAll('td'),td=>td.textContent?.trim()||'')),
                            datafeedProduct:root?.querySelector('[data-testid="funnest-actual-getbars"]')?.getAttribute('data-product'),
                            datafeedSession:root?.querySelector('[data-testid="funnest-actual-getbars"]')?.getAttribute('data-session'),
                            datafeedBars:root?.querySelector('[data-testid="funnest-actual-getbars"]')?.getAttribute('data-bars')
                          };
                        }""")
                        if dom['selected'].strip()!=product:continue
                        if 'OBSERVED LIVE SOURCE' not in dom['liveLabel']:continue
                        if session not in dom['sourceHealth']:continue
                        match=VERSION.search(dom['status'])
                        if not match:continue
                        v=int(match.group(1))
                        params=urlencode({'product':product,'session':session,'version':v})
                        try:back=read(origin,'/api/v36/state?'+params)['state']
                        except HTTPError as exc:
                            if exc.code==409:continue  # old version evicted or source no longer healthy
                            raise
                        if back['session_id']!=session or back['product']!=product:
                            fail('BAD_BACKEND_PRODUCT_SESSION')
                        for metric in METRICS:
                            observed=dom['cards'].get(metric.upper().replace('_',' '))
                            if observed is None or Decimal(observed)!=Decimal(str(back[metric])):
                                fail('EXACT_VERSION_DOM_'+metric+'_MISMATCH_'+product)
                        expected=[[back['depth']['bids'][k]['price'],
                                   back['depth']['bids'][k]['quantity'],
                                   back['depth']['asks'][k]['price'],
                                   back['depth']['asks'][k]['quantity']] for k in range(10)]
                        if len(dom['depth'])!=10 or dom['depth']!=expected:
                            fail('EXACT_VERSION_ATOMIC_DEPTH_MISMATCH_'+product)
                        versions=results['products'][product]['versions']
                        if v not in versions:versions.append(v)
                        candles=read(origin,'/api/v36/candles?product='+product)
                        if candles.get('session_id')!=session:fail('CANDLE_SESSION_CHANGED')
                        eligible={b['time']:b for b in candles['rows']}
                        results['products'][product]['backend_completed_candles']=max(
                            results['products'][product]['backend_completed_candles'],len(eligible))
                        if dom.get('datafeedBars') is not None:
                            if dom.get('datafeedProduct')!=product or dom.get('datafeedSession')!=session:
                                continue
                            bars=json.loads(dom.get('datafeedBars') or '[]')
                            for b in bars:
                                ref=eligible.get(b['time'])
                                if ref is None:fail('FUNNEST_GETBARS_UNBACKED_TIME_'+product)
                                for field in ('open','high','low','close','volume'):
                                    if float(ref[field])!=b[field]:
                                        fail('FUNNEST_GETBARS_NUMERIC_MISMATCH_'+product+'_'+field)
                            results['products'][product]['actual_funnest_getbars_matched']=max(
                                results['products'][product]['actual_funnest_getbars_matched'],len(bars))
                print('LIVE_BROWSER_PROGRESS '+json.dumps({
                    'elapsed_seconds':round(time.monotonic()-started,1),
                    'verified_versions':{p:len(results['products'][p]['versions'])
                                         for p in PRODUCTS},
                    'backed_funnest_bars':{p:results['products'][p]['actual_funnest_getbars_matched']
                                          for p in PRODUCTS}
                },sort_keys=True),flush=True)
                if errors:fail('CHROMIUM_JAVASCRIPT_RUNTIME_ERROR:'+errors[0][:150])
                page.wait_for_timeout(1500)
        finally:
            browser.close()
    results['elapsed_seconds']=round(time.monotonic()-started,3)
    for product in PRODUCTS:
        p=results['products'][product]
        p['number_of_distinct_source_versions']=len(p['versions'])
        # Omit verbose version arrays in public logs, keep them in private JSON.
        if len(p['versions'])<min_versions:fail('INSUFFICIENT_REAL_LIVE_DOM_VERSIONS_'+product)
        if p['backend_completed_candles']<min_bars_per_product:
            fail('INSUFFICIENT_REAL_COMPLETED_CANDLES_'+product)
        if p['actual_funnest_getbars_matched']<min_bars_per_product:
            fail('FUNNEST_DATAFEED_COMPLETED_CANDLE_NOT_OBSERVED_'+product)
    results['status']='PASS_VERSION_PINNED_LIVE_DOM_AND_FUNNEST_GETBARS'
    return results

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port',type=int,default=8766)
    ap.add_argument('--browser-executable',required=True)
    ap.add_argument('--seconds',type=int,default=180)
    ap.add_argument('--min-versions-per-product',type=int,default=10)
    ap.add_argument('--min-bars-per-product',type=int,default=1)
    ap.add_argument('--report',type=Path,required=True)
    args=ap.parse_args()
    if not 120<=args.seconds<=600:ap.error('invalid observation duration')
    try:
        result=audit_browser('http://127.0.0.1:'+str(args.port),args.browser_executable,
                             args.seconds,args.min_versions_per_product,args.min_bars_per_product,args.report)
    except Exception as error:
        result={'schema':'v36_1_actual_live_browser_contract_v1','status':'FAIL',
                'reason':type(error).__name__+':'+str(error)[:250],
                'synthetic_data_used':False,'trading_enabled':False,
                'canvas_pixel_numeric_equality':'NOT_VERIFIED'}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    summary={k:v for k,v in result.items() if k not in ('products','source_sessions')}
    if result.get('status','').startswith('PASS'):
        summary['products']={p:{k:v for k,v in result['products'][p].items() if k!='versions'}
                             for p in PRODUCTS}
        print('PASS_GENUINE_LIVE_CHROMIUM_VERSION_AND_FUNNEST_DATAFEED '+json.dumps(summary,sort_keys=True))
        return 0
    print('FAIL_GENUINE_LIVE_CHROMIUM_VERSION_AND_FUNNEST_DATAFEED '+json.dumps(summary,sort_keys=True))
    return 2
if __name__=='__main__':raise SystemExit(main())
