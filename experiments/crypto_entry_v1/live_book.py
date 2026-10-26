"""Binance public L2 + trade capture; integrity-first, research-only, never places orders.

Depth updates are absolute quantities, not deltas; U/u sequence validation
and WS-before-snapshot buffering are mandatory. All raw records persisted.
"""
from __future__ import annotations
import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import time
import aiohttp

ENDPOINTS={
  "global": ("wss://data-stream.binance.vision/stream?streams=", "https://data-api.binance.vision/api/v3/depth"),
  "us": ("wss://stream.binance.us:9443/stream?streams=", "https://api.binance.us/api/v3/depth"),
}

class SequenceGap(Exception):
    pass

class Book:
    def __init__(self):
        self.bids:dict[float,float]={}
        self.asks:dict[float,float]={}
        self.last_id=-1
        self.synced=False

    def snapshot(self, obj:dict)->None:
        self.bids={float(p):float(q) for p,q in obj["bids"] if float(q)>0}
        self.asks={float(p):float(q) for p,q in obj["asks"] if float(q)>0}
        self.last_id=int(obj["lastUpdateId"])
        self.synced=False
        self.validate()

    def validate(self):
        if not self.bids or not self.asks:
            raise ValueError("Empty side of book")
        if max(self.bids) >= min(self.asks):
            raise ValueError("Crossed or locked book; invalid for our features")

    def top(self):
        self.validate()
        return max(self.bids),self.bids[max(self.bids)],min(self.asks),self.asks[min(self.asks)]

    def apply(self, obj:dict) ->bool:
        U,u=int(obj["U"]),int(obj["u"])
        if u <= self.last_id: return False
        if U>self.last_id+1:
            raise SequenceGap(f"Missed depth updates: last={self.last_id} next_U={U}")
        # For first update, ensure it bridges the snapshot's lastUpdateId + 1.
        if not (U<=self.last_id+1<=u):
            raise SequenceGap("Noncontiguous event")
        for name,side in (("b",self.bids),("a",self.asks)):
            for p,q in obj[name]:
                price,qty=float(p),float(q)
                if qty == 0:side.pop(price,None)
                else:side[price]=qty
        self.last_id=u
        self.validate()
        self.synced=True
        return True


def ofi_change(before:tuple,after:tuple)->float:
    b0,qb0,a0,qa0=before
    b1,qb1,a1,qa1=after
    return ((qb1 if b1>=b0 else 0)-(qb0 if b1<=b0 else 0)
            -(qa1 if a1<=a0 else 0)+(qa0 if a1>=a0 else 0))


def features(book:Book,ofi30:deque,flow30:deque)->dict:
    bid,bq,ask,aq=book.top()
    mid=(bid+ask)/2
    mprice=(ask*bq+bid*aq)/(bq+aq)
    lower=mid*(1-0.001)
    upper=mid*(1+0.001)
    biddepth=sum(p*q for p,q in book.bids.items() if p>=lower)
    askdepth=sum(p*q for p,q in book.asks.items() if p<=upper)
    t= time.time()
    while ofi30 and ofi30[0][0] < t-30:ofi30.popleft()
    while flow30 and flow30[0][0] < t-30:flow30.popleft()
    buy=sum(v for _,v in flow30 if v>0)
    sell=-sum(v for _,v in flow30 if v<0)
    return {"exchange_sequence":book.last_id, "mid":mid,
            "spread_bps":(ask-bid)/mid*1e4,
            "obi_top":(bq-aq)/(bq+aq),
            "depth_bid_quote_10bps":biddepth,
            "depth_ask_quote_10bps":askdepth,
            "obi_depth_10bps":(biddepth-askdepth)/(biddepth+askdepth) if biddepth+askdepth else None,
            "microprice_gap_bps":(mprice-mid)/mid*1e4,
            "ofi_top_30s_base_units":sum(v for _,v in ofi30),
            "aggressor_imbalance_30s":(buy-sell)/(buy+sell) if buy+sell else None}

