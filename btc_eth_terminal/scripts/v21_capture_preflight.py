"""Observe, do not claim: local suitability for Coinbase public capture.

No trading, no WebSocket subscriptions or background collection. DNS check only;
external connectivity and 24h durability cannot be inferred from host metadata.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
import platform
from pathlib import Path
import shutil
import socket
import subprocess
import sys


def assess(root: Path, duration: int):
    if duration<60 or duration>259200:
        raise ValueError('Duration must be between 60 and 259200 seconds')
    candidates=[]
    node=shutil.which('node')
    if node:
        result=subprocess.run([node,'--version'],capture_output=True,text=True,timeout=4)
        node_version=result.stdout.strip()
    else:
        node_version=None
        candidates.append('NODE_MISSING')
    if not node_version or not node_version.startswith('v') or int(node_version[1:].split('.')[0])<22:
        candidates.append('NODE_22_REQUIRED')
    if sys.version_info<(3,11):
        candidates.append('PYTHON_3_11_REQUIRED')
    if not root.is_dir() or root.is_symlink():
        candidates.append('PERSISTENT_WRITABLE_DIRECTORY_NOT_ESTABLISHED')
        free=None
    else:
        try:
            free=shutil.disk_usage(root).free
        except OSError:
            free=None
            candidates.append('DISK_FREE_SPACE_UNKNOWN')
    # Based only on one historical hour; not a burst bound or capacity guarantee.
    estimated=round(325724466 * duration / 3600)
    min_required=estimated*2 + 4*1024**3
    if free is not None and free<min_required:
        candidates.append('DISK_ESTIMATE_PLUS_RESERVE_INSUFFICIENT')
    try:
        records=socket.getaddrinfo('advanced-trade-ws.coinbase.com',443,type=socket.SOCK_STREAM)
        dns_status='RESOLVED_NOT_PROOF_OF_WEBSOCKET_CONNECTIVITY'
    except (socket.gaierror,OSError):
        records=[]
        dns_status='FAILED_DNS_LOOKUP'
        candidates.append('COINBASE_PUBLIC_DNS_UNAVAILABLE')
    # Process supervision, actual clock sync, and long-run connection require external proof.
    candidates.extend(['HOST_PROCESS_SUPERVISION_NOT_VERIFIED',
                       'CLOCK_SYNC_NOT_INDEPENDENTLY_ATTESTED',
                       'PERSISTENT_24H_RUNTIME_NOT_PROVEN'])
    return {'timestamp_utc':datetime.now(timezone.utc).isoformat(),
        'status':'NOT_READY_FOR_VERIFIED_24H_CAPTURE' if candidates else 'LOCAL_PREFLIGHT_ONLY',
        'python_version':platform.python_version(),'node_version':node_version,
        'requested_duration_seconds':duration,'storage_root':str(root.resolve()) if root.exists() else str(root),
        'disk_free_bytes_at_check':free,'historical_one_hour_archived_jsonl_bytes':325724466,
        'linear_archive_growth_estimate_bytes_not_guaranteed':estimated,
        'estimated_double_capacity_plus_4GiB_bytes_not_guaranteed':min_required,
        'dns_status':dns_status,'dns_addresses_returned':len(records),
        'blockers_or_unverified_controls':candidates,
        'real_coinbase_websocket_connected':False,'24h_continuous_source_verified':False,
        'clock_calibration_verified':False,'orders_sent':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--duration',type=int,default=86400)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    out=assess(a.root,a.duration)
    a.output.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n')
    print(out['status'])
