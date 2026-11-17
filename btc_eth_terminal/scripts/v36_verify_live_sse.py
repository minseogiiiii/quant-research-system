"""Observe ACTUAL new Coinbase L2 states over two concurrent persistent SSE streams.

This test is invoked only after an independent public-collector LIVE admission.
The market data must not be replaced by mocks or an archived replay service.
It does not claim Chromium/browser certification or 24-hour durability.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import re
import time
from urllib.request import urlopen

PRODUCTS=('BTC-USD','ETH-USD')
CURSOR=re.compile(r'v36:([0-9a-f]{32}):(BTC-USD|ETH-USD):([1-9][0-9]*)\Z')

def observe(origin:str,product:str,limit_s:float=14.0)->dict:
    started=time.monotonic()
    events=[]
    session=None
    snapshot=None
    states=[]
    with urlopen(origin+'/api/v36/stream?product='+product,timeout=5) as response:
        if response.status!=200 or 'text/event-stream' not in response.headers.get('Content-Type',''):
            raise AssertionError('NOT_A_LIVE_SSE_200_'+product)
        kind=None;data=None;event_id=None
        while time.monotonic()-started<limit_s and len(states)<2:
            chunk=response.readline(200_001)
            if not chunk:
                raise AssertionError('EARLY_SSE_EOF_'+product)
            if len(chunk)>=200_001:raise AssertionError('UNBOUNDED_SSE_LINE')
            line=chunk.decode('utf-8').rstrip('\r\n')
            if not line:
                if kind and data is not None:
                    payload=json.loads(data)
                    if kind=='source':
                        if (payload.get('mode')!='LIVE_SOURCE'
                                or payload.get('product')!=product
                                or payload.get('trading_enabled') is not False):
                            raise AssertionError('BAD_LIVE_SSE_SOURCE')
                        session=payload['session_id']
                    elif kind in ('snapshot','state'):
                        if not session or payload.get('session_id')!=session:
                            raise AssertionError('SESSION_CHANGED')
                        if (payload.get('product')!=product
                                or payload.get('mode')!='LIVE_SOURCE'
                                or payload.get('live_feed') is not True
                                or payload.get('trading_enabled') is not False):
                            raise AssertionError('BAD_LIVE_SSE_STATE')
                        parsed=CURSOR.fullmatch(event_id or '')
                        if parsed is None or parsed.group(1)!=session or parsed.group(2)!=product:
                            raise AssertionError('BAD_SOURCE_BOUND_SSE_ID')
                        if int(parsed.group(3))!=payload['state_version']:
                            raise AssertionError('CURSOR_VERSION_MISMATCH')
                        if Decimal(payload['best_bid'])>=Decimal(payload['best_ask']):
                            raise AssertionError('CROSSED_QUOTE')
                        if not re.fullmatch('[0-9a-f]{64}',payload.get('source_raw_sha256','')):
                            raise AssertionError('MISSING_WIRE_DIGEST')
                        if kind=='snapshot':
                            if snapshot is not None:raise AssertionError('DUPLICATE_INITIAL_SNAPSHOT')
                            snapshot=payload
                        else:
                            if snapshot is None:raise AssertionError('UPDATE_BEFORE_SNAPSHOT')
                            previous=states[-1] if states else snapshot
                            if (payload['state_version']<=previous['state_version']
                                or payload['receiver_time_ms']<previous['receiver_time_ms']
                                or payload['source_sequence']<previous['source_sequence']):
                                raise AssertionError('SSE_VERSION_OR_SOURCE_SEQUENCE_REGRESSION')
                            states.append(payload)
                kind=data=event_id=None
                continue
            if line.startswith('event: '):kind=line[7:]
            elif line.startswith('data: '):data=line[6:]
            elif line.startswith('id: '):event_id=line[4:]
    if snapshot is None or not states:
        raise AssertionError('NO_NEW_LIVE_MARKET_STATE_'+product)
    return {'product':product,'initial_version':snapshot['state_version'],
            'latest_version':states[-1]['state_version'],
            'verified_new_states':len(states),'session_id':session,
            'elapsed_seconds':round(time.monotonic()-started,3)}

def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('invalid loopback port')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(observe,'http://127.0.0.1:'+str(args.port),p) for p in PRODUCTS]
            observed=[j.result(timeout=22) for j in jobs]
        if observed[0]['session_id']!=observed[1]['session_id']:
            raise AssertionError('PRODUCT_SOURCE_SESSIONS_DIFFER')
    except Exception as error:
        print('FAIL_REAL_LIVE_SSE_TWO_PRODUCTS '+type(error).__name__+':'+str(error)[:180])
        return 2
    # Never print market-data payloads, auth secrets or raw WebSocket frames.
    print('PASS_REAL_LIVE_SSE_TWO_PRODUCTS '+json.dumps(observed,sort_keys=True))
    return 0
if __name__=='__main__':raise SystemExit(main())
