"""Regression tests for v5: source tamper, endpoint protection, retry and audits."""
from __future__ import annotations

import csv
import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import audit_features
import benchmark_existing
import bridge
import fetch_btc_eth
import fetch_coinbase_5m as coinbase
from test_btc_eth_pair import START, END, exchange_fixture


class TestArchivePerRequest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.root
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({
            'scope': 'BTC_ETH_SPOT_5M_RESEARCH_ONLY',
            'datasets': [], 'research': [], 'experiments': []
        }))
        self.folder = fetch_btc_eth.export_pair(
            START * 1000, END * 1000, data_root=self.root,
            manifest_path=self.manifest, fetch=exchange_fixture, register=True)
        self.doc = bridge.read_manifest(self.manifest)

    def tearDown(self):
        bridge.DATA_ROOT = self.old_root
        self.temp.cleanup()

    def test_unchanged_archive_allows_read_only_history(self):
        data = bridge.get_bars(self.doc, 'BTC_COINBASE_SPOT', '5', END * 1000, 0, 3)
        self.assertEqual(len(data['bars']), 3)

    def test_tampered_source_blocks_bars_without_catalog_refresh(self):
        self.assertEqual(len(bridge.get_bars(self.doc, 'BTC_COINBASE_SPOT', '5', END * 1000, 0, 5)['bars']), 3)
        raw = self.folder / 'raw' / 'eth_coinbase_spot_5m' / '0000.json'
        raw.write_bytes(raw.read_bytes() + b' ')
        with self.assertRaisesRegex(coinbase.SourceError, 'SHA-256'):
            bridge.get_bars(self.doc, 'BTC_COINBASE_SPOT', '5', END * 1000, 0, 5)
        with self.assertRaisesRegex(coinbase.SourceError, 'SHA-256'):
            bridge.get_signals(self.doc, 'BTC_COINBASE_SPOT')
        with self.assertRaisesRegex(coinbase.SourceError, 'SHA-256'):
            bridge.get_fills(self.doc, 'BTC_COINBASE_SPOT')

    def test_registration_rejects_cross_product_csv(self):
        self.doc['datasets'][0]['file'] = self.doc['datasets'][1]['file']
        with self.assertRaisesRegex(bridge.InputError, 'Dataset file'):
            bridge.get_bars(self.doc, 'BTC_COINBASE_SPOT', '5', END * 1000, 0, 5)

    def test_registration_rejects_cross_product_source_sidecar(self):
        self.doc['datasets'][0]['provenance_file'] = self.doc['datasets'][1]['provenance_file']
        with self.assertRaisesRegex(bridge.InputError, 'Provenance sidecar'):
            bridge.get_bars(self.doc, 'BTC_COINBASE_SPOT', '5', END * 1000, 0, 5)

    def test_empty_benchmark_reports_no_false_bottleneck(self):
        old = bridge.MANIFEST
        empty = self.root / 'empty.json'
        empty.write_text(json.dumps({'datasets': []}))
        bridge.MANIFEST = empty
        try:
            result = benchmark_existing.benchmark(2)
        finally:
            bridge.MANIFEST = old
        self.assertEqual(result['status'], 'NO_REGISTERED_MARKET_DATA')
        self.assertFalse(result['performance_bottleneck_demonstrated'])


class TestCoinbaseRetry(unittest.TestCase):
    @staticmethod
    def ok_response():
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'{"candles":[]}'
        return FakeResponse()

    def test_429_then_success_without_rewriting_source_bytes(self):
        fail = HTTPError('https://api.coinbase.com/', 429, 'rate limit', {'Retry-After': '0'}, None)
        with patch.object(coinbase, 'urlopen', side_effect=[fail, self.ok_response()]) as opened, \
             patch.object(coinbase.time, 'sleep') as slept:
            raw, doc = coinbase.request_json('BTC-USD', START, END)
        self.assertEqual(raw, b'{"candles":[]}')
        self.assertEqual(doc, {'candles': []})
        self.assertEqual(opened.call_count, 2)
        slept.assert_called_once_with(0.0)

    def test_http_404_is_not_retried(self):
        fail = HTTPError('https://api.coinbase.com/', 404, 'not found', {}, None)
        with patch.object(coinbase, 'urlopen', side_effect=fail) as opened:
            with self.assertRaisesRegex(coinbase.SourceError, '404'):
                coinbase.request_json('BTC-USD', START, END)
        self.assertEqual(opened.call_count, 1)

    def test_retry_is_bounded_after_three_transient_failures(self):
        with patch.object(coinbase, 'urlopen', side_effect=URLError('offline')) as opened, \
             patch.object(coinbase.time, 'sleep') as slept:
            with self.assertRaisesRegex(coinbase.SourceError, 'bounded retries'):
                coinbase.request_json('ETH-USD', START, END)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(slept.call_count, 2)

    def test_invalid_json_is_not_retried(self):
        class Malformed:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'invalid json'
        with patch.object(coinbase, 'urlopen', return_value=Malformed()) as opened:
            with self.assertRaisesRegex(coinbase.SourceError, 'not JSON'):
                coinbase.request_json('BTC-USD', START, END)
        self.assertEqual(opened.call_count, 1)


