"""SHA-pinned v35.1 React/Funnest candidate, preserving immutable v34 UI.

A new route is deliberately used until full browser/source verification is done.
The v34 verifier remains authoritative for SHA-checked asset serving.
"""

from pathlib import Path

from v34_frontend_integrity import FrontendIntegrityError, get_asset

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist_v35_1"
MANIFEST = ROOT / "frontend" / "v35_1_dist_integrity.json"
PINNED_MANIFEST_SHA256 = (
    "c3226751a5d7e882a9e113f9b674d7cbabb2144a5d781e9006000b447c4b80d2"
)
CANDIDATE_JS = "/assets/index-Dmz4-5Al.js"
CANDIDATE_CSS = "/assets/index-DgWkONqN.css"


def candidate_asset(path: str) -> tuple[bytes, str]:
    if path in ("/research/v35.1", "/research/v35.1/index.html"):
        path = "/research"
    if path not in ("/research", CANDIDATE_JS, CANDIDATE_CSS):
        raise FrontendIntegrityError("Unknown candidate frontend path")
    return get_asset(
        path, dist=DIST, manifest_path=MANIFEST, pinned_sha256=PINNED_MANIFEST_SHA256
    )
