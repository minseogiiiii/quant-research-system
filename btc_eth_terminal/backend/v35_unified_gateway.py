"""Same-origin bounded v35.1 read-only replay gateway, mounted on the v34 bridge.

Retains /research, /terminal, /api/v30/* and /api/v34/* unchanged. Never opens
exchange clients or order routes. Replay is immutable and fully prevalidated.
"""
from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, urlsplit

from bridge import Handler as HistoricalHandler
from v35_replay_service import replay_capture, ReplayDataset, StateRejected, PRODUCTS

MAX_SSE_EVENTS = 60
# EventSource resumes only when archive, product and state version all agree.
CURSOR_RE = re.compile(r'([0-9a-f]{64}):(BTC-USD|ETH-USD):([1-9][0-9]{0,12})\Z')
CLIENT_SOCKET_TIMEOUT_SECONDS = 3.0

class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    max_clients = 8

    def __init__(self, address, handler):
        self.slots = BoundedSemaphore(self.max_clients)
        self._stats_lock = Lock()
        self._stats = {'accepted_requests': 0, 'rejected_client_slots': 0,
                       'active_connections': 0, 'peak_connections': 0,
                       'sse_started': 0, 'sse_completed': 0,
                       'sse_aborted': 0, 'resync_required': 0}
        super().__init__(address, handler)

    def get_request(self):
        # Slow peers may never finish HTTP headers or read an SSE response.
        # Time out both read and write operations.
        sock, address = super().get_request()
        sock.settimeout(CLIENT_SOCKET_TIMEOUT_SECONDS)
        return sock, address

    def count(self, name: str, delta: int = 1) -> None:
        with self._stats_lock:
            self._stats[name] += delta
            if name == 'active_connections':
                self._stats['peak_connections'] = max(self._stats['peak_connections'],
                                                       self._stats['active_connections'])

    def metrics(self) -> dict:
        with self._stats_lock:
            values = dict(self._stats)
        return {'schema': 'v35_1_finite_gateway_metrics_v1',
                'mode': 'ARCHIVE_REPLAY', 'live_feed': False,
                'trading_enabled': False,
                'max_clients': self.max_clients,
                'socket_timeout_seconds': CLIENT_SOCKET_TIMEOUT_SECONDS,
                'max_sse_events_per_response': MAX_SSE_EVENTS,
                'sse_queue_depth': 0,  # Finite precomputed SSE, no persistent queue.
                **values}

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.count('rejected_client_slots')
            self.shutdown_request(request)
            return
        self.count('accepted_requests')
        self.count('active_connections')
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.count('active_connections', -1)
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.count('active_connections', -1)
            self.slots.release()


