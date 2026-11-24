"""Read back an immutable Actions artifact by ID. No name-based lookup.

Read-only REST token stays on api.github.com. Redirected blob requests
never receive it. Retries are bounded and only for transport availability.
Identity, archive SHA, member integrity and source receipts fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from q29_evidence_gate import FILES, MANIFEST, MAX_BYTES, verify

LIMIT = 2 * 1024 * 1024
METADATA_LIMIT = 128 * 1024
DELAYS = (0, 2, 4, 8, 16, 30)
SMOKE_NAME = "q29-transport-smoke-not-market"
SMOKE_FILE = "transport_fixture.json"
HEX = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
TRANSIENT = {404, 408, 429, 500, 502, 503, 504}


class TransportRejected(ValueError):
    pass


class TemporaryUnavailable(RuntimeError):
    pass


def need(ok, code):
    if not ok:
        raise TransportRejected(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_limited(response, limit):
    data = response.read(limit + 1)
    need(0 < len(data) <= limit, "RESPONSE_SIZE_LIMIT")
    return data


def request(url, token, *, limit, allow_redirect=False):
    # Only the trusted fixed GitHub API endpoint receives authorization.
    opener = urllib.request.build_opener(NoRedirect())
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "q29-evidence-transport"}
    if token is not None:
        need(urllib.parse.urlsplit(url).netloc == "api.github.com",
             "TOKEN_DESTINATION_REJECTED")
        headers["Authorization"] = "Bearer " + token
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=20) as response:
            return read_limited(response, limit)
    except urllib.error.HTTPError as exc:
        if allow_redirect and exc.code in (301, 302, 303, 307, 308):
            target = urllib.parse.urlsplit(exc.headers.get("Location", ""))
            need(target.scheme == "https" and bool(target.hostname)
                 and target.username is None and target.password is None
                 and target.port in (None, 443), "UNSAFE_BLOB_REDIRECT")
            # Do not forward GitHub token, and do not print signed URL.
            return request(target.geturl(), None, limit=limit)
        if exc.code in TRANSIENT:
            raise TemporaryUnavailable("HTTP_" + str(exc.code)) from None
        raise TransportRejected("HTTP_" + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise TemporaryUnavailable("NETWORK_UNAVAILABLE") from None


def validate_metadata(meta, *, artifact_id, name, run_id, sha, zip_sha):
    need(type(meta) is dict, "INVALID_ARTIFACT_METADATA")
    need(type(meta.get("id")) is int and meta["id"] == artifact_id,
         "ARTIFACT_ID_MISMATCH")
    need(meta.get("name") == name and meta.get("expired") is False,
         "ARTIFACT_NAME_OR_EXPIRY")
    workflow = meta.get("workflow_run")
    need(type(workflow) is dict and type(workflow.get("id")) is int
         and workflow["id"] == run_id and workflow.get("head_sha") == sha,
         "ARTIFACT_RUN_OR_SHA_MISMATCH")
    expected = meta.get("digest")
    need(expected == "sha256:" + zip_sha, "ARTIFACT_METADATA_DIGEST_MISMATCH")
    size = meta.get("size_in_bytes")
    need(type(size) is int and 0 < size <= LIMIT, "ARTIFACT_SIZE_LIMIT")
    return size


def fetch_bytes(repo, token, *, artifact_id, name, run_id, sha, zip_sha):
    need(type(repo) is str and REPO.fullmatch(repo) is not None
         and bool(token), "INVALID_GITHUB_IDENTITY")
    need(type(artifact_id) is int and artifact_id > 0
         and type(run_id) is int and run_id > 0
         and SHA.fullmatch(sha) is not None
         and HEX.fullmatch(zip_sha) is not None, "INVALID_EXPECTED_IDENTITY")
    endpoint = "https://api.github.com/repos/" + repo
    endpoint += "/actions/artifacts/" + str(artifact_id)
    for i, delay in enumerate(DELAYS, 1):
        if delay:
            time.sleep(delay)
        try:
            raw = request(endpoint, token, limit=METADATA_LIMIT)
            meta = json.loads(raw)
            size = validate_metadata(
                meta, artifact_id=artifact_id, name=name, run_id=run_id,
                sha=sha, zip_sha=zip_sha,
            )
            archive = request(endpoint + "/zip", token, limit=LIMIT,
                              allow_redirect=True)
            need(len(archive) == size, "ZIP_SIZE_MISMATCH")
            need(hashlib.sha256(archive).hexdigest() == zip_sha,
                 "ZIP_DIGEST_MISMATCH")
            return archive
        except TemporaryUnavailable as exc:
            # Only fixed status codes; no API response bodies or signed URLs.
            print(f"Q29_ARTIFACT_RETRY attempt={i} code={exc}", flush=True)
    raise TransportRejected("ARTIFACT_RETRY_BUDGET_EXHAUSTED")


def extract(archive, dest, names, validator):
    need(not dest.exists() and not dest.is_symlink(),
         "DESTINATION_MUST_BE_FRESH")
    need(dest.parent.is_dir(), "DESTINATION_PARENT_MISSING")
    with tempfile.TemporaryDirectory(prefix="q29_artifact_", dir=dest.parent) as t:
        staging = Path(t) / "verified"
        staging.mkdir(mode=0o700)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = bundle.infolist()
            need(len(entries) == len(names)
                 and {e.filename for e in entries} == set(names),
                 "ZIP_MEMBERS_MISMATCH")
            for e in entries:
                mode = (e.external_attr >> 16) & 0xFFFF
                need(not e.is_dir() and not stat.S_ISLNK(mode)
                     and not (e.flag_bits & 1)
                     and 0 < e.file_size <= MAX_BYTES,
                     "UNSAFE_ZIP_MEMBER")
                with bundle.open(e) as src:
                    data = src.read(MAX_BYTES + 1)
                need(len(data) == e.file_size, "ZIP_MEMBER_SIZE_MISMATCH")
                with (staging / e.filename).open("xb") as output:
                    output.write(data)
        validator(staging)
        staging.rename(dest)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--artifact-id", type=int, required=True)
    p.add_argument("--zip-sha", required=True)
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--duration", type=int, choices=(180, 1800, 18000))
    p.add_argument("--manifest-sha")
    p.add_argument("--smoke-sha")
    a = p.parse_args()
    try:
        need(os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
             and os.environ.get("GITHUB_RUN_ID") == str(a.run_id)
             and os.environ.get("GITHUB_SHA") == a.sha, "RUN_IDENTITY_MISMATCH")
        if a.smoke_sha is not None:
            need(a.duration is None and a.manifest_sha is None
                 and HEX.fullmatch(a.smoke_sha), "INVALID_SMOKE_MODE")
            name = SMOKE_NAME
            names = {SMOKE_FILE}

            def validator(root):
                need(hashlib.sha256((root / SMOKE_FILE).read_bytes()).hexdigest()
                     == a.smoke_sha, "SMOKE_FIXTURE_MISMATCH")
        else:
            need(a.duration is not None and a.manifest_sha is not None,
                 "MISSING_SOURCE_EVIDENCE_GATE")
            name = f"q29-{a.duration}s-validated-evidence"
            names = set(FILES) | {MANIFEST}

            def validator(root):
                verify(root, a.duration, a.sha, a.run_id, 1, a.manifest_sha)

        archive = fetch_bytes(
            os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"],
            artifact_id=a.artifact_id, name=name, run_id=a.run_id,
            sha=a.sha, zip_sha=a.zip_sha,
        )
        extract(archive, a.directory, names, validator)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        # Only our fixed error labels are allowed in logs.
        safe = str(exc) if isinstance(exc, TransportRejected) else type(exc).__name__
        print("FAIL_Q29_ARTIFACT_TRANSPORT " + safe, flush=True)
        return 1
    print(f"PASS_Q29_ARTIFACT_ID_ROUNDTRIP id={a.artifact_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
