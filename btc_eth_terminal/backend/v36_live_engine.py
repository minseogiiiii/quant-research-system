"""Bounded v36 live-source admission and incremental state projection.

Uses the unchanged v23 Book and v34 Decimal-depth feature code. Never connects
an exchange, issues an order, or promotes archived messages to a live source.
The trusted operator supplies a freshly spawned collector's already-recorded
JSONL envelopes over an isolated local IPC connection. This is *host observed*
Coinbase public-data evidence, not signed exchange attestation.
"""
from __future__ import annotations

from collections import deque
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import secrets
from threading import Condition, RLock
import time
from datetime import datetime

from v23_causal_microstructure import Book, FeatureReject, ofi_delta
from v35_replay_service import _book_compact, _features, PRODUCTS, StateRejected

MAX_LINE = 18_000_000
MAX_WIRE = 9_000_000
MAX_RETAINED = 256
STALE_MS = 5_000
MAX_RECEIVER_FUTURE_MS = 2_000
MAX_RECEIVER_PAST_MS = 30_000
SAMPLE_MS = 1000
CHANNELS = frozenset(('l2_data','market_trades','heartbeats','subscriptions'))
REQUIRED_ACKS = frozenset(('level2','market_trades','heartbeats'))


def _ms(iso: str) -> int:
    if not isinstance(iso, str) or not iso.endswith('Z'):
        raise StateRejected('BAD_UTC_RECEIVER_CLOCK')
    try:
        value=datetime.fromisoformat(iso.replace('Z','+00:00'))
        return int(value.timestamp()*1000)
    except (ValueError, OverflowError) as exc:
        raise StateRejected('BAD_UTC_RECEIVER_CLOCK') from exc


def _positive(value: object) -> Decimal:
    if not isinstance(value,str) or len(value)>96:
        raise StateRejected('INVALID_TRADE_DECIMAL')
    try:
        x=Decimal(value)
    except (ValueError, InvalidOperation) as exc:
        raise StateRejected('INVALID_TRADE_DECIMAL') from exc
    if not x.is_finite() or x<=0:
        raise StateRejected('INVALID_TRADE_DECIMAL')
    return x


