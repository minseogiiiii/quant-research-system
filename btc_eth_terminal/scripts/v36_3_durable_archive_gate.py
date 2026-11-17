"""Bounded large-capture gate, reusing the unchanged v35 raw-byte auditor.

Never promotes crash fragments, faulted/synthetic sources or reconnects to
continuous market truth. No source mutation, no LIVE or execution admission.
"""
from __future__ import annotations
import argparse
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import sys

BOUNDS={'MAX_MANIFEST_BYTES':24_000_000,'MAX_CHUNKS':20_000,
        'MAX_CHUNK_BYTES':32_000_000,'MAX_LINE_BYTES':18_000_000,
        'MAX_MESSAGES':30_000_000,'MAX_TOTAL_BYTES':80_000_000_000}
SHA=re.compile(r'[0-9a-f]{64}\Z')

class DurableRejected(ValueError):pass


def _gate():
    # Isolated import means frozen v35 limits never change for its callers.
    source=Path(__file__).resolve().parents[1]/'backend/v35_archive_ingress.py'
    spec=importlib.util.spec_from_file_location('_v36_durable_isolated_ingress',source)
    if not spec or not spec.loader:raise DurableRejected('ORIGINAL_VERIFIER_NOT_AVAILABLE')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key,value in BOUNDS.items():setattr(module,key,value)
    return module


def _sha(path,limit):
    if path.is_symlink() or not path.is_file() or not 0<path.stat().st_size<=limit:
        raise DurableRejected('UNSAFE_OR_UNBOUNDED_MANIFEST')
    h=sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()


def _mono_span(root,entries):
    if not entries:return None
    def n(path,first):
        with path.open('rb') as f:
            if first:line=f.readline(BOUNDS['MAX_LINE_BYTES']+1)
            else:
                data=f.read(BOUNDS['MAX_CHUNK_BYTES']+1)
                if len(data)>BOUNDS['MAX_CHUNK_BYTES']:raise DurableRejected('UNBOUNDED_FINAL_CHUNK')
                line=data.splitlines()[-1]
        try:val=json.loads(line)['local_monotonic_ns']
        except (ValueError,KeyError,TypeError,IndexError):
            raise DurableRejected('MISSING_MONOTONIC_SOURCE_EVIDENCE')
        if not isinstance(val,str) or not val.isascii() or not val.isdigit() or len(val)>32:
            raise DurableRejected('BAD_MONOTONIC_SOURCE_EVIDENCE')
        return int(val)
    return (n(root/entries[-1]['file'],False)-
            n(root/entries[0]['file'],True))/1e9


def audit(root:Path,pin:str)->dict:
    root=Path(root)
    if root.is_symlink() or not root.is_dir():raise DurableRejected('UNSAFE_SOURCE_ROOT')
    if not isinstance(pin,str) or not SHA.fullmatch(pin):
        raise DurableRejected('EXTERNAL_SHA256_REQUIRED')
    digest=_sha(root/'manifest.json',BOUNDS['MAX_MANIFEST_BYTES'])
    if digest!=pin:raise DurableRejected('MANIFEST_PIN_MISMATCH')
    try:manifest=json.loads((root/'manifest.json').read_bytes())
    except (ValueError,UnicodeError):raise DurableRejected('INVALID_MANIFEST')
    if not isinstance(manifest,dict):raise DurableRejected('INVALID_MANIFEST_OBJECT')
    chunks=manifest.get('chunks')
    if not isinstance(chunks,list) or len(chunks)>BOUNDS['MAX_CHUNKS']:
        raise DurableRejected('UNBOUNDED_CHUNK_INVENTORY')
    directory=root/'chunks'
    if directory.is_symlink() or not directory.is_dir():raise DurableRejected('UNSAFE_CHUNKS_DIRECTORY')
    pending=[];committed=[]
    for p in directory.iterdir():
        if p.is_symlink() or not p.is_file():raise DurableRejected('UNSAFE_CHUNK_ENTRY')
        if p.name.endswith('.jsonl.partial'):pending.append(p.name)
        elif p.name.endswith('.jsonl'):committed.append(p.name)
        else:raise DurableRejected('UNEXPECTED_CHUNK_ENTRY')
    listed={entry.get('file') for entry in chunks if isinstance(entry,dict)}
    if len(listed)!=len(chunks):raise DurableRejected('DUPLICATE_MANIFEST_CHUNK')
    if any('chunks/'+n not in listed for n in committed):
        raise DurableRejected('UNLISTED_FINALIZED_SOURCE_CHUNK')
    temporary=(root/'manifest.json.tmp').exists()
    if (root/'manifest.json.tmp').is_symlink():raise DurableRejected('UNSAFE_PENDING_MANIFEST')
    # On SIGKILL an active .partial has no finalized chunk manifest entry.
    # Forensic classification must not silently ingest this source.
    cert=None
    if chunks:
        verifier=_gate()
        try:cert=verifier.verify_capture(root,pin)
        except (verifier.ArchiveRejected,OSError) as exc:raise DurableRejected(str(exc))
    elif committed:
        raise DurableRejected('FINALIZED_CHUNKS_NOT_MANIFESTED')
    span=_mono_span(root,chunks) if chunks else None
    requested=manifest.get('required_duration_seconds')
    duration_valid=(type(requested) is int and 15<=requested<=86400
                    and span is not None and span>=requested-5)
    clean=(cert is not None and cert['source_eligible_for_state_replay'] is True
           and not pending and not temporary and duration_valid
           and manifest.get('synthetic_fixture') is not True
           and manifest.get('synthetic_fixture_never_admit') is not True
           and manifest.get('controlled_fault_injection') is None
           and manifest.get('injection_not_triggered') is not True
           and manifest.get('finish_reason')=='DURATION_REACHED'
           and manifest.get('total_reconnects')==0
           and cert['connections']==1)
    return {'schema':'v36_3_durable_archive_gate_v1',
        'classification':'VERIFIED_CLEAN_ARCHIVE_ENVELOPE' if clean else 'QUARANTINED_SOURCE_NO_PROMOTION',
        'manifest_sha256':digest,'sealed_chunks_verified':cert['chunks'] if cert else 0,
        'sealed_messages_verified':cert['messages'] if cert else 0,
        'sealed_bytes_verified':cert['archived_bytes'] if cert else 0,
        'partial_chunks_not_admitted':len(pending),'manifest_tmp_not_admitted':temporary,
        'source_connections':cert['connections'] if cert else len(manifest.get('connections',[])),
        'monotonic_source_span_seconds':round(span,3) if span is not None else None,
        'capture_duration_requested_seconds':requested,
        'eligible_for_archived_replay':clean,
        'independent_exchange_attestation':False,
        'actual_persistent_24h_host_certified':False,
        'live_feed':False,'trading_enabled':False,'execution_authorized':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture',required=True,type=Path)
    p.add_argument('--manifest-sha256',required=True)
    args=p.parse_args()
    try:result=audit(args.capture,args.manifest_sha256)
    except (DurableRejected,OSError,TypeError,KeyError) as exc:
        print('REJECTED_DURABLE_ARCHIVE '+json.dumps({'reason':str(exc),'trading_enabled':False}))
        return 2
    print(json.dumps(result,sort_keys=True))
    return 0 if result['eligible_for_archived_replay'] else 1
if __name__=='__main__':sys.exit(main())
