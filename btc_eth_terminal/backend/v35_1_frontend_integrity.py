"""SHA-pinned v35.1 React/Funnest candidate, preserving immutable v34 UI.

A new route is deliberately used until full browser/source verification is done.
The v34 verifier remains authoritative for SHA-checked asset serving.
"""
from pathlib import Path
from v34_frontend_integrity import get_asset, verify_build, FrontendIntegrityError

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'frontend' / 'dist_v35_1'
MANIFEST = ROOT / 'frontend' / 'v35_1_dist_integrity.json'
PINNED_MANIFEST_SHA256 = '51216df2dd58abcf34732e46765ec0d196df1140a7fca9976ec80eb6a9f47888'
CANDIDATE_JS = '/assets/index-a80XelIM.js'
CANDIDATE_CSS = '/assets/index-DgWkONqN.css'


def candidate_asset(path: str) -> tuple[bytes, str]:
    if path in ('/research/v35.1', '/research/v35.1/index.html'):
        path = '/research'
    if path not in ('/research', CANDIDATE_JS, CANDIDATE_CSS):
        raise FrontendIntegrityError('Unknown candidate frontend path')
    return get_asset(path, dist=DIST, manifest_path=MANIFEST,
                     pinned_sha256=PINNED_MANIFEST_SHA256)