class UnifiedHandler(HistoricalHandler):
    dataset: ReplayDataset | None = None

    def do_GET(self):
        # Preserve the existing historical handler for all unmodified routes.
        if len(self.path) > 512:
            self.send_obj(414, {'error': 'REQUEST_TARGET_TOO_LONG', 'live_feed': False})
            return
        parsed = urlsplit(self.path)
        # Independently pin the new candidate without replacing the working
        # historical /research and /terminal routes or their original assets.
        from v35_1_frontend_integrity import candidate_asset, CANDIDATE_JS, CANDIDATE_CSS
        if parsed.path in ('/research/v35.1', '/research/v35.1/index.html', CANDIDATE_JS, CANDIDATE_CSS):
            try:
                if parsed.query:
                    raise ValueError('Candidate assets do not accept query parameters')
                body, content_type = candidate_asset(parsed.path)
            except (ValueError, OSError) as exc:
                self.send_obj(503, {'error':'SHA_PINNED_V35_1_FRONTEND_UNAVAILABLE', 'live_feed':False})
                return
            self.send_response(200)
            for key,value in [('Content-Type',content_type+'; charset=utf-8'),
                  ('Content-Length',str(len(body))),('Cache-Control','no-store'),
                  ('X-Content-Type-Options','nosniff'),('X-Frame-Options','DENY'),
                  ('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self' data:; base-uri 'none'; object-src 'none'; frame-ancestors 'none'")]:
                self.send_header(key,value)
            self.end_headers();self.wfile.write(body)
            return
        if not parsed.path.startswith('/api/v35/'):
            return super().do_GET()
        sse_response_started = False
        try:
            if self.dataset is None:
                self.send_obj(503, {'error': 'NO_VERIFIED_ARCHIVE', 'live_feed': False})
                return
            route = parsed.path
            permitted = {
                '/api/v35/health': set(),
                '/api/v35/metrics': set(),
                '/api/v35/snapshot': {'product', 'version'},
                '/api/v35/history': {'product', 'after', 'limit'},
                '/api/v35/candles': {'product', 'limit'},
                '/api/v35/stream': {'product', 'after', 'limit'},
            }
            if route not in permitted:
                self.send_obj(404, {'error': 'UNKNOWN_READ_ONLY_ROUTE'})
                return
            args = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            if any(k not in permitted[route] or len(v) != 1 or not v[0] for k, v in args.items()):
                raise StateRejected('INVALID_QUERY_OR_DUPLICATE_PARAMETER')
            def one(k: str) -> str:
                vals = args.get(k)
                if not vals or len(vals) != 1:
                    raise StateRejected(f'MISSING_{k.upper()}')
                return vals[0]
            def numeric(k: str, default: int | None = None) -> int:
                raw = args.get(k, [None if default is None else str(default)])[0]
                if raw is None or re.fullmatch(r'(0|[1-9]\d{0,12})', raw) is None:
                    raise StateRejected(f'INVALID_{k.upper()}')
                return int(raw)
            if route == '/api/v35/metrics':
                self.send_obj(200, self.server.metrics())
                return
            if route == '/api/v35/health':
                self.send_obj(200, {'schema': 'v35_1_health_v1',
                                    'mode': 'ARCHIVE_REPLAY', 'status': 'VERIFIED_ARCHIVE_ONLY',
                                    'source_admission': self.dataset.certificate['admission'],
                                    'source_manifest_sha256': self.dataset.certificate['manifest_sha256'],
                                    'messages': self.dataset.message_count,
                                    'final_state_sha256': self.dataset.final_state_sha256,
                                    'products': list(PRODUCTS),
                                    'freshness': 'ARCHIVED_NOT_LIVE',
                                    'live_feed': False, 'trading_enabled': False,
                                    'execution_authorized': False})
                return
            product = one('product')
            if product not in PRODUCTS:
                raise StateRejected('UNKNOWN_PRODUCT')
            if route == '/api/v35/snapshot':
                at_version = numeric('version') if 'version' in args else None
                self.send_obj(200, self.dataset.snapshot(product, at_version))
                return
            if route == '/api/v35/candles':
                self.send_obj(200, self.dataset.candles_page(product, numeric('limit', 400)))
                return
            after = numeric('after', 0)
            limit = numeric('limit', 50)
            if route == '/api/v35/history':
                self.send_obj(200, self.dataset.page(product, after, limit))
                return
            if limit > MAX_SSE_EVENTS:
                raise StateRejected('STREAM_LIMIT_EXCEEDED')
            # Numeric URL 'after' positions contain no archive/product identity.
            # Resume SSE only using the source-qualified Last-Event-ID contract.
            if after > 0:
                raise StateRejected('RESYNC_REQUIRED')
            last_id = self.headers.get('Last-Event-ID')
            if last_id:
                if 'after' in args:
                    raise StateRejected('INVALID_SSE_RESUME_CURSOR')
                match = CURSOR_RE.fullmatch(last_id)
                if match is None:
                    # Numeric legacy IDs cannot establish source continuity.
                    if re.fullmatch(r'[1-9]\d{0,12}', last_id):
                        raise StateRejected('UNSCOPED_CURSOR_RESYNC_REQUIRED')
                    raise StateRejected('INVALID_SSE_RESUME_CURSOR')
                manifest_pin, cursor_product, version = match.groups()
                if manifest_pin != self.dataset.certificate['manifest_sha256']:
                    raise StateRejected('SOURCE_CHANGED_RESYNC_REQUIRED')
                if cursor_product != product:
                    raise StateRejected('PRODUCT_CHANGED_RESYNC_REQUIRED')
                after = int(version)
            page = self.dataset.page(product, after, limit)
            frames: list[str] = []
            def event(name: str, data: dict, cursor: str | None = None):
                if cursor is not None:
                    frames.append(f'id: {cursor}\n')
                frames.append('event: '+name+'\n')
                frames.append('data: '+json.dumps(data, separators=(',', ':'), allow_nan=False)+'\n\n')
            event('source', {'mode': 'ARCHIVE_REPLAY', 'product': product,
                             'source_manifest_sha256': self.dataset.certificate['manifest_sha256'],
                             'live_feed': False, 'trading_enabled': False,
                             'cursor_contract': 'manifest_sha256:product:state_version'})
            for row in page['rows']:
                # Native EventSource carries this id in Last-Event-ID on reconnect.
                # A restart on a different archive cannot reuse numeric positions.
                cursor = (self.dataset.certificate['manifest_sha256'] + ':' +
                          product + ':' + str(row['state_version']))
                event('state', row, cursor)
            event('replay_end', {'next_after': page['next_after'],
                                 'has_more': page['has_more'], 'mode': 'ARCHIVE_REPLAY',
                                 'live_feed': False})
            body = ''.join(frames).encode('utf8')
            if len(body) > 2_000_000:
                raise StateRejected('SSE_FRAME_BUDGET_EXCEEDED')
            self.server.count('sse_started')
            sse_response_started = True
            self.send_response(200)
            for key,value in [
                ('Content-Type', 'text/event-stream; charset=utf-8'),
                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                ('Content-Length', str(len(body))), ('X-Accel-Buffering', 'no'),
            ]:
                self.send_header(key,value)
            self.end_headers()
            self.wfile.write(body)
            self.server.count('sse_completed')
        except (StateRejected, ValueError, KeyError) as exc:
            if str(exc) in ('RESYNC_REQUIRED', 'STATE_VERSION_NOT_RETAINED',
                                'UNSCOPED_CURSOR_RESYNC_REQUIRED',
                                'SOURCE_CHANGED_RESYNC_REQUIRED',
                                'PRODUCT_CHANGED_RESYNC_REQUIRED'):
                self.server.count('resync_required')
            code = 409 if str(exc) in ('RESYNC_REQUIRED', 'STATE_VERSION_NOT_RETAINED',
                                     'UNSCOPED_CURSOR_RESYNC_REQUIRED',
                                     'SOURCE_CHANGED_RESYNC_REQUIRED',
                                     'PRODUCT_CHANGED_RESYNC_REQUIRED') else 400
            self.send_obj(code, {'error': str(exc), 'mode': 'ARCHIVE_REPLAY',
                                 'live_feed': False, 'trading_enabled': False})
        except (ConnectionError, TimeoutError, OSError):
            # Includes peer disconnects and timed-out slow readers.
            if sse_response_started:
                self.server.count('sse_aborted')
            return


def server_for(dataset: ReplayDataset, host: str = '127.0.0.1', port: int = 0) -> BoundedServer:
    if host not in ('127.0.0.1', '::1'):
        raise StateRejected('LOOPBACK_ONLY')
    class Handler(UnifiedHandler):
        pass
    Handler.dataset = dataset
    return BoundedServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('Invalid port')
    dataset = replay_capture(args.archive, args.manifest_sha256)
    server = server_for(dataset, port=args.port)
    print(f'ARCHIVE_REPLAY ONLY: http://127.0.0.1:{server.server_address[1]}/research', flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()