"""Verify strategy outputs on validated H1 packet Parquet, not vendor bars."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import duckdb
from h1_streaming_parquet import digest,feed_hash,PACKET
from f5_independent_clock import milliseconds
from independent_soak_replay import strict_json
from f6_liquidity_recovery import project
def need(ok,reason):
    if not ok:raise ValueError("H1_F6_"+reason)
def replay_dataset(root,pin,product,*,fixture=False,projector=project,receipt_pin=None):
    root=Path(root);raw=(root/"dataset.json").read_bytes()
    if receipt_pin is not None:need(hashlib.sha256(raw).hexdigest()==receipt_pin,"RECEIPT_PIN")
    r=strict_json(raw)
    need(r["schema"]=="h1_historical_dataset_v1" and r["source_manifest_sha256"]==pin and r["epoch"]==pin+":0"
         and r["independent_replay_parity"] is True and r["execution_authorized"] is False and r["alpha_confirmed"] is False,"CONTRACT")
    need((r["source_kind"]=="SYNTHETIC_TEST_ONLY" and r["research_eligible"] is False) if fixture else
         (r["source_kind"]=="ARCHIVE_REPLAY" and r["research_eligible"] is True),"MODE")
    f=root/"packets.parquet";expected=r["tables"]["packets"]
    need(not f.is_symlink() and digest(f)==expected["file_sha256"],"FILE_HASH")
    db=duckdb.connect(":memory:",config={"memory_limit":"256MB","threads":"1","preserve_insertion_order":"true"})
    h=hashlib.sha256();count=0;last_ms=None;last_mono=None
    def packets():
        nonlocal count,last_ms,last_mono
        cursor=db.execute("SELECT * FROM read_parquet(?)",[str(f)])
        while batch:=cursor.fetchmany(256):
            for values in batch:
                p=dict(zip(PACKET.names,values));feed_hash(h,p)
                need(p["source_manifest_sha256"]==pin and p["epoch"]==pin+":0" and p["connection_id"]==0
                     and p["sequence"]==count and milliseconds(p["receiver_utc"])==p["receiver_ms"]
                     and hashlib.sha256(p["payload"].encode()).hexdigest()==p["payload_sha256"],"PACKET")
                need((last_ms is None or p["receiver_ms"]>=last_ms) and (last_mono is None or p["receiver_monotonic_ns"]>=last_mono),"CLOCK_ORDER")
                last_ms,last_mono=p["receiver_ms"],p["receiver_monotonic_ns"]
                wire=strict_json(p["payload"])
                need(wire["sequence_num"]==count and wire["channel"]==p["channel"],"WIRE")
                count+=1
                yield p["sequence"],p["receiver_ms"],wire
    try:result=projector(packets(),product)
    finally:db.close()
    need(count==r["source_messages"]==expected["rows"] and h.hexdigest()==expected["canonical_rows_sha256"]
         and digest(f)==expected["file_sha256"],"ROW_PARITY")
    need((root/"dataset.json").read_bytes()==raw,"RECEIPT_CHANGED")
    return result