class TestStreamingAudit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.t0 = START * 1000

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, rows, *, gzip_out=False):
        p = self.root / name
        opener = gzip.open if gzip_out else open
        with opener(p, 'wt', encoding='utf-8', newline='') as fp:
            writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return p

    def row(self, time, **fields):
        return {'time': str(time), 'open': '100', 'close': '101', **fields}

    def test_csv_gz_audit_reports_real_gaps_no_imputation(self):
        data = [self.row(self.t0), self.row(self.t0 + 600_000)]
        source = self.write('btc_features_5m.csv.gz', data, gzip_out=True)
        report = audit_features.audit(source, time_column='time', venue='Binance', instrument='BTCUSDT', feature_input=True)
        self.assertEqual(report['rows'], 2)
        self.assertEqual(report['missing_5m_bar_intervals'], 1)
        self.assertFalse(report['complete_regular_grid'])
        self.assertFalse(report['point_in_time_source_truth_verified'])
        self.assertEqual(len(report['input_sha256']), 64)

    def test_future_labels_are_not_model_features(self):
        source = self.write('btc_features_5m.csv', [self.row(self.t0, future_return_30m='0.02')])
        report = audit_features.audit(source, time_column='time')
        self.assertEqual(report['suspicious_forward_label_columns'], ['future_return_30m'])
        with self.assertRaisesRegex(bridge.InputError, 'Potential future'):
            audit_features.audit(source, time_column='time', feature_input=True)
        labels = self.write('btc_future_labels.csv', [self.row(self.t0)])
        with self.assertRaisesRegex(bridge.InputError, 'Future-label'):
            audit_features.audit(labels, time_column='time', feature_input=True)

    def test_open_time_requires_completed_bar_availability(self):
        bad = self.write('eth_5m.csv', [self.row(self.t0, available_at=str(self.t0 + 299_999))])
        with self.assertRaisesRegex(bridge.InputError, 'before close'):
            audit_features.audit(bad, time_column='time', available_column='available_at')
        okay = self.write('eth_good.csv', [self.row(self.t0, available_at=str(self.t0 + 300_000))])
        result = audit_features.audit(okay, time_column='time', available_column='available_at')
        self.assertTrue(result['availability_value_checks_passed'])
        self.assertFalse(result['point_in_time_source_truth_verified'])

    def test_duplicate_time_and_unaligned_candle_rejected(self):
        dup = self.write('btc_dup.csv', [self.row(self.t0), self.row(self.t0)])
        with self.assertRaisesRegex(bridge.InputError, 'Non-increasing'):
            audit_features.audit(dup, time_column='time')
        bad = self.write('btc_bad.csv', [self.row(self.t0 + 1)])
        with self.assertRaisesRegex(bridge.InputError, '5-minute UTC'):
            audit_features.audit(bad, time_column='time')

    def test_does_not_guess_timestamp_mapping_or_source_identity(self):
        source = self.write('eth_features.csv', [self.row(self.t0)])
        report = audit_features.audit(source)
        self.assertFalse(report['time_sequence_validated'])
        self.assertIsNone(report['venue_declared_not_verified'])
        self.assertIsNone(report['missing_5m_bar_intervals'])
        with self.assertRaisesRegex(bridge.InputError, 'Explicit time column'):
            audit_features.audit(source, time_column='event_time')


if __name__ == '__main__':
    unittest.main()

class TestFeatureBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.root
        import export_ohlcv
        self.old_export_root = export_ohlcv.DATA_ROOT
        export_ohlcv.DATA_ROOT = self.root

    def tearDown(self):
        bridge.DATA_ROOT = self.old_root
        import export_ohlcv
        export_ohlcv.DATA_ROOT = self.old_export_root
        self.temp.cleanup()

    def test_chart_csv_cannot_contain_forward_label_column(self):
        file = self.root / 'btc_bad.csv'
        file.write_text(
            'time,open,high,low,close,future_return_30m\n'
            f'{START * 1000},100,101,99,100,0.12\n'
        )
        with self.assertRaisesRegex(bridge.InputError, 'forward/target/label'):
            bridge.parse_bars(file.name)

    def test_explicit_ohlcv_mapping_cannot_select_future_target(self):
        import export_ohlcv
        src = self.root / 'features.csv'
        src.write_text(
            'time,open,high,low,close,future_price\n'
            f'{START * 1000},100,101,99,100,110\n'
        )
        columns = {'time': 'time', 'open': 'open', 'high': 'high',
                   'low': 'low', 'close': 'future_price'}
        with self.assertRaisesRegex(bridge.InputError, 'cannot use future'):
            export_ohlcv.convert(src=src, dst=self.root / 'out.csv', columns=columns)
        self.assertFalse((self.root / 'out.csv').exists())

class TestRawArchiveBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.root
        self.folder = fetch_btc_eth.export_pair(
            START * 1000, END * 1000, data_root=self.root,
            fetch=exchange_fixture, register=False)

    def tearDown(self):
        bridge.DATA_ROOT = self.old_root
        self.temp.cleanup()

    def test_archive_metadata_requires_explicit_utc(self):
        src = self.folder / 'btc_coinbase_spot_5m.source.json'
        meta = json.loads(src.read_text())
        meta['start_inclusive_utc'] = meta['start_inclusive_utc'].replace('+00:00', '')
        src.write_text(json.dumps(meta))
        with self.assertRaisesRegex(coinbase.SourceError, 'explicitly use UTC'):
            fetch_btc_eth.verify_bundle(self.folder)

    def test_symlinked_raw_page_outside_archive_is_rejected(self):
        raw = self.folder / 'raw' / 'btc_coinbase_spot_5m' / '0000.json'
        outside = self.root / 'outside.json'
        outside.write_bytes(raw.read_bytes())
        raw.unlink()
        raw.symlink_to(outside)
        with self.assertRaisesRegex(coinbase.SourceError, 'bounded in-archive'):
            fetch_btc_eth.verify_bundle(self.folder)

    def test_malicious_oversize_mock_page_rejected_before_write(self):
        def oversize(product, a, b):
            return (b' ' * (2_000_001)), {'candles': []}
        before = set(self.root.iterdir())
        with self.assertRaisesRegex(coinbase.SourceError, 'safe per-page size'):
            fetch_btc_eth.export_pair(
                (START - 300) * 1000, (END - 300) * 1000,
                data_root=self.root, fetch=oversize, register=False)
        self.assertEqual(set(self.root.iterdir()), before)

class TestMultiPageAndHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_root = bridge.DATA_ROOT
        bridge.DATA_ROOT = self.root
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({
            'scope': 'BTC_ETH_SPOT_5M_RESEARCH_ONLY',
            'datasets': [], 'research': [], 'experiments': []
        }))

    def tearDown(self):
        bridge.DATA_ROOT = self.old_root
        self.temp.cleanup()

    def test_301_bars_split_into_two_pages_per_instrument(self):
        end = START + 301 * 300
        result = fetch_btc_eth.export_pair(
            START * 1000, end * 1000, data_root=self.root,
            manifest_path=self.manifest, fetch=exchange_fixture, register=True)
        audit = fetch_btc_eth.verify_bundle(result)
        self.assertEqual(audit['markets']['BTC-USD']['rows'], 301)
        self.assertEqual(audit['markets']['ETH-USD']['rows'], 301)
        for base in ('btc', 'eth'):
            sidecar = json.loads((result / f'{base}_coinbase_spot_5m.source.json').read_text())
            self.assertEqual(len(sidecar['pages']), 2)
            self.assertEqual([p['count'] for p in sidecar['pages']], [300, 1])
        bars = bridge.get_bars(bridge.read_manifest(self.manifest), 'ETH_COINBASE_SPOT',
                               '5', end * 1000, 0, 350)['bars']
        self.assertEqual(len(bars), 301)
        self.assertLess(bars[0]['time'], bars[-1]['time'])

    def test_http_bars_refuse_archive_tampering_after_catalog(self):
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from urllib.request import urlopen
        from urllib.error import HTTPError
        folder = fetch_btc_eth.export_pair(
            START * 1000, END * 1000, data_root=self.root,
            manifest_path=self.manifest, fetch=exchange_fixture, register=True)
        old_manifest = bridge.MANIFEST
        bridge.MANIFEST = self.manifest
        server = ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        th = Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            addr = f'http://127.0.0.1:{server.server_address[1]}'
            with urlopen(addr + '/api/catalog') as response:
                self.assertEqual(len(json.load(response)['datasets']), 2)
            url = addr + f'/api/bars?symbol=BTC_COINBASE_SPOT&resolution=5&to={END * 1000}&countBack=2'
            with urlopen(url) as response:
                self.assertEqual(len(json.load(response)['bars']), 2)
            raw = folder / 'raw' / 'eth_coinbase_spot_5m' / '0000.json'
            raw.write_bytes(raw.read_bytes() + b'\n')
            with self.assertRaises(HTTPError) as error:
                urlopen(url)
            self.assertEqual(error.exception.code, 400)
            self.assertIn('SHA-256', json.loads(error.exception.read())['error'])
        finally:
            server.shutdown()
            server.server_close()
            th.join()
            bridge.MANIFEST = old_manifest
