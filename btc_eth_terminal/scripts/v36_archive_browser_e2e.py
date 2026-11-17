"""Actual source → v35 Archive Ingress → v36 engine → Chromium DOM parity.

No synthetic market payloads. Funnest Charts draws to canvas, so this harness
checks that the chart exists plus the exact HTTP OHLCV input separately; canvas
pixel-level OHLCV equivalence needs an additional supported chart introspection
mechanism and is NOT silently marked as verified.
"""
from __future__ import annotations
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Thread
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from v36_live_engine import LiveMarketEngine
from v36_live_gateway import server_for_live
from v36_play_verified_archive import admitted_verified_chunks

DEFAULT=ROOT/'backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy'

def run(capture:Path,pin:str,browser_executable:str|None=None)->dict:
    result={'schema':'v36_real_archive_chromium_e2e_v1','status':'NOT_EXECUTED',
            'source':'AUTHENTIC_SHA_PINNED_ARCHIVED_COINBASE','mode':'ARCHIVE_SIMULATION',
            'live_feed':False,'browser_status':'NOT_EXECUTED','manifest_sha256':pin,
            'verified_products':[],'market_data_mocked':False,'order_count':0}
    e=LiveMarketEngine()
    stream=admitted_verified_chunks(capture,pin)
    certificate,_=next(stream)
    count=0
    for _,data in stream:
        for row in data.splitlines(keepends=True):
            e.consume(row);count+=1
    assert count==certificate['messages']
    result['source_message_count']=count
    result['admission']=certificate['admission']
    svr=server_for_live(e)
    worker=Thread(target=svr.serve_forever,daemon=True);worker.start()
    origin=f'http://127.0.0.1:{svr.server_address[1]}'
    try:
        critical=['/research/v36','/api/v36/health',
                  '/api/v36/snapshot?product=BTC-USD',
                  '/api/v36/snapshot?product=ETH-USD',
                  '/api/v36/candles?product=BTC-USD',
                  '/api/v36/candles?product=ETH-USD']
        legacy=['/research','/terminal','/research/v35.1']
        for route in critical+legacy:
            try:
                with urlopen(origin+route,timeout=8) as res:
                    assert res.status==200,route
                    payload=res.read()
                    if route.startswith('/api/v36/'):
                        obj=json.loads(payload)
                        assert obj['mode']=='ARCHIVE_SIMULATION',route
                result.setdefault('http_routes_verified',[]).append(route)
            except HTTPError as exc:
                # Code-only public CI does not contain frozen legacy dist.
                # Separate its missing historical assets from actual v36 E2E.
                if route in legacy and exc.code==503:
                    result.setdefault('legacy_asset_routes_not_verified',[]).append(route)
                    continue
                raise
        result['http_status']='PASS_GENUINE_ARCHIVE_V36_CORE'
        result['legacy_routes_status']=('UNVERIFIED_MISSING_FROZEN_ASSETS_IN_CODE_ONLY_CI'
                                        if result.get('legacy_asset_routes_not_verified') else
                                        'PASS_LEGACY_ASSET_ROUTES')
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            result.update(status='PARTIAL_HTTP_ONLY',browser_status='BLOCKED_PLAYWRIGHT_MISSING')
            return result
        try:
            with sync_playwright() as playwright:
                cfg={'headless':True}
                if browser_executable:cfg['executable_path']=browser_executable
                browser=playwright.chromium.launch(**cfg)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':920})
                    js_errors=[]
                    page.on('pageerror',lambda exc:js_errors.append(str(exc)))
                    page.goto(origin+'/research/v36',wait_until='domcontentloaded',timeout=15000)
                    page.get_by_role('heading',name='BTC/ETH Live Market Research').wait_for(timeout=12000)
                    for product in ('BTC-USD','ETH-USD'):
                        page.get_by_role('button',name=product).click()
                        expected=e.snapshot(product)['state']
                        if expected is None:raise AssertionError('ARCHIVED_SOURCE_NOT_ADMITTED')
                        # Product changes are asynchronous. Wait for the exact
                        # matching source-backed state, never read prior-product
                        # market values as evidence of the new instrument.
                        page.get_by_role('status').filter(
                            has_text=f"state version {expected['state_version']}").wait_for(timeout=12000)
                        page.get_by_text('ARCHIVE SIMULATION · NOT LIVE').wait_for(timeout=12000)
                        panel=page.get_by_role('region',name='Validated top of book')
                        for key in ('best_bid','best_ask','spread','microprice','ofi_since_connection_base'):
                            card=panel.locator('div').filter(has_text=key.upper().replace('_',' ')).first
                            observed=card.locator('strong').inner_text().strip()
                            if observed!=str(expected[key]):raise AssertionError(f'{product}:{key}: {observed} != {expected[key]}')
                        depth=page.get_by_role('region',name='Live order book 10 levels')
                        cells=depth.locator('tbody tr').first.locator('td').all_inner_texts()
                        expected_cells=[expected['depth']['bids'][0]['price'],expected['depth']['bids'][0]['quantity'],
                            expected['depth']['asks'][0]['price'],expected['depth']['asks'][0]['quantity']]
                        if cells!=expected_cells:raise AssertionError(f'{product}:top_of_book_depth_mismatch')
                        candle_count=len(e.candles_page(product)['rows'])
                        # A canvas presence is NOT an OHLCV value parity assertion.
                        has_chart=page.get_by_role('region',name='Live source-backed Funnest chart').locator('canvas').count()>0
                        if candle_count>0 and not has_chart:raise AssertionError('FUNNEST_CANVAS_MISSING')
                        result['verified_products'].append({'product':product,'state_version':expected['state_version'],
                            'best_bid':expected['best_bid'],'best_ask':expected['best_ask'],
                            'spread':expected['spread'],'session_id':expected['session_id'],
                            'receiver_time_ms':expected['receiver_time_ms'],'completed_candle_count':candle_count,
                            'chart_canvas_present':has_chart,'canvas_OHLCV_numeric_parity':'NOT_VERIFIED'})
                    if js_errors:raise AssertionError('JAVASCRIPT_RUNTIME_ERRORS:'+repr(js_errors))
                    result['browser_status']='PASS_DOM_BOOK_AND_QUOTES_ONLY'
                    result['status']='PARTIAL_CANVAS_VALUES_UNVERIFIED'
                finally:browser.close()
        except Exception as exc:
            if 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):
                result['browser_status']='BLOCKED_BY_ADMINISTRATOR'
                result['status']='PARTIAL_HTTP_ONLY'
                result['blocker']=str(exc)[:600]
            else:
                result['status']='FAILED'
                result['browser_status']='FAILED'
                result['error']=repr(exc)[:900]
        return result
    finally:
        svr.shutdown();svr.server_close();worker.join(timeout=4)

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--capture',required=True,type=Path)
    ap.add_argument('--manifest-sha256',required=True,
                    help='Independently pinned digest of the supplied, private source manifest')
    ap.add_argument('--browser-executable')
    ap.add_argument('--report',type=Path,required=True)
    args=ap.parse_args()
    try:res=run(args.capture,args.manifest_sha256,args.browser_executable)
    except Exception as exc:res={'status':'FAILED','error':repr(exc),'market_data_mocked':False}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(res,indent=2,sort_keys=True)+'\n')
    print(json.dumps({'status':res['status'],'browser_status':res.get('browser_status'),'http_status':res.get('http_status')}))
    return 0 if res['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
