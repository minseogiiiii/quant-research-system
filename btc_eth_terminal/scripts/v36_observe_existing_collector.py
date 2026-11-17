"""Opt-in read-only v36 observer for the UNCHANGED v29 underlying Node collector.

The v29 operator uses scripts/btc_eth_multiday_v20.mjs. This tool executes
that exact existing collector with only an OPTIONAL local Unix tap added. Raw
archive writes, budgets, chunk hashes and manifest rules remain independent
of UI availability. No credentials or real order APIs. Linux SO_PEERCRED is
required to prove the producer PID for a live admission attempt.

Example: python scripts/v36_observe_existing_collector.py --execute-public-capture \
  --workdir /private/new/empty/workdir --duration 120 --port 8766
"""
from __future__ import annotations
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import sys
from threading import Thread
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from v36_live_engine import LiveMarketEngine, MAX_LINE
from v36_live_gateway import server_for_live
from v35_replay_service import StateRejected

COLLECTOR=ROOT/'scripts/btc_eth_multiday_v20.mjs'


def read_tap(connection:socket.socket,engine:LiveMarketEngine,done:list):
    buffer=bytearray()
    try:
        connection.settimeout(2.0)
        while not done[0]:
            try: data=connection.recv(1<<16)
            except socket.timeout:continue
            if not data:
                engine.transport_lost('COLLECTOR_OBSERVATION_SOCKET_CLOSED');return
            buffer.extend(data)
            if len(buffer)>MAX_LINE+65536:
                engine.transport_lost('LOCAL_OBSERVER_BACKPRESSURE_EXCEEDED');return
            while True:
                idx=buffer.find(b'\n')
                if idx<0:break
                line=bytes(buffer[:idx+1]);del buffer[:idx+1]
                try:engine.consume(line)
                except StateRejected:
                    # The only safe continuity action is a new full source
                    # connection. The collector continues archiving regardless.
                    engine.transport_lost('LIVE_ADMISSION_REJECTED');return
    except OSError as exc:
        engine.transport_lost('OBSERVER_SOCKET_FAILURE:'+type(exc).__name__)
    finally:
        try:connection.close()
        except OSError:pass


def main(argv=None)->int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--workdir',required=True,type=Path)
    ap.add_argument('--execute-public-capture',action='store_true')
    ap.add_argument('--duration',type=int,default=120)
    ap.add_argument('--port',type=int,default=8766)
    ap.add_argument('--raw-budget-bytes',type=int,default=15_000_000_000)
    ap.add_argument('--archive-budget-bytes',type=int,default=20_000_000_000)
    ap.add_argument('--disk-reserve-bytes',type=int,default=4*1024**3)
    args=ap.parse_args(argv)
    if not args.execute_public_capture:
        print('BLOCKED: explicit --execute-public-capture required',file=sys.stderr)
        return 2
    if not sys.platform.startswith('linux') or not hasattr(socket,'SO_PEERCRED'):
        print('BLOCKED: Linux SO_PEERCRED unavailable',file=sys.stderr)
        return 2
    if not 15<=args.duration<=3600 or not 1024<=args.port<=65535:
        ap.error('duration 15..3600, port 1024..65535')
    if (args.raw_budget_bytes<1024 or args.archive_budget_bytes<args.raw_budget_bytes or
        args.disk_reserve_bytes<4*1024**3):
        ap.error('Storage budgets must preserve raw<=archive and >=4GiB disk reserve')
    work=args.workdir
    if work.exists() or work.is_symlink():
        ap.error('Workdir MUST NOT exist (no preexisting raw evidence can be reused)')
    if not shutil.which('node'):
        ap.error('Node 22+ must be installed')
    original=COLLECTOR.read_bytes()
    script_hash=sha256(original).hexdigest()
    work.mkdir(parents=True,mode=0o700)
    tap_path=work/'v36_observer.sock'
    if len(os.fsencode(tap_path))>100:
        ap.error('Private observation socket path too long')
    listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    listener.bind(str(tap_path));os.chmod(tap_path,0o600)
    listener.listen(1);listener.settimeout(2.0)
    engine=LiveMarketEngine(mode='LIVE_SOURCE',collector_runtime_verified=True,
                             collector_script_sha256=script_hash)
    server=server_for_live(engine,port=args.port)
    gateway_thread=Thread(target=server.serve_forever,daemon=True)
    gateway_thread.start()
    env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),
         'SOAK_DURATION_SECONDS':str(args.duration),
         'SOAK_RAW_BUDGET_BYTES':str(args.raw_budget_bytes),
         'SOAK_ARCHIVE_BUDGET_BYTES':str(args.archive_budget_bytes),
         'SOAK_DISK_RESERVE_BYTES':str(args.disk_reserve_bytes),
         'SOAK_CHUNK_LIMIT_BYTES':'8000000',
         'SOAK_TEST_FIXTURE':'0','SOAK_TEST_DISCONNECT_AFTER_MESSAGES':'0',
         'V36_LIVE_TAP_SOCKET':str(tap_path)}
    done=[False];proc=None
    start=time.monotonic()
    try:
        with (work/'collector_stdout.log').open('xb') as stdout, (work/'collector_stderr.log').open('xb') as stderr:
            proc=subprocess.Popen([shutil.which('node'),str(COLLECTOR)],cwd=work,
                                   env=env,stdout=stdout,stderr=stderr,start_new_session=True)
            try:
                while proc.poll() is None:
                    try:client,_=listener.accept()
                    except socket.timeout:continue
                    # Verify same-user exact child PID, not an arbitrary local
                    # process pretending to supply accepted exchange messages.
                    peer=client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
                    pid,uid,_gid=struct.unpack('3i',peer)
                    if pid!=proc.pid or uid!=os.getuid():
                        client.close();engine.transport_lost('COLLECTOR_PEER_CREDENTIAL_MISMATCH');continue
                    worker=Thread(target=read_tap,args=(client,engine,done),daemon=True)
                    worker.start()
                    # The collector tap is intentionally single-connection.
                    # Dropping it does NOT cause capture termination.
                    break
                while proc.poll() is None:
                    time.sleep(.2)
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)
            engine.stopped()
            stdout.flush();os.fsync(stdout.fileno())
            stderr.flush();os.fsync(stderr.fileno())
        candidates=list((work/'soak_evidence').glob('coinbase_btc_eth_soak_*'))
        source=candidates[0] if len(candidates)==1 else None
        manifest=source/'manifest.json' if source else None
        source_hash=sha256(manifest.read_bytes()).hexdigest() if manifest and manifest.is_file() else None
        evidence={'schema':'v36_observer_evidence_v1','mode':'LIVE_SOURCE',
                  'source_attestation':'HOST_PID_AND_CLOCK_ONLY_NOT_SIGNED_EXCHANGE',
                  'producer_script_sha256':script_hash,'collector_exit_code':proc.returncode,
                  'elapsed_seconds':round(time.monotonic()-start,3),
                  'captured_manifest_sha256':source_hash,
                  'captured_dir':str(source) if source else None,
                  'final_observer_health':engine.health(),
                  'execution_authorized':False,'trading_enabled':False}
        p=work/'V36_OBSERVER_EVIDENCE.json'
        with p.open('x') as stream:
            json.dump(evidence,stream,indent=2,sort_keys=True);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        print(json.dumps(evidence,sort_keys=True))
        return 0 if proc.returncode==0 and source_hash else 2
    finally:
        done[0]=True
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        server.shutdown();server.server_close();gateway_thread.join(timeout=3)
        listener.close()
        try:tap_path.unlink()
        except FileNotFoundError:pass

if __name__=='__main__':raise SystemExit(main())
