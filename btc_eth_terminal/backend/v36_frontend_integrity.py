"""Exactly SHA-pinned v36 Funnest bundle, isolated from frozen v34/v35.1 UI.

Like the existing v34 verifier, all three dist files and the immutable manifest
are verified before serving ANY one of the v36 assets. This is local software
integrity, not secure boot or code signing.
"""
from pathlib import Path
from v34_frontend_integrity import get_asset, FrontendIntegrityError

ROOT=Path(__file__).resolve().parents[1]
DIST=ROOT/'frontend/dist_v36'
MANIFEST=ROOT/'frontend/v36_dist_integrity.json'
PINNED_MANIFEST_SHA256='5d840d2f8856ce77ff958dc37e6e5b38cef69f18427b250ff3fab48ff508837f'
LIVE_JS='/assets/index-Ywn0ndzt.js'
LIVE_CSS='/assets/index-Ckn1nBC7.css'


def v36_asset(path:str)->tuple[bytes,str]:
    if path in ('/research/v36','/research/v36/index.html'):
        path='/research'
    if path not in ('/research',LIVE_JS,LIVE_CSS):
        raise FrontendIntegrityError('UNKNOWN_V36_ASSET_PATH')
    return get_asset(path,dist=DIST,manifest_path=MANIFEST,
                     pinned_sha256=PINNED_MANIFEST_SHA256)
