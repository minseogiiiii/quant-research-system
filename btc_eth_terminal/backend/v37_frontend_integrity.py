"""SHA-pinned Factor Lab extension of the same React/Funnest terminal.

Previous v34/v35.1/v36 compiled distributions are immutable and still routed.
"""
from pathlib import Path
from v34_frontend_integrity import get_asset, FrontendIntegrityError

ROOT=Path(__file__).resolve().parents[1]
DIST=ROOT/'frontend/dist_v37'
MANIFEST=ROOT/'frontend/v37_dist_integrity.json'
PINNED_MANIFEST_SHA256='4042289a99ccd21b350a3cae1bc390f688a74803a685143e4a18df7f492763d0'
V37_JS='/assets/index-CwNUwDGE.js'
V37_CSS='/assets/index-B7ewa_f-.css'


def v37_asset(path):
    if path in ('/research/v37','/research/v37/index.html'):
        path='/research'
    if path not in ('/research',V37_JS,V37_CSS):
        raise FrontendIntegrityError('Unknown Q7 versioned asset')
    body,kind=get_asset(path,dist=DIST,manifest_path=MANIFEST,
                        pinned_sha256=PINNED_MANIFEST_SHA256)
    return body,kind
