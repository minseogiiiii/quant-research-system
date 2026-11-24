import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from h4_cross_session_catalog import catalog
from h1_streaming_parquet import digest,PACKET,LEVEL,TRADE
def fixture(root,source,start,role):
    root.mkdir();epoch=source+":0"
    rows=[dict(source_manifest_sha256=source,epoch=epoch,venue="COINBASE_ADVANCED",
        connection_id=0,sequence=i,receiver_ms=start+i*1000,receiver_utc="fixture",
        receiver_monotonic_ns=i,channel="l2_data",chunk="fixture",payload_sha256="a"*64,payload="fixture") for i in range(2)]
    tables={}
    for name,schema,data in (("packets",PACKET,rows),("l2",LEVEL,[]),("trades",TRADE,[])):
        path=root/(name+".parquet");pq.write_table(pa.Table.from_pylist(data,schema=schema),path)
        tables[name]={"file_sha256":digest(path),"rows":len(data),"canonical_rows_sha256":"a"*64}
    r=dict(schema="h1_historical_dataset_v1",source_manifest_sha256=source,epoch=epoch,source_kind="SYNTHETIC_TEST_ONLY",
        source_messages=2,tables=tables,independent_replay_parity=True,research_eligible=False,
        live_feed=False,oos_approved=False,alpha_confirmed=False,execution_authorized=False)
    raw=json.dumps(r).encode();(root/"dataset.json").write_bytes(raw)
    return dict(dataset_path=str(root),dataset_receipt_sha256=hashlib.sha256(raw).hexdigest(),role=role)
class Catalog(unittest.TestCase):
    def test_chronological_roles_and_determinism(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);a=fixture(p/"a","a"*64,10000,"DEVELOPMENT");b=fixture(p/"b","b"*64,20000,"DECLARED_HOLDOUT")
            r=catalog([b,a],1000,fixture=True)
            self.assertEqual(r,catalog([a,b],1000,fixture=True))
            self.assertEqual(r["session_count"],2)
            self.assertFalse(r["oos_approved"])
            self.assertFalse(r["holdout_access_history_verified"])
            with self.assertRaises(ValueError):catalog([a,b],1000)
    def test_overlaps_duplicate_role_embargo_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);a=fixture(p/"a","a"*64,10000,"DEVELOPMENT");b=fixture(p/"b","b"*64,10500,"VALIDATION")
            c=fixture(p/"c","c"*64,12000,"VALIDATION")
            for rows,embargo in (([a,b],0),([a,a],0),([a,c],1000),
                ([dict(a,role="DECLARED_HOLDOUT"),c],0)):
                with self.assertRaises(ValueError):catalog(rows,embargo,fixture=True)
            path=p/"a"/"packets.parquet";path.write_bytes(path.read_bytes()+b"tamper")
            with self.assertRaises(ValueError):catalog([a],0,fixture=True)
