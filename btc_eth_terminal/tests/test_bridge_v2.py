"""Evidence and temporal-integrity tests added to the original 12 regression gates."""
import csv
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import bridge
import export_ohlcv

BASE = {
    'symbol': 'BTCUSDT', 'name': 'BTC/USDT research', 'exchange': 'RESEARCH',
    'timezone': 'UTC', 'session': '24x7', 'type': 'crypto', 'resolution': '5',
    'file': 'btc.csv', 'source_type': 'synthetic', 'evidence_status': 'unverified',
    'pricePrecision': 2, 'tickSize': 0.01,
}
T0, T1, T2 = (1793629800000, 1793630100000, 1793630400000)
A0, A1, A2 = (1793630100000, 1793630400000, 1793630700000)
BAR_CSV = f'''time,open,high,low,close,volume,available_at
{T0},100,101,99,100.5,10,{A0}
{T1},100.5,102,100,101.5,20,{A1}
{T2},101.5,102,99,100,30,{A2}
'''
SIGNAL_CSV = f'''signal_id,time,available_at,feature_max_observed_at,side,model_id
s1,{A0},{A0},{T0},LONG,baseline
s2,{A1},{A1},{T1},WAIT,baseline
'''
FILL_CSV = f'''time,price,side,quantity,event_type,available_at
{T1},101,buy,1,paper_fill,{A1}
'''


class EvidenceGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.old_root, self.old_manifest, self.old_export_root = bridge.DATA_ROOT, bridge.MANIFEST, export_ohlcv.DATA_ROOT
        bridge.DATA_ROOT = self.data
        export_ohlcv.DATA_ROOT = self.data
        self.manifest = self.root / 'manifest.json'
        bridge.MANIFEST = self.manifest
        self.write('btc.csv', BAR_CSV)
        self.write('signals.csv', SIGNAL_CSV)
        self.write('fills.csv', FILL_CSV)
        self.ds = {**BASE, 'signals_file': 'signals.csv', 'fills_file': 'fills.csv'}
        self.set_manifest([self.ds])

    def tearDown(self):
        bridge.DATA_ROOT, bridge.MANIFEST, export_ohlcv.DATA_ROOT = self.old_root, self.old_manifest, self.old_export_root
        self.tmp.cleanup()

    def write(self, name, text):
        (self.data / name).write_text(text, encoding='utf-8')

    def set_manifest(self, datasets):
        self.manifest.write_text(json.dumps({'datasets': datasets, 'research': []}), encoding='utf-8')

    def doc(self): return bridge.read_manifest(self.manifest)

    def test_asof_cutoff_respects_availability_not_bar_start(self):
        d = self.doc()
        before = bridge.get_bars(d, 'BTCUSDT', '5', A2, 0, 30, asof=T1 + 1000)
        self.assertEqual([b['time'] for b in before['bars']], [T0])
        self.assertTrue(before['noMoreData'])
        self.assertEqual(len(bridge.get_bars(d, 'BTCUSDT', '5', A2, 0, 30, asof=A2)['bars']), 3)
        self.assertNotIn('available_at', before['bars'][0])

    def test_asof_rejects_non_available_at(self):
        self.write('btc.csv', BAR_CSV.replace(',available_at', '').replace(f',{A0}', '').replace(f',{A1}', '').replace(f',{A2}', ''))
        with self.assertRaisesRegex(bridge.InputError, 'available_at'):
            bridge.get_bars(self.doc(), 'BTCUSDT', '5', A2, 0, 10, asof=T1)

    def test_signal_future_observation_is_rejected(self):
        self.write('signals.csv', SIGNAL_CSV.replace(f's1,{A0},{A0},{T0}', f's1,{A0},{A0},{A1}'))
        with self.assertRaisesRegex(bridge.InputError, 'point-in-time'):
            bridge.parse_signals('signals.csv')

    def test_signal_arrival_after_decision_is_rejected(self):
        self.write('signals.csv', SIGNAL_CSV.replace(f's2,{A1},{A1}', f's2,{A1},{A2}'))
        with self.assertRaisesRegex(bridge.InputError, 'point-in-time'):
            bridge.catalog(self.doc())

    def test_repeated_signal_id_rejected(self):
        self.write('signals.csv', SIGNAL_CSV.replace('s2,', 's1,'))
        with self.assertRaisesRegex(bridge.InputError, 'duplicate'):
            bridge.parse_signals('signals.csv')

    def test_asof_signals_filtered_and_never_equal_fills(self):
        d = self.doc()
        self.assertEqual([s['signal_id'] for s in bridge.get_signals(d, 'BTCUSDT', asof=A0)], ['s1'])
        self.assertEqual(bridge.get_fills(d, 'BTCUSDT', asof=A0), [])
        self.assertEqual([s['record_type'] for s in bridge.get_signals(d, 'BTCUSDT')], ['CANDIDATE_SIGNAL'] * 2)
        self.assertEqual(len(bridge.get_fills(d, 'BTCUSDT', asof=A2)), 1)

    def test_fill_asof_without_available_at_fails(self):
        self.write('fills.csv', FILL_CSV.replace(',available_at', '').replace(f',{A1}', ''))
        with self.assertRaisesRegex(bridge.InputError, 'available_at'):
            bridge.get_fills(self.doc(), 'BTCUSDT', asof=A2)
        self.assertEqual(len(bridge.get_fills(self.doc(), 'BTCUSDT')), 1)

    def test_mutating_only_future_bars_preserves_past_information_set(self):
        snapshot = bridge.get_bars(self.doc(), 'BTCUSDT', '5', A2, 0, 10, asof=T1 + 1000)
        self.write('btc.csv', BAR_CSV.replace('101.5,102,99,100,30', '101.5,9999,0.01,999,30'))
        mutated = bridge.get_bars(self.doc(), 'BTCUSDT', '5', A2, 0, 10, asof=T1 + 1000)
        self.assertEqual(snapshot['bars'], mutated['bars'])
        self.assertNotEqual(snapshot['csv_sha256'], mutated['csv_sha256'])

    def test_csv_sha_is_hash_of_parsed_exact_bytes(self):
        b, digest = bridge.parse_bars('btc.csv')
        self.assertEqual(digest, hashlib.sha256((self.data / 'btc.csv').read_bytes()).hexdigest())
        self.assertEqual(len(b), 3)

    def test_malformed_extra_and_duplicate_headers_rejected(self):
        self.write('btc.csv', BAR_CSV.replace('time,open,high', 'time,open,open'))
        with self.assertRaisesRegex(bridge.InputError, 'header'):
            bridge.parse_bars('btc.csv')
        self.write('btc.csv', BAR_CSV + f'{T2+300000},101,102,100,101,10,{A2+300000},extra\n')
        with self.assertRaisesRegex(bridge.InputError, 'extra/missing'):
            bridge.parse_bars('btc.csv')

    def test_manifest_duplicate_resolution_rejected(self):
        self.set_manifest([self.ds, self.ds])
        with self.assertRaisesRegex(bridge.InputError, 'Duplicate'):
            self.doc()

    def test_multi_resolution_same_symbol_support(self):
        alternate = {**self.ds, 'resolution': '15', 'file': 'btc_15m.csv'}
        self.write('btc_15m.csv', BAR_CSV)
        self.set_manifest([self.ds, alternate])
        c = bridge.catalog(self.doc())
        self.assertEqual(c['datasets'][0]['supportedResolutions'], ['5', '15'])
        self.assertEqual(c['datasets'][0]['csv_sha256'], c['datasets'][1]['csv_sha256'])

    def test_symbol_metadata_conflict_rejected(self):
        alternate = {**self.ds, 'resolution': '15', 'exchange': 'FAKE'}
        self.set_manifest([self.ds, alternate])
        with self.assertRaisesRegex(bridge.InputError, 'identical'):
            self.doc()

    def test_boolean_ticksize_and_fake_source_hash_rejected(self):
        self.set_manifest([{**self.ds, 'tickSize': True}])
        with self.assertRaisesRegex(bridge.InputError, 'tickSize'):
            self.doc()
        self.set_manifest([{**self.ds, 'source_type': 'real', 'evidence_status': 'source_audited', 'original_raw_sha256': 'not-a-digest'}])
        with self.assertRaisesRegex(bridge.InputError, 'original_raw_sha256'):
            self.doc()

    def test_future_labels_filename_cannot_be_datafeed(self):
        self.write('BTC_future_labels.csv', BAR_CSV)
        self.set_manifest([{**self.ds, 'file': 'BTC_future_labels.csv'}])
        with self.assertRaisesRegex(bridge.InputError, 'Future-label'):
            self.doc()

    def test_symlink_escape_rejected(self):
        (self.data / 'bad.csv').symlink_to(self.manifest)
        with self.assertRaises(bridge.InputError):
            bridge.safe_path('bad.csv')

    def test_1990s_2000s_epoch_millis_supported_for_frozen_spy(self):
        self.assertEqual(bridge._time_ms('2000-01-03T21:00:00Z'), 946933200000)
        self.assertEqual(bridge._time_ms('946933200000'), 946933200000)
        with self.assertRaisesRegex(bridge.InputError, 'seconds forbidden'):
            bridge._time_ms('9469332000')

    def test_atomic_export_of_gz_data_with_availability(self):
        src = self.root / 'btc_features_5m.csv.gz'
        with gzip.open(src, 'wt', encoding='utf-8') as fp:
            fp.write('t,o,h,l,c,v,observed\n')
            fp.write(f'{T0},100,101,99,100.5,10,{A0}\n')
            fp.write(f'{T1},100.5,102,100,101.5,20,{A1}\n')
        dst = self.data / 'exported.csv'
        cols = dict(time='t', open='o', high='h', low='l', close='c', volume='v', available_at='observed')
        meta = export_ohlcv.convert(src=src, dst=dst, columns=cols)
        self.assertEqual(meta['rows'], 2)
        self.assertTrue(meta['asof_supported'])
        self.assertEqual(len(bridge.parse_bars('exported.csv')[0]), 2)
        self.assertEqual(list(self.data.glob('.quant-export-*')), [])
        with self.assertRaisesRegex(bridge.InputError, 'already exists'):
            export_ohlcv.convert(src=src, dst=dst, columns=cols)

    def test_failed_export_leaves_existing_file_unchanged(self):
        src = self.root / 'invalid.csv'
        src.write_text('t,o,h,l,c\n' + f'{T0},100,90,99,105\n')
        dst = self.data / 'exported.csv'
        dst.write_bytes(b'ORIGINAL\n')
        with self.assertRaisesRegex(bridge.InputError, 'Invalid'):
            export_ohlcv.convert(src=src, dst=dst, columns={'time':'t','open':'o','high':'h','low':'l','close':'c'}, overwrite=True)
        self.assertEqual(dst.read_bytes(), b'ORIGINAL\n')

    def test_http_asof_signals_and_files_hashes(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        addr = f'http://127.0.0.1:{server.server_address[1]}'
        def api(path):
            with urlopen(addr + path) as response:
                return json.load(response)
        try:
            catalog = api('/api/catalog')
            ds = catalog['datasets'][0]
            self.assertTrue(ds['supports_asof'])
            self.assertEqual(len(ds['csv_sha256']), 64)
            self.assertEqual(len(ds['signals_sha256']), 64)
            sig = api(f'/api/signals?symbol=BTCUSDT&asof={A0}')
            self.assertEqual(len(sig['signals']), 1)
            self.assertEqual(sig['signals_sha256'], ds['signals_sha256'])
            fills = api(f'/api/fills?symbol=BTCUSDT&asof={A0}')
            self.assertEqual(fills['fills'], [])
            self.assertEqual(fills['fills_sha256'], ds['fills_sha256'])
            with self.assertRaises(HTTPError) as cm:
                api('/api/bars?symbol=BTCUSDT&resolution=5&to=abc&countBack=2')
            self.assertEqual(cm.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
