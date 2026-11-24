"""Q28.1/Q29: require durable, same-run, content-addressed public receipts.

Never accept a missing artifact, a previous run, a synthetic source, an
incomplete trace, or an apparent pass after a reconnect. Only six known
redacted JSON summaries may enter a success bundle. Raw market data stays
private. Hashes bind transport integrity; they are not exchange signatures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
from pathlib import Path

MANIFEST = "evidence_manifest.json"
FILES = {
    "preflight.json": "q22_live_five_hour_preflight_v1",
    "live_monitoring_summary.json": "q22_live_ci_source_sse_resource_v1",
    "replay.json": "q22_5h_capture_two_replay_gate_v1",
    "q24_redacted_ws_close_receipt.json": "q24_p0a_public_close_forensics_v1",
    "q27_redacted_heartbeat_guard.json": "q27_heartbeat_guard_public_receipt_v1",
    "q28_redacted_incident_receipt.json": "q28_public_termination_incident_v1",
}
MAX_BYTES = 256 * 1024
DURATIONS = {180: (1, 60, 150), 1800: (3, 120, 1700),
             18000: (24, 240, 17400)}
HEX = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")


class EvidenceRejected(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise EvidenceRejected(code)


def unique_pairs(pairs):
    out = {}
    for key, val in pairs:
        need(key not in out, "DUPLICATE_JSON_KEY")
        out[key] = val
    return out


def reject_constant(_value):
    raise EvidenceRejected("NONFINITE_JSON")


def read(root, name):
    p = root / name
    s = p.lstat()
    need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1
         and 0 < s.st_size <= MAX_BYTES, "UNSAFE_RECEIPT_FILE")
    raw = p.read_bytes()
    need(len(raw) == s.st_size, "RECEIPT_SIZE_CHANGED")
    obj = json.loads(raw, object_pairs_hook=unique_pairs,
                     parse_constant=reject_constant)
    need(type(obj) is dict, "RECEIPT_NOT_OBJECT")
    return raw, obj


def integer(value, low=0, high=10**15):
    return type(value) is int and low <= value <= high


def number(value, low=0, high=10**15):
    return (type(value) in (int, float) and low <= value <= high
            and math.isfinite(value))


def equals(obj, fields):
    for key, expected in fields.items():
        value = obj.get(key)
        need(type(value) is type(expected) and value == expected,
             "RECEIPT_FIELD_MISMATCH:" + key)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def validate(root, duration, *, manifest=False):
    need(duration in DURATIONS, "UNKNOWN_DURATION")
    need(root.is_dir() and not root.is_symlink(), "UNSAFE_BUNDLE_DIR")
    expected = set(FILES) | ({MANIFEST} if manifest else set())
    need({p.name for p in root.iterdir()} == expected,
         "MISSING_OR_EXTRA_RECEIPT")
    data = {}
    hashes = {}
    for name, schema in FILES.items():
        raw, obj = read(root, name)
        need(obj.get("schema") == schema, "WRONG_SCHEMA:" + name)
        data[name] = obj
        hashes[name] = {"sha256": digest(raw), "bytes": len(raw)}
    p, m, r, w, h, c = (data[name] for name in FILES)
    windows, window_seconds, sample_span = DURATIONS[duration]
    equals(p, {"source_mode": "LIVE_SOURCE", "trading_enabled": False})
    equals(m, {
        "status": "PASS", "capture_target_seconds": duration,
        "windows_passed": windows, "source_session_stable": True,
        "collector_pid_stable": True, "q25_epoch_guard_verified": True,
        "q27_30m_guarded_only": duration == 1800,
        "ephemeral_runner_only": True, "real_orders_enabled": False,
        "order_execution_enabled": False, "24h_persistent_host_certified": False,
    })
    need(number(m.get("sample_span_seconds"), sample_span, duration + 60),
         "MONITOR_SPAN")
    need(integer(m.get("max_rss_bytes"), 1, 2 * 1024**3), "RSS_LIMIT")
    need(integer(m.get("rss_growth_bytes"), 0, 1_100_000_000), "RSS_GROWTH")
    need(integer(m.get("max_open_fds"), 1, 512), "FD_LIMIT")
    need(integer(m.get("min_disk_free_bytes"), 4 * 1024**3 + 500_000_000),
         "DISK_MARGIN")
    records = m.get("sse_windows")
    need(type(records) is list and len(records) == windows, "SSE_COUNT")
    for i, window in enumerate(records, 1):
        equals(window, {"status": "PASS", "window": i})
        need(integer(window.get("max_active_sse"), 8, 512), "SSE_FANOUT")
        need(number(window.get("elapsed_seconds"), window_seconds - 5,
                    window_seconds + 60), "SSE_DURATION")
        need(integer(window.get("total_states"), 1), "SSE_EMPTY")
    need(m.get("sse_total_states") == sum(x["total_states"] for x in records),
         "SSE_TOTAL")
    equals(r, {
        "status": "PASS", "expected_source_seconds": duration,
        "mode": "REAL_COINBASE_PUBLIC_SOURCE_CAPTURE",
        "source_connections": 1, "source_reconnects": 0,
        "primary_independent_final_books_agree": True,
        "real_orders_enabled": False, "persistent_host_24h_certified": False,
        "oos_profitability_verified": False,
    })
    need(number(r.get("source_monotonic_seconds"), duration - 5, duration + 60),
         "SOURCE_DURATION")
    need(integer(r.get("messages"), 1) and integer(r.get("chunks"), 1)
         and integer(r.get("archive_bytes"), 1, 14_000_000_000),
         "ARCHIVE_COUNTS")
    need(integer(m.get("total_source_messages_last_sample"), 1, r["messages"]),
         "MONITOR_ARCHIVE_MESSAGE_COUNT")
    books = r.get("independent_final_book_sha256")
    need(type(books) is dict and set(books) == {"BTC-USD", "ETH-USD"}
         and all(type(x) is str and HEX.fullmatch(x) for x in books.values()),
         "INVALID_BOOK_DIGESTS")
    for key in ("manifest_output_sha256", "observer_evidence_sha256",
                "primary_final_state_sha256"):
        val = r.get(key)
        need(type(val) is str and HEX.fullmatch(val), "INVALID_REPLAY_DIGEST")
    equals(w, {
        "trace_schema_verified": True, "opened_connections": 1,
        "observed_connections": 1, "websocket_error_events": 0,
        "close_events": 1, "probe_exited": True, "last_close_local_request": True,
        "raw_websocket_payloads_saved": False,
        "error_messages_or_close_reasons_saved": False,
        "websocket_endpoint_or_ip_saved": False,
        "root_network_cause_proven": False, "real_orders_enabled": False,
        "execution_authorized": False,
    })
    equals(h, {
        "trace_schema_verified": True, "synthetic_fixture": False,
        "opened_sockets_constructed": 1, "sockets_with_first_heartbeat": 1,
        "interventions_total": 0, "heartbeat_timeout_events": 0,
        "close_failure_events": 0, "collector_stop_observed": True,
        "real_orders_enabled": False, "execution_authorized": False,
    })
    need(integer(h.get("last_heartbeat_count"), 1), "HEARTBEAT_MISSING")
    need(integer(h.get("last_nonheartbeat_message_count"), 1), "L2_MISSING")
    need(w.get("last_reported_heartbeat_count") == h["last_heartbeat_count"],
         "HEARTBEAT_TRACE_DISAGREEMENT")
    need(w.get("last_reported_message_count") ==
         h["last_heartbeat_count"] + h["last_nonheartbeat_message_count"],
         "WS_TRACE_MESSAGE_DISAGREEMENT")
    need(w["last_reported_message_count"] == r["messages"],
         "WS_ARCHIVE_MESSAGE_DISAGREEMENT")
    equals(c, {
        "classification": "DIAGNOSTIC_ONLY_NOT_SOURCE_ACCEPTANCE",
        "observed_termination_class": "LOCAL_NON_GUARD_CLOSE_OBSERVED",
        "q24_trace_validation": "COMMITTED_PREFIX_SCHEMA_VALID",
        "q27_trace_validation": "COMMITTED_PREFIX_SCHEMA_VALID",
        "q24_process_exit_observed": True, "q27_process_exit_observed": True,
        "q24_trace_complete_for_normal_acceptance": True,
        "q27_trace_complete_for_normal_acceptance": True,
        "known_supervisor_failfast": False, "guard_interventions_observed": 0,
        "network_root_cause_proven": False, "real_orders_enabled": False,
        "source_continuity_certified": False,
        "raw_payload_or_close_reason_published": False,
    })
    equals(c["first_socket"], {
        "first_socket_close_observed": True,
        "first_socket_local_close_request_observed": True,
        "first_socket_error_events": 0, "first_socket_q27_timeout_events": 0,
        "second_socket_constructed": False,
    })
    need(c["q24_committed_events"] == w["events_observed"]
         and c["q27_committed_events"] == h["observed_event_count"],
         "FORENSIC_EVENT_COUNT_DISAGREEMENT")
    return hashes


def identity(duration, sha, run_id, attempt):
    need(duration in DURATIONS and SHA.fullmatch(sha) is not None
         and integer(run_id, 1) and attempt == 1, "INVALID_RUN_IDENTITY")
    return {
        "schema": "q29_same_run_evidence_manifest_v1",
        "source_sha": sha, "run_id": run_id, "run_attempt": attempt,
        "duration_seconds": duration, "real_orders_enabled": False,
        "persistent_24h_certified": False,
    }


def seal(root, duration, sha, run_id, attempt):
    meta = identity(duration, sha, run_id, attempt)
    meta["files"] = validate(root, duration)
    raw = (json.dumps(meta, sort_keys=True, indent=2) + "\n").encode()
    with (root / MANIFEST).open("xb") as out:
        out.write(raw)
    return digest(raw)


def verify(root, duration, sha, run_id, attempt, manifest_sha):
    need(type(manifest_sha) is str and HEX.fullmatch(manifest_sha),
         "EXPECTED_MANIFEST_SHA_REQUIRED")
    meta = identity(duration, sha, run_id, attempt)
    meta["files"] = validate(root, duration, manifest=True)
    raw, actual = read(root, MANIFEST)
    need(digest(raw) == manifest_sha, "MANIFEST_SHA_MISMATCH")
    need(actual == meta, "MANIFEST_CONTENT_OR_FILE_SHA_MISMATCH")
    return manifest_sha


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("seal", "verify"))
    p.add_argument("directory", type=Path)
    p.add_argument("--duration", type=int, required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--manifest-sha")
    a = p.parse_args()
    args = (a.directory, a.duration, a.sha, a.run_id, a.attempt)
    result = (seal(*args) if a.mode == "seal"
              else verify(*args, a.manifest_sha))
    print(result)


if __name__ == "__main__":
    main()
