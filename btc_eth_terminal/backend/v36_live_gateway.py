"""Read-only persistent SSE extension of the existing v35.1 Gateway.

Archive endpoints and original historical Funnest routes keep their contracts.
Live sessions and cursors are completely separate from archive replay cursors.
"""
from __future__ import annotations
import json
import re
import time
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from v35_unified_gateway import BoundedServer, UnifiedHandler
from v35_replay_service import StateRejected, ReplayDataset
from v36_live_engine import LiveMarketEngine, PRODUCTS, MAX_RETAINED

LIVE_CURSOR = re.compile(r'v36:([0-9a-f]{32}):(BTC-USD|ETH-USD):([1-9][0-9]{0,12})\Z')
MAX_STREAM_SECONDS = 300
MAX_EVENTS_PER_BURST = 64

class LiveHandler(UnifiedHandler):
    live_engine: LiveMarketEngine | None=None
    protocol_version='HTTP/1.1'

    def _reply(self,code:int,data:dict):
        self.send_obj(code,data)

    def do_GET(self):
        if len(self.path)>512:
            self._reply(414,{'error':'REQUEST_TARGET_TOO_LONG','trading_enabled':False})
            return
        parsed=urlsplit(self.path)
        from v36_frontend_integrity import LIVE_JS, LIVE_CSS
        if parsed.path in ('/research/v36','/research/v36/index.html',LIVE_JS,LIVE_CSS):
            try:
                if parsed.query:raise ValueError('NO_ASSET_QUERY_PARAMETERS')
                from v36_frontend_integrity import v36_asset
                body,content_type=v36_asset(parsed.path)
                self.send_response(200)
                for key,val in [('Content-Type',content_type+'; charset=utf-8'),
                     ('Content-Length',str(len(body))),('Cache-Control','no-store'),
                     ('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),
                     ('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; frame-ancestors 'none'")]:
                    self.send_header(key,val)
                self.end_headers();self.wfile.write(body)
            except (ValueError,OSError,KeyError,TypeError) as exc:
                self.send_obj(503,{'error':'V36_FRONTEND_INTEGRITY_FAILURE','live_feed':False})
            return
        if not parsed.path.startswith('/api/v36/'):
            return super().do_GET()
        source=self.live_engine
        if source is None:
            self._reply(503,{'error':'NO_LIVE_SOURCE_CONFIGURED','trading_enabled':False})
            return
        permitted={
            '/api/v36/health':set(),
            '/api/v36/metrics':set(),
            '/api/v36/snapshot':{'product'},
            '/api/v36/state':{'product','session','version'},
            '/api/v36/candles':{'product'},
            '/api/v36/stream':{'product'},
        }
        if parsed.path not in permitted:
            self._reply(404,{'error':'UNKNOWN_READ_ONLY_ROUTE','trading_enabled':False});return
        try:
            args=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=3)
            if any(k not in permitted[parsed.path] or len(v)!=1 or not v[0] for k,v in args.items()):
                raise StateRejected('INVALID_QUERY')
            if parsed.path=='/api/v36/health':
                self._reply(200,source.health());return
            if parsed.path=='/api/v36/metrics':
                health=source.health()
                counters=self.server.metrics()
                # The v35 metrics contract remains archived-only.
                # This separate route carries actual live source semantics.
                self._reply(200,{
                    'schema':'v36_live_transport_metrics_v1',
                    'mode':health['mode'], 'live_feed':health['live_feed'],
                    'source_status':health['status'],
                    'source_healthy':health['status']=='HEALTHY',
                    'source_session_id':health['session_id'],
                    'trading_enabled':False,'execution_authorized':False,
                    'max_clients':counters['max_clients'],
                    'max_sse_clients':counters['max_sse_clients'],
                    'active_sse_connections':counters['active_sse_connections'],
                    'sse_rejected_slots':counters['sse_rejected_slots'],
                    'max_events_per_burst':MAX_EVENTS_PER_BURST,
                    'max_stream_seconds':MAX_STREAM_SECONDS,
                    'state_retention_per_product':MAX_RETAINED,
                    **{k:counters[k] for k in (
                        'accepted_requests','rejected_client_slots',
                        'active_connections','peak_connections',
                        'sse_started','sse_completed','sse_aborted',
                        'resync_required')},
                });return
            product=args.get('product',[None])[0]
            if product not in PRODUCTS:raise StateRejected('UNKNOWN_PRODUCT')
            if parsed.path=='/api/v36/snapshot':
                snap=source.snapshot(product)
                self._reply(200 if snap['state'] is not None else 503,snap)
                return
            if parsed.path=='/api/v36/state':
                if 'session' not in args or 'version' not in args:
                    raise StateRejected('INVALID_VERSIONED_SOURCE_REQUEST')
                version_text=args['version'][0]
                if not re.fullmatch(r'[1-9][0-9]{0,12}',version_text):
                    raise StateRejected('INVALID_VERSIONED_SOURCE_REQUEST')
                self._reply(200,source.state_at_version(product,args['session'][0],int(version_text)))
                return
            if parsed.path=='/api/v36/candles':
                self._reply(200,source.candles_page(product));return
            self._stream(source,product)
        except (StateRejected,ValueError,KeyError) as exc:
            kind=str(exc)
            code=409 if 'RESYNC_REQUIRED' in kind else 400
            self._reply(code,{'error':kind,'mode':source.mode,'trading_enabled':False})
        except (ConnectionError,OSError,TimeoutError):
            return

    def _stream(self,source:LiveMarketEngine,product:str):
        # Persistent SSE must not exhaust the monitoring connection slots.
        if not self.server.sse_slots.acquire(blocking=False):
            self.server.count('sse_rejected_slots')
            self._reply(503,{'error':'LIVE_SSE_SUBSCRIPTION_LIMIT',
                             'trading_enabled':False,'resync_required':True})
            return
        self.server.count('active_sse_connections')
        try:
            return self._stream_inner(source,product)
        finally:
            self.server.count('active_sse_connections',-1)
            self.server.sse_slots.release()

    def _stream_inner(self,source:LiveMarketEngine,product:str):
        # Atomic source/initial-book binding. Old separate health() and
        # snapshot() calls could cross a live collector session rotation.
        snap=source.stream_initial(product)
        session=snap['session_id']
        last=self.headers.get('Last-Event-ID')
        if last:
            match=LIVE_CURSOR.fullmatch(last)
            if match is None:raise StateRejected('INVALID_LIVE_CURSOR_RESYNC_REQUIRED')
            token_session,token_product,value=match.groups()
            if token_session!=session or token_product!=product:
                raise StateRejected('SOURCE_OR_PRODUCT_CHANGED_RESYNC_REQUIRED')
            after=int(value)
            # Validate that history has not expired BEFORE sending HTTP 200.
            source.poll(product,session,after,timeout=0.001)
        else:
            after=snap['state']['state_version']
        self.send_response(200)
        for k,v in [('Content-Type','text/event-stream; charset=utf-8'),
                    ('Cache-Control','no-store'),('Connection','close'),
                    ('X-Content-Type-Options','nosniff'),('X-Accel-Buffering','no')]:
            self.send_header(k,v)
        self.end_headers()
        self.close_connection=True
        self.server.count('sse_started')
        def emit(event:str,data:dict,cursor=None):
            lines=[]
            if cursor is not None:lines.append('id: '+cursor+'\n')
            lines.extend(('event: '+event+'\n',
                        'data: '+json.dumps(data,separators=(',',':'),allow_nan=False)+'\n\n'))
            raw=''.join(lines).encode()
            if len(raw)>200_000:raise StateRejected('SSE_FRAME_TOO_LARGE')
            self.wfile.write(raw)
            self.wfile.flush()
        try:
            emit('source',{'schema':'v36_live_source_handshake_v1','mode':source.mode,
                           'session_id':session,'product':product,
                           'cursor_contract':'v36:session_id:product:state_version',
                           'trading_enabled':False,'execution_authorized':False})
            if not last:
                emit('snapshot',snap['state'],f'v36:{session}:{product}:{after}')
            started=time.monotonic()
            while time.monotonic()-started<MAX_STREAM_SECONDS:
                updates=source.poll(product,session,after,timeout=1.0)
                if len(updates)>MAX_EVENTS_PER_BURST:
                    raise StateRejected('RESYNC_REQUIRED')
                if not updates:
                    emit('heartbeat',{'mode':source.mode,'session_id':session,
                                      'last_state_version':after,'trading_enabled':False})
                    continue
                for row in updates:
                    if row['state_version']<=after or row['session_id']!=session:
                        raise StateRejected('RESYNC_REQUIRED')
                    cursor=f'v36:{session}:{product}:{row["state_version"]}'
                    emit('state',row,cursor)
                    after=row['state_version']
            emit('stream_rotation',{'reason':'BOUNDED_CONNECTION_LIFETIME','after':after,
                                    'session_id':session})
            self.server.count('sse_completed')
        except (StateRejected,) as exc:
            self.server.count('resync_required')
            try:emit('resync_required',{'reason':str(exc),'session_id':session})
            except (OSError,TimeoutError):pass
        except (BrokenPipeError,ConnectionError,TimeoutError,OSError):
            self.server.count('sse_aborted')


class LiveBoundedServer(BoundedServer):
    """Eight persistent SSE subscriptions plus two short monitoring slots.

    The historical v35 BoundedServer is unchanged at eight total slots.
    """
    max_clients=10
    max_sse_clients=8

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.sse_slots=BoundedSemaphore(self.max_sse_clients)
        self._stats.update({'active_sse_connections':0,
                            'sse_rejected_slots':0})

    def metrics(self):
        m=super().metrics()
        m['max_sse_clients']=self.max_sse_clients
        return m


def server_for_live(engine:LiveMarketEngine,host='127.0.0.1',port=0,
                    historical_dataset:ReplayDataset|None=None)->BoundedServer:
    if host not in ('127.0.0.1','::1'):
        raise StateRejected('LOOPBACK_ONLY')
    class BoundHandler(LiveHandler):
        pass
    BoundHandler.live_engine=engine
    BoundHandler.dataset=historical_dataset
    return LiveBoundedServer((host,port),BoundHandler)
