import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import io
import zipfile
import numpy as np
import pandas as pd
import pytest
from history import load_zip, verify_zip
from ablation import GROUPS,build_features,run_ablation
from live_book import Book,SequenceGap,ofi_change,features
from collections import deque


def fake_data(days=14,seed=9001):
    rng=np.random.default_rng(seed)
    n=days*1440
    ts=pd.date_range('2024-01-01',periods=n,freq='min',tz='UTC')
    ret=rng.normal(0,0.00035,n)
    close=100*np.exp(np.cumsum(ret))
    openp=np.r_[100,close[:-1]]
    high=np.maximum(openp,close)*(1+rng.uniform(0,0.0003,n))
    low=np.minimum(openp,close)*(1-rng.uniform(0,0.0003,n))
    quote_vol=np.exp(rng.normal(12,0.4,n))
    buy_ratio=0.5+0.25*np.tanh(ret*500)
    return pd.DataFrame({'ts':ts,'open':openp,'high':high,'low':low,'close':close,
                         'volume':quote_vol/close,'quote_volume':quote_vol,
                         'taker_buy_quote':quote_vol*buy_ratio})


def test_archive_ms_us(tmp_path):
    for stamp in [1704067200000,1735689600000000]:
        data=[str(stamp),"100","101","99","100","1",str(stamp+60_000),"100","3","0.5","50","0"]
        p=tmp_path/f'{stamp}.zip'
        with zipfile.ZipFile(p,'w') as z:
            z.writestr('data.csv',','.join(data)+'\n')
        df=load_zip(p)
        assert df.iloc[0]['ts'].year in (2024,2025)
    with pytest.raises(ValueError,match='SHA256'):
        verify_zip(b'bad','0'*64+'  file.zip','file.zip')


def test_features_are_point_in_time():
    raw=fake_data(2)
    a=build_features(raw)
    modified=raw.copy()
    cutoff=1000
    modified.loc[cutoff+1:,'close']*=4.0
    modified.loc[cutoff+1:,'open']*=4.0
    modified.loc[cutoff+1:,'high']*=4.0
    modified.loc[cutoff+1:,'low']*=4.0
    b=build_features(modified)
    earlier=pd.Timestamp('2024-01-01',tz='UTC')+pd.Timedelta(minutes=cutoff)
    common=pd.merge(a[a.decision_time_utc<=earlier],b[b.decision_time_utc<=earlier],on='decision_time_utc',suffixes=('_a','_b'))
    assert len(common)>100
    for cols in GROUPS.values():
        for f in cols:
            assert np.allclose(common[f+'_a'],common[f+'_b'],equal_nan=True),f


def test_synthetic_ablation_smoke():
    # These data are NOT real market data and must not be used to claim edge.
    f=build_features(fake_data(14))
    r=run_ablation(f,'2024-01-05','2024-01-09','2024-01-14',20)
    assert r['rows']['train']>100 and r['rows']['holdout']>100
    assert len(r['models']) == 10
    assert 'FULL_G1_G4' in r['models']
    assert all(r['models'][f'DROP_{g}']['full_minus_drop_delta_mse'] is not None for g in GROUPS)
    assert 0 <= r['models']['FULL_G1_G4']['holdout']['coverage'] <= 1


def test_book_reconstruct_and_resync():
    b=Book()
    b.snapshot({'lastUpdateId':100,'bids':[['100','10'],['99','15']], 'asks':[['101','5']]})
    assert not b.synced
    assert not b.apply({'U':90,'u':99,'b':[],'a':[]})
    before=b.top()
    assert b.apply({'U':99,'u':105,'b':[['100','12']], 'a':[['101','4']]})
    assert b.synced and b.last_id==105
    assert ofi_change(before,b.top())==3.0
    assert b.apply({'U':106,'u':108,'b':[['99','0']], 'a':[]})
    assert 99.0 not in b.bids
    with pytest.raises(SequenceGap):
        b.apply({'U':110,'u':112,'b':[], 'a':[]})


def test_book_features():
    b=Book()
    b.snapshot({'lastUpdateId':1,'bids':[['100','8']], 'asks':[['100.1','2']]})
    b.apply({'U':2,'u':2,'b':[],'a':[]})
    f=features(b,deque([(0.,2.0)]),deque([(0.,100.)]))
    assert abs(f['obi_top']-0.6)<1e-9
    assert f['spread_bps']>0
    assert f['microprice_gap_bps']>0