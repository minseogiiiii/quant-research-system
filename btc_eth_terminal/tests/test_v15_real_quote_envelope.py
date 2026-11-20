"""Mandatory Coinbase original-source read-only replay, NOT a fill benchmark."""
import sys
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from taker_quote_envelope import Assumptions,evaluate

class RealProviderQuoteEnvelopeSmoke(unittest.TestCase):
    def test_v14_clean_real_archive_fixed_observation_intents(self):
        source=ROOT/'backend/data/live_v14_clean_120s/coinbase_btc_eth_soak_wsgXRy'
        intents=[
          {'id':'btc_l2_seq100','product_id':'BTC-USD','side':'BUY','quantity':'0.001','signal_sequence':100},
          {'id':'eth_l2_seq1200','product_id':'ETH-USD','side':'SELL','quantity':'0.02','signal_sequence':1200},
          {'id':'btc_l2_seq2500','product_id':'BTC-USD','side':'BUY','quantity':'0.005','signal_sequence':2500},
        ]
        # These are explicit hypothetical assumptions, *not* Coinbase fee
        # claims or empirically observed exchange routing latencies.
        assumed=Assumptions(250,5000,2500,'10','5','0.25')
        out=evaluate(source,intents,assumed)
        self.assertEqual(out['source_messages_audited'],5177)
        self.assertEqual(out['source_manifest_sha256'],
                         '8f2e0ec88a601a92ab4c7bcd0e04b20536fbe46bbd8b3c7f67ae3c1d075c28e5')
        expected=[('btc_l2_seq100',116,'67198.472445'),
                  ('eth_l2_seq1200',1207,'1895.061995'),
                  ('btc_l2_seq2500',2509,'67231.418910')]
        self.assertEqual([(x['id'],x.get('quote_sequence'),x.get('hypothetical_quoted_price'))
                          for x in out['results']],expected)
        self.assertTrue(all(x['status']=='INDICATIVE_TAKER_QUOTE_ONLY' for x in out['results']))
        self.assertFalse(out['fills_verified'])
        self.assertFalse(out['strategy_alpha_verified'])
        self.assertFalse(out['trading_or_api_writes_enabled'])
        self.assertFalse(out['historic_venue_fee_tier_verified'])

if __name__=='__main__':unittest.main()