class LiveMarketEngine:
    """Single-writer, multi-reader source-state machine with fail-closed admission.

    mode='ARCHIVE_SIMULATION' is strictly for offline parity tests. A caller
    must opt into LIVE_SOURCE and prove that its own fresh collector child was
    launched; no archive certification can enable live mode. A random run ID
    and a random ID on every new WebSocket connection bound all SSE cursors.
    """
    def __init__(self, *, mode: str='ARCHIVE_SIMULATION', collector_runtime_verified: bool=False,
                 collector_script_sha256: str|None=None, clock_ms=None, monotonic_ns=None):
        if mode not in ('ARCHIVE_SIMULATION','LIVE_SOURCE'):
            raise StateRejected('INVALID_MODE')
        if mode == 'LIVE_SOURCE' and (not collector_runtime_verified or
                not isinstance(collector_script_sha256,str) or
                not re.fullmatch(r'[0-9a-f]{64}',collector_script_sha256)):
            raise StateRejected('LIVE_OPERATOR_PROOF_REQUIRED')
        self.mode=mode
        self._clock=clock_ms or (lambda: time.time_ns()//1_000_000)
        self._mono=monotonic_ns or time.monotonic_ns
        self.collector_script_sha256=collector_script_sha256
        self.run_id=secrets.token_hex(16)
        self._cv=Condition(RLock())
        self.connection_id=None
        self.session_id=None
        self.status='CONNECTING'
        self.reason='AWAITING_COLLECTOR_SOURCE'
        self.seq=-1
        self.last_ms=None
        self.earliest_complete_minute=None
        self.last_producer_mono=None
        self.last_local_mono=None
        self.acks=set()
        self.snapshots=set()
        self.heartbeats=0
        self.last_heartbeat_local_mono=None
        self.gaps=0
        self.last_discontinuity=None
        self.version=0
        self.messages=0
        self.failures=0
        self.books={p:Book() for p in PRODUCTS}
        self.ofi={p:Decimal(0) for p in PRODUCTS}
        self.last_l2={p:None for p in PRODUCTS}
        self.last_pub={p:None for p in PRODUCTS}
        self.recent_trades={p:deque(maxlen=24) for p in PRODUCTS}
        self.candle_open={p:None for p in PRODUCTS}
        self.candles={p:deque(maxlen=120) for p in PRODUCTS}
        self.rows={p:deque(maxlen=MAX_RETAINED) for p in PRODUCTS}

    def _invalid(self, reason: str, phase: str='QUARANTINED') -> None:
        self.status=phase
        self.reason=reason
        self.failures+=1
        self._cv.notify_all()

    def transport_lost(self, reason: str='SOURCE_TRANSPORT_LOST') -> None:
        with self._cv:
            self._invalid(reason, 'RECONNECTING')

    def stopped(self) -> None:
        with self._cv:
            self._invalid('SOURCE_STOPPED','STOPPED')

    def _new_connection(self, conn: int) -> None:
        self.connection_id=conn
        self.session_id=secrets.token_hex(16)  # Never reused across source gap or server restart.
        self.status='WAITING_FOR_SNAPSHOT'
        self.reason='WAITING_FOR_DUAL_SNAPSHOT_AND_ACKS'
        self.seq=-1
        self.last_ms=None
        self.earliest_complete_minute=None
        self.last_producer_mono=None
        self.last_local_mono=None
        self.acks=set()
        self.snapshots=set()
        self.heartbeats=0
        self.last_heartbeat_local_mono=None
        self.books={p:Book() for p in PRODUCTS}
        self.ofi={p:Decimal(0) for p in PRODUCTS}
        self.last_l2={p:None for p in PRODUCTS}
        self.last_pub={p:None for p in PRODUCTS}
        self.recent_trades={p:deque(maxlen=24) for p in PRODUCTS}
        self.candle_open={p:None for p in PRODUCTS}
        self.candles={p:deque(maxlen=120) for p in PRODUCTS}
        self.rows={p:deque(maxlen=MAX_RETAINED) for p in PRODUCTS}
        self._cv.notify_all()

    def _stale_if_necessary(self):
        if self.mode!='LIVE_SOURCE' or self.status!='HEALTHY':return
        now=self._mono()
        if self.last_local_mono is not None and (now-self.last_local_mono)//1_000_000>STALE_MS:
            self._invalid('NO_COLLECTOR_MESSAGES_WITHIN_STALENESS_BUDGET','STALE');return
        if self.last_heartbeat_local_mono is not None and (now-self.last_heartbeat_local_mono)//1_000_000>STALE_MS:
            self._invalid('HEARTBEAT_STALE','STALE');return
        now_ms=self._clock()
        if any(self.last_l2[p] is None or now_ms-self.last_l2[p][0]>STALE_MS for p in PRODUCTS):
            self._invalid('PRODUCT_L2_STALE','STALE')

    def _publish(self, product: str, receiver_ms: int, seq: int) -> None:
        if self.status != 'HEALTHY' or self.last_l2[product] is None:
            return
        received, l2_seq, raw_digest, collector_monotonic = self.last_l2[product]
        if receiver_ms-received>STALE_MS:
            return
        if self.last_pub[product] is not None and receiver_ms-self.last_pub[product]<SAMPLE_MS:
            return
        state=_features(self.books[product],None,self.ofi[product])
        self.version+=1
        row={
            'schema':'v36_source_state_v1', 'product':product,
            'venue':'Coinbase Advanced public spot', 'mode':self.mode,
            'live_feed':self.mode=='LIVE_SOURCE', 'trading_enabled':False,
            'execution_authorized':False, 'independent_exchange_attestation':False,
            'run_id':self.run_id, 'session_id':self.session_id,
            'connection_id':self.connection_id,'source_sequence':l2_seq,
            'source_raw_sha256':raw_digest,'source_monotonic_ns':collector_monotonic,
            'source_event_version':1,
            'receiver_time_ms':receiver_ms,'source_last_l2_receiver_ms':received,
            'source_age_ms':receiver_ms-received, 'state_version':self.version,
            'source_admission':'HOST_OBSERVED_LIVE_SOURCE' if self.mode=='LIVE_SOURCE'
                              else 'OFFLINE_ARCHIVE_SIMULATION_NOT_LIVE',
            'status':'VALID_OBSERVED_QUOTE', 'freshness':'CURRENT_OBSERVED_NOT_EXCHANGE_ATTESTED',
            'recent_trades':list(self.recent_trades[product]), **state,
        }
        self.rows[product].append(row)
        self.last_pub[product]=receiver_ms
        self._cv.notify_all()

    def _update_candles(self, product: str, trade: dict, receiver_ms: int) -> None:
        p=_positive(trade.get('price'));q=_positive(trade.get('size'))
        start=receiver_ms//60_000*60_000
        previous=self.candle_open[product]
        if previous is not None and previous['time']!=start:
            previous['available_at']=receiver_ms
            self.candles[product].append(previous)
            previous=None
        if previous is None:
            previous={'time':start,'open':str(p),'high':str(p),'low':str(p),
                      'close':str(p),'volume':str(q),'observed_trade_count':1}
        else:
            previous['high']=str(max(Decimal(previous['high']),p))
            previous['low']=str(min(Decimal(previous['low']),p))
            previous['close']=str(p)
            previous['volume']=str(Decimal(previous['volume'])+q)
            previous['observed_trade_count']+=1
        self.candle_open[product]=previous

    def _complete_candles(self,recv:int)->None:
        # Completion is only known as of the next actually received message.
        for p in PRODUCTS:
            open_bar=self.candle_open[p]
            if open_bar is not None and recv>=open_bar['time']+60_000:
                if open_bar['time']>=self.earliest_complete_minute:
                    open_bar['available_at']=recv
                    self.candles[p].append(open_bar)
                self.candle_open[p]=None

    def consume(self, envelope_line: bytes) -> None:
        """Accept one *already archived by the collector* envelope, or quarantine.

        Offline test calls must instantiate ARCHIVE_SIMULATION, never LIVE_SOURCE.
        No acceptance occurs merely because a data file looks like Coinbase.
        """
        with self._cv:
            try:
                self._consume(envelope_line)
            except (StateRejected, FeatureReject, ValueError, KeyError, TypeError, ArithmeticError,
                    UnicodeError, OverflowError) as exc:
                self._invalid('SOURCE_ENVELOPE_REJECTED:'+str(exc)[:120]);raise StateRejected(self.reason) from exc

    def _consume(self, line: bytes) -> None:
        if not isinstance(line,bytes) or not 0<len(line)<=MAX_LINE or not line.endswith(b'\n'):
            raise StateRejected('INCOMPLETE_OR_OVERSIZED_ARCHIVAL_RECORD')
        if self.status=='STOPPED':
            raise StateRejected('SOURCE_STOPPED')
        envelope=json.loads(line)
        if not isinstance(envelope,dict):raise StateRejected('BAD_ENVELOPE')
        raw=envelope.get('raw_text')
        if not isinstance(raw,str):raise StateRejected('BAD_RAW_TEXT')
        raw_bytes=raw.encode('utf8')
        if len(raw_bytes)>MAX_WIRE or sha256(raw_bytes).hexdigest()!=envelope.get('sha256'):
            raise StateRejected('RAW_HASH_OR_LENGTH_MISMATCH')
        wire=json.loads(raw)
        if not isinstance(wire,dict):raise StateRejected('INVALID_WIRE')
        seq=wire.get('sequence_num')
        conn=envelope.get('connection_id')
        if type(seq) is not int or seq<0 or type(conn) is not int or conn<0:
            raise StateRejected('INVALID_SOURCE_SEQUENCE_OR_CONNECTION')
        if self.status in ('QUARANTINED','STALE','RECONNECTING') and not (self.connection_id is not None and conn==self.connection_id+1 and seq==0):
            raise StateRejected('SOURCE_REQUIRES_NEW_CONNECTION')
        if self.connection_id is not None and conn!=self.connection_id:
            self.gaps+=1
            self.last_discontinuity='NEW_COLLECTOR_CONNECTION_WITH_SOURCE_GAP'
        if self.connection_id is None or conn!=self.connection_id:
            if self.connection_id is None and conn!=0:
                raise StateRejected('INITIAL_CONNECTION_ID_NOT_ZERO')
            if self.connection_id is not None and (conn!=self.connection_id+1 or seq!=0):
                raise StateRejected('UNANNOUNCED_OR_NONCONTIGUOUS_CONNECTION')
            if seq!=0:raise StateRejected('NEW_CONNECTION_NOT_BEGINNING_AT_SEQUENCE_ZERO')
            self._new_connection(conn)
        if seq!=self.seq+1:raise StateRejected('SOURCE_SEQUENCE_GAP')
        recv=_ms(envelope.get('received_utc'))
        mono=envelope.get('local_monotonic_ns')
        if not isinstance(mono,str) or not mono.isascii() or not mono.isdigit() or len(mono)>32:
            raise StateRejected('INVALID_COLLECTOR_MONOTONIC')
        producer_ns=int(mono)
        if self.last_ms is not None and recv<self.last_ms:
            raise StateRejected('RECEIVER_WALL_TIME_REGRESSION')
        if self.last_producer_mono is not None and producer_ns<=self.last_producer_mono:
            raise StateRejected('COLLECTOR_MONOTONIC_REGRESSION')
        if self.mode=='LIVE_SOURCE':
            now=self._clock()
            if recv>now+MAX_RECEIVER_FUTURE_MS or now-recv>MAX_RECEIVER_PAST_MS:
                raise StateRejected('LIVE_SOURCE_RECEIVER_TIME_NOT_FRESH')
        channel=wire.get('channel')
        events=wire.get('events')
        if channel not in CHANNELS or not isinstance(events,list):
            raise StateRejected('UNSUPPORTED_CHANNEL')
        self.seq=seq
        self.messages+=1
        if self.earliest_complete_minute is None:
            self.earliest_complete_minute=((recv+59_999)//60_000)*60_000
        self.last_ms=recv
        self.last_producer_mono=producer_ns
        self.last_local_mono=self._mono()
        self._complete_candles(recv)
        if channel=='subscriptions':
            for ev in events:
                if not isinstance(ev,dict) or not isinstance(ev.get('subscriptions'),dict):
                    raise StateRejected('BAD_SUBSCRIPTION_ACK')
                for key,products in ev['subscriptions'].items():
                    if (key not in REQUIRED_ACKS or not isinstance(products,list) or
                        (products!=['heartbeats'] if key=='heartbeats' else
                         set(products)!=set(PRODUCTS) or len(products)!=2)):
                        raise StateRejected('INVALID_SUBSCRIPTION_PRODUCTS_OR_CHANNEL')
                    self.acks.add(key)
        elif channel=='heartbeats':
            self.heartbeats+=1
            self.last_heartbeat_local_mono=self._mono()
        elif channel=='market_trades':
            for ev in events:
                if not isinstance(ev,dict) or ev.get('type') not in ('snapshot','update'):
                    raise StateRejected('BAD_TRADE_EVENT')
                for trade in ev.get('trades',[]):
                    if not isinstance(trade,dict) or trade.get('product_id') not in PRODUCTS:
                        raise StateRejected('BAD_TRADE_PRODUCT')
                    p=trade['product_id']
                    if ev['type']=='update':
                        self._update_candles(p,trade,recv)
                    if self.status=='HEALTHY':
                        self.recent_trades[p].append({
                          'price':trade['price'],'size':trade['size'],
                          'maker_side':trade.get('side'),'received_at_ms':recv,
                          'trade_id':trade.get('trade_id')})
        elif channel=='l2_data':
            for ev in events:
                if (not isinstance(ev,dict) or ev.get('product_id') not in PRODUCTS or
                    ev.get('type') not in ('snapshot','update') or not isinstance(ev.get('updates'),list)):
                    raise StateRejected('BAD_L2_EVENT')
                p=ev['product_id']
                book=self.books[p]
                before=book.best()
                if ev['type']=='snapshot':
                    if p in self.snapshots:raise StateRejected('DUPLICATE_PRODUCT_SNAPSHOT')
                    self.snapshots.add(p)
                elif p not in self.snapshots:
                    raise StateRejected('UPDATE_BEFORE_SNAPSHOT')
                book.apply(ev['type'],ev['updates'])
                _book_compact(book)
                after=book.best()
                if after is None:raise StateRejected('L2_INCOMPLETE_AFTER_EVENT')
                if ev['type']=='update':self.ofi[p]+=ofi_delta(before,after)
                self.last_l2[p]=(recv,seq,envelope['sha256'],mono)
                if self.status=='HEALTHY':self._publish(p,recv,seq)
        if (self.status=='WAITING_FOR_SNAPSHOT' and self.snapshots==set(PRODUCTS) and
            self.heartbeats>0 and self.acks==set(REQUIRED_ACKS)):
            self.status='HEALTHY'
            self.reason=None
            for p in PRODUCTS:self._publish(p,recv,seq)
        self._cv.notify_all()

    def health(self) -> dict:
        with self._cv:
            self._stale_if_necessary()
            return {'schema':'v36_live_source_health_v1','mode':self.mode,
                'live_feed':self.mode=='LIVE_SOURCE', 'trading_enabled':False,
                'execution_authorized':False, 'exchange_signed_attestation':False,
                'run_id':self.run_id,'session_id':self.session_id,
                'connection_id':self.connection_id,'status':self.status,'reason':self.reason,
                'last_sequence':self.seq,'last_receiver_time_ms':self.last_ms,
                'products':list(PRODUCTS),'snapshots':sorted(self.snapshots),
                'subscriptions':sorted(self.acks),'heartbeats':self.heartbeats,
                'messages':self.messages,'state_version':self.version,'failures':self.failures,
                'connection_gaps':self.gaps,'last_discontinuity':self.last_discontinuity,
                'collector_script_sha256':self.collector_script_sha256}

    def snapshot(self, product: str) -> dict:
        if product not in PRODUCTS:raise StateRejected('UNKNOWN_PRODUCT')
        with self._cv:
            self._stale_if_necessary()
            valid=self.status=='HEALTHY' and bool(self.rows[product])
            return {'schema':'v36_live_snapshot_v1','mode':self.mode,
                    'product':product,'status':self.status,'session_id':self.session_id,
                    'state':self.rows[product][-1] if valid else None,
                    'trading_enabled':False,'execution_authorized':False,
                    'resync_required':not valid}

    def state_at_version(self, product: str, session: str, version: int) -> dict:
        """Bounded exact-state lookup for live browser and forensic parity.

        This cannot return old prices after stale or source-session loss.
        An unavailable version must produce RESYNC_REQUIRED, not latest state.
        """
        if (product not in PRODUCTS or not isinstance(session,str)
                or not re.fullmatch(r'[0-9a-f]{32}',session)
                or type(version) is not int or version<1):
            raise StateRejected('INVALID_VERSIONED_SOURCE_REQUEST')
        with self._cv:
            self._stale_if_necessary()
            if self.status!='HEALTHY' or session!=self.session_id:
                raise StateRejected('RESYNC_REQUIRED')
            for row in self.rows[product]:
                if row['state_version']==version and row['session_id']==session:
                    return {'schema':'v36_version_pinned_state_v1',
                            'product':product,'session_id':session,'state':dict(row),
                            'trading_enabled':False}
            raise StateRejected('RESYNC_REQUIRED')

    def candles_page(self, product: str) -> dict:
        if product not in PRODUCTS:raise StateRejected('UNKNOWN_PRODUCT')
        with self._cv:
            self._stale_if_necessary()
            return {'schema':'v36_trade_candles_v1','mode':self.mode,'product':product,
                    'session_id':self.session_id,'status':self.status,
                    'rows':list(self.candles[product]) if self.status=='HEALTHY' else [],
                    'incomplete_candle_never_published':True,'live_feed':self.mode=='LIVE_SOURCE',
                    'trading_enabled':False}

    def poll(self, product: str, session: str, after: int, timeout: float=1.5) -> list[dict]:
        if product not in PRODUCTS or type(after) is not int or after<0 or not 0<timeout<=3:
            raise StateRejected('INVALID_LIVE_SUBSCRIPTION')
        with self._cv:
            self._stale_if_necessary()
            if self.status!='HEALTHY' or self.session_id!=session:
                raise StateRejected('RESYNC_REQUIRED')
            if after>0 and after<self.version and not any(r['state_version']==after for r in self.rows[product]):
                raise StateRejected('RESYNC_REQUIRED')
            if after>self.version:raise StateRejected('RESYNC_REQUIRED')
            if not any(row['state_version']>after for row in self.rows[product]):
                self._cv.wait(timeout)
            self._stale_if_necessary()
            if self.status!='HEALTHY' or self.session_id!=session:
                raise StateRejected('RESYNC_REQUIRED')
            if after>0 and after<self.version and not any(r['state_version']==after for r in self.rows[product]):
                raise StateRejected('RESYNC_REQUIRED')
            return [r for r in self.rows[product] if r['state_version']>after]