async def record(venue:str, symbol:str, out:Path, duration:float)->None:
    """Collect for a bounded period. Start this on an always-on host for durability."""
    out.mkdir(parents=True,exist_ok=True)
    prefix,rest=ENDPOINTS[venue]
    stream=f"{symbol.lower()}@depth@100ms/{symbol.lower()}@trade"
    path=out/f"{venue}_{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    deadline=time.monotonic()+duration if duration>0 else float('inf')
    stats={"sessions":0,"snapshots":0,"depth_updates":0,"trades":0,"resyncs":0,"feature_rows":0}
    with gzip.open(str(path)+"_raw.jsonl.gz","wt",encoding="utf-8") as raw, \
         gzip.open(str(path)+"_features.jsonl.gz","wt",encoding="utf-8") as processed:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            while time.monotonic()<deadline:
                queue:asyncio.Queue=asyncio.Queue(maxsize=100000)
                pump=None
                try:
                    async with session.ws_connect(prefix+stream,heartbeat=20,timeout=20,max_msg_size=5_000_000) as ws:
                        stats["sessions"]+=1
                        async def receive():
                            async for msg in ws:
                                if msg.type==aiohttp.WSMsgType.TEXT:
                                    recv_ns=time.time_ns()
                                    outer=json.loads(msg.data)
                                    ev=outer.get("data",outer)
                                    channel=outer.get("stream",ev.get("e","unknown"))
                                    # Persist unmodified exchange event + local reception time.
                                    rec={"recv_unix_ns":recv_ns,"channel":channel,"event":ev}
                                    raw.write(json.dumps(rec,separators=(',',':'))+'\n')
                                    raw.flush()
                                    queue.put_nowait((recv_ns,channel,ev))
                                elif msg.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR):
                                    break
                            await queue.put((None,None,None))
                        pump=asyncio.create_task(receive())
                        # Start WS before requesting snapshot; buffered updates bridge the sequence.
                        async with session.get(rest,params={"symbol":symbol.upper(),"limit":1000}) as response:
                            response.raise_for_status()
                            snap=await response.json()
                        raw.write(json.dumps({"recv_unix_ns":time.time_ns(),"channel":"snapshot","event":snap})+'\n')
                        raw.flush()
                        stats["snapshots"]+=1
                        book=Book();book.snapshot(snap)
                        ofi30=deque();flow30=deque()
                        last_emit=0.
                        while time.monotonic()<deadline:
                            recv_ns,channel,event=await asyncio.wait_for(queue.get(),timeout=15)
                            if recv_ns is None:
                                raise ConnectionError("WebSocket closed")
                            etype=event.get("e")
                            if etype=="depthUpdate":
                                before=book.top()
                                if book.apply(event):
                                    stats["depth_updates"]+=1
                                    after=book.top()
                                    ofi30.append((recv_ns/1e9,ofi_change(before,after)))
                            elif etype=="trade":
                                stats["trades"]+=1
                                side=-1 if event.get("m") else 1 # buyer maker => seller aggressive
                                flow30.append((recv_ns/1e9,side*float(event["p"])*float(event["q"])))
                            now=time.time()
                            if book.synced and now-last_emit>=1:
                                row={"recv_unix_ns":time.time_ns(),"venue":venue,"symbol":symbol.upper(),
                                     "valid":True, **features(book,ofi30,flow30)}
                                processed.write(json.dumps(row,separators=(',',':'))+'\n')
                                processed.flush()
                                stats["feature_rows"]+=1
                                last_emit=now
                except (SequenceGap,ValueError,ConnectionError,TimeoutError,aiohttp.ClientError,asyncio.QueueFull) as ex:
                    stats["resyncs"]+=1
                    raw.write(json.dumps({"recv_unix_ns":time.time_ns(),"channel":"collector_error",
                                           "reason":str(ex),"valid":False})+'\n')
                    raw.flush()
                    # Do not emit derived features from an invalid book.
                    await asyncio.sleep(2)
                finally:
                    if pump:
                        pump.cancel()
                        try:await pump
                        except asyncio.CancelledError:pass
    (Path(str(path)+"_stats.json")).write_text(json.dumps(stats,indent=2))
    print(json.dumps({"path_prefix":str(path),**stats},indent=2))


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--venue",choices=ENDPOINTS,default="global")
    ap.add_argument("--symbol",default="BTCUSDT")
    ap.add_argument("--out",type=Path,default=Path("data/live"))
    ap.add_argument("--duration",type=float,default=60,help="seconds; 0 means until stopped")
    args=ap.parse_args()
    try:asyncio.run(record(args.venue,args.symbol,args.out,args.duration))
    except KeyboardInterrupt:print("Interrupted by user")

if __name__=="__main__": main()