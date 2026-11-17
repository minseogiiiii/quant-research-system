"""Genuine Coinbase public LIVE SSE fanout and operator health stress.

Requires a separately running authorized v36 LIVE_SOURCE observer. Never
accepts a synthetic source or archived replay. No orders or private APIs.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
from urllib.request import urlopen

PRODUCTS=('BTC-USD','ETH-USD')
TOKEN=re.compile(r'v36:([0-9a-f]{32}):(BTC-USD|ETH-USD):([1-9][0-9]*)\Z')

def get(origin,path):
    with urlopen(origin+path,timeout=2) as r:
        if r.status!=200:raise AssertionError('MONITOR_HTTP_NON200')
        return json.load(r)

def rss(pid):
    try:
        for line in (Path('/proc')/str(pid)/'status').read_text().splitlines():
            if line.startswith('VmRSS:'):return int(line.split()[1])*1024
    except (OSError,ValueError,IndexError):return None
    return None

def run(origin,duration,pid):
    started=time.monotonic()
    for _ in range(80):
        try:
            h=get(origin,'/api/v36/health')
            if (h.get('mode')=='LIVE_SOURCE' and h.get('status')=='HEALTHY'
                and h.get('live_feed') is True and h.get('trading_enabled') is False
                and set(h.get('snapshots',[]))==set(PRODUCTS)):break
        except (OSError,ValueError):pass
        time.sleep(.5)
    else:raise AssertionError('REAL_PUBLIC_SOURCE_NEVER_ADMITTED')
    session=h['session_id']
    end=time.monotonic()+duration
    stat=[{'client':i,'product':PRODUCTS[i%2],'states':0,'snapshots':0,
           'heartbeats':0,'resync':0,'last_version':0,'errors':[]} for i in range(8)]
    def consume(z):
        url=origin+'/api/v36/stream?product='+z['product']
        source=None;kind=None;data=None;cursor=None
        with urlopen(url,timeout=15) as stream:
            if (stream.status!=200 or
                'text/event-stream' not in stream.headers.get('Content-Type','')):
                raise AssertionError('NO_SSE_SUBSCRIPTION')
            while time.monotonic()<end:
                raw=stream.readline(200001)
                if not raw:break
                if len(raw)>200000:raise AssertionError('UNBOUNDED_SSE_LINE')
                line=raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('event: '):kind=line[7:]
                elif line.startswith('data: '):data=json.loads(line[6:])
                elif line.startswith('id: '):cursor=line[4:]
                elif not line:
                    if kind=='source':
                        if (data.get('mode')!='LIVE_SOURCE' or
                            data.get('session_id')!=session or data.get('product')!=z['product']
                            or data.get('trading_enabled') is not False):
                            raise AssertionError('SOURCE_HANDSHAKE_INVALID')
                        source=data['session_id']
                    elif kind in ('snapshot','state'):
                        if (source!=session or data.get('mode')!='LIVE_SOURCE'
                            or data.get('live_feed') is not True or
                            data.get('product')!=z['product'] or data.get('session_id')!=session
                            or data.get('trading_enabled') is not False):
                            raise AssertionError('UNTRUSTED_SSE_STATE')
                        m=TOKEN.fullmatch(cursor or '')
                        if (not m or m.group(1)!=session or m.group(2)!=z['product']
                            or int(m.group(3))!=data['state_version']):
                            raise AssertionError('NON_SOURCE_BOUND_CURSOR')
                        if z['last_version'] and data['state_version']<=z['last_version']:
                            raise AssertionError('SSE_VERSION_DUPLICATE_OR_REGRESSION')
                        z['last_version']=data['state_version']
                        if kind=='snapshot':z['snapshots']+=1
                        else:z['states']+=1
                        if z['client']==7:time.sleep(.25)
                    elif kind=='heartbeat':
                        if data.get('session_id')!=session:
                            raise AssertionError('HEARTBEAT_SESSION_MISMATCH')
                        z['heartbeats']+=1
                    elif kind=='resync_required':z['resync']+=1;break
                    elif kind=='stream_rotation':break
                    kind=data=cursor=None
    def observed(z):
        try:consume(z)
        except Exception as exc:
            z['errors'].append(type(exc).__name__+':'+str(exc)[:150])
    metrics=[]
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs=[pool.submit(observed,z) for z in stat]
        while time.monotonic()<end:
            t=time.monotonic()
            try:
                health=get(origin,'/api/v36/health')
                mid=time.monotonic()
                m=get(origin,'/api/v36/metrics')
                if (health.get('mode')!='LIVE_SOURCE' or health.get('status')!='HEALTHY'
                    or health.get('session_id')!=session or m.get('max_clients')!=10
                    or m.get('max_sse_clients')!=8 or m.get('source_healthy') is not True):
                    raise AssertionError('SOURCE_OR_TRANSPORT_NOT_HEALTHY')
                metrics.append({'health_ms':(mid-t)*1000,'rss':rss(pid),
                                'connections':m['active_sse_connections']})
            except Exception as exc:
                raise AssertionError('LIVE_HEALTH_METRICS_UNAVAILABLE:'+str(exc)[:100])
            time.sleep(.4)
        for job in jobs:job.result(timeout=20)
    problems=[]
    if len(metrics)<80:problems.append('TOO_FEW_HEALTH_SAMPLES')
    for s in stat:
        if s['errors']:problems.append('SSE_CLIENT_ERROR:'+str(s['client']))
        if s['snapshots']!=1:problems.append('SSE_INITIAL_SNAPSHOT_MISSING:'+str(s['client']))
        if s['client']!=7 and s['states']<20:
            problems.append('INSUFFICIENT_REAL_STATE_EVENTS:'+str(s['client']))
        if s['client']!=7 and s['resync']:
            problems.append('NORMAL_CLIENT_NEEDS_UNEXPECTED_RESYNC:'+str(s['client']))
        if s['heartbeats']>duration/3+5:
            problems.append('EXCESSIVE_SSE_HEARTBEATS:'+str(s['client']))
    def q95(values):
        a=sorted(values)
        return round(a[min(len(a)-1,int(.95*(len(a)-1)))],2) if a else None
    return {'schema':'v36_3_genuine_public_live_fanout_v1',
      'status':'PASS' if not problems else 'FAIL',
      'mode':'REAL_PUBLIC_COINBASE_LIVE_NOT_ARCHIVE',
      'elapsed_seconds':round(time.monotonic()-started,2),
      'health_samples':len(metrics),
      'health_latency_p95_ms':q95([x['health_ms'] for x in metrics]),
      'observer_rss_peak_bytes':max((x['rss'] or 0 for x in metrics),default=0),
      'max_active_sse':max((x['connections'] for x in metrics),default=0),
      'clients':stat,'problems':problems,
      'trading_enabled':False,'certified_24h':False}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port',type=int,default=8766)
    p.add_argument('--seconds',type=int,default=100)
    p.add_argument('--observer-pid',type=int,required=True)
    args=p.parse_args()
    if not 60<=args.seconds<=240:p.error('60..240 seconds only')
    try:result=run('http://127.0.0.1:'+str(args.port),args.seconds,args.observer_pid)
    except Exception as exc:
        result={'schema':'v36_3_genuine_public_live_fanout_v1','status':'FAIL',
                'reason':type(exc).__name__+':'+str(exc)[:220],
                'mode':'REAL_PUBLIC_LIVE_PROBE_NO_FIXTURE',
                'trading_enabled':False,'certified_24h':False}
    print(('PASS' if result['status']=='PASS' else 'FAIL')+
          '_REAL_PUBLIC_LIVE_FANOUT '+json.dumps(result,sort_keys=True),flush=True)
    return 0 if result['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
