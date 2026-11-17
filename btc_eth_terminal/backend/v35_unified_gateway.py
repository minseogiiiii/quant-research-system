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
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from bridge import Handler as HistoricalHandler
from v35_replay_service import replay_capture, ReplayDataset, StateRejected, PRODUCTS

MAX_SSE_EVENTS = 60

class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    max_clients = 8

    def __init__(self, address, handler):
        self.slots = BoundedSemaphore(self.max_clients)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
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
        try:
            if self.dataset is None:
                self.send_obj(503, {'error': 'NO_VERIFIED_ARCHIVE', 'live_feed': False})
                return
            route = parsed.path
            permitted = {
                '/api/v35/health': set(),
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
            last_id = self.headers.get('Last-Event-ID')
            if last_id:
                if 'after' in args or re.fullmatch(r'[1-9]\d{0,12}', last_id) is None:
                    raise StateRejected('INVALID_SSE_RESUME_CURSOR')
                after = int(last_id)
            page = self.dataset.page(product, after, limit)
            frames: list[str] = []
            def event(name: str, data: dict, cursor: int | None = None):
                if cursor is not None:
                    frames.append(f'id: {cursor}\n')
                frames.append('event: '+name+'\n')
                frames.append('data: '+json.dumps(data, separators=(',', ':'), allow_nan=False)+'\n\n')
            event('source', {'mode': 'ARCHIVE_REPLAY', 'product': product,
                             'source_manifest_sha256': self.dataset.certificate['manifest_sha256'],
                             'live_feed': False, 'trading_enabled': False})
            for row in page['rows']:
                event('state', row, row['state_version'])
            event('replay_end', {'next_after': page['next_after'],
                                 'has_more': page['has_more'], 'mode': 'ARCHIVE_REPLAY',
                                 'live_feed': False})
            body = ''.join(frames).encode('utf8')
            if len(body) > 2_000_000:
                raise StateRejected('SSE_FRAME_BUDGET_EXCEEDED')
            self.send_response(200)
            for key,value in [
                ('Content-Type', 'text/event-stream; charset=utf-8'),
                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                ('Content-Length', str(len(body))), ('X-Accel-Buffering', 'no'),
            ]:
                self.send_header(key,value)
            self.end_headers()
            self.wfile.write(body)
        except (StateRejected, ValueError, KeyError) as exc:
            code = 409 if str(exc) in ('RESYNC_REQUIRED', 'STATE_VERSION_NOT_RETAINED') else 400
            self.send_obj(code, {'error': str(exc), 'mode': 'ARCHIVE_REPLAY',
                                 'live_feed': False, 'trading_enabled': False})
        except (ConnectionError, BrokenPipeError):
            # Disconnecting clients cannot modify the source or state.
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