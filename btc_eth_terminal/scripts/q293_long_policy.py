"""Bounded long-run policy; no 5h acceptance or execution authorization."""
from __future__ import annotations

import os
import time
from pathlib import Path

GIB = 1024**3
DURATIONS = (180, 1800, 18000)
DISK_RESERVE = 4 * GIB


def budgets(seconds):
    if type(seconds) is not int or not 15 <= seconds <= 18000:
        raise ValueError("LONG_DURATION_INVALID")
    # Conservative floor plus explicit bytes/second ceilings per collector.
    # Includes full snapshots. These are caps, not measured demand.
    return max(300_000_000, seconds * 700_000), max(500_000_000, seconds * 900_000)


def admission(seconds, restarts=0):
    if seconds not in DURATIONS or type(restarts) is not int or not 0 <= restarts <= 3:
        raise ValueError("LONG_ADMISSION_INVALID")
    _, archive = budgets(seconds)
    # All attempts remain on disk; reserve full per-attempt cap.
    return 2 * (1 + restarts) * archive + DISK_RESERVE + 2 * GIB


def remaining(deadline, maximum):
    if deadline is None:
        return maximum
    # Collector duration starts only after both snapshots; allow setup and
    # finalization time inside the common absolute wall-clock bound.
    return min(maximum, int(deadline - time.monotonic()) - 50)


def process_sample(root_pid):
    """Scan PPid across all Linux processes; includes thread-spawned children."""
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            pid = int(entry.name)
            # /proc/PID/stat fields 3 onward: state, ppid, ..., utime, stime,
            # starttime and rss. A disappeared process is not a live sample.
            table[pid] = (int(raw[1]), int(raw[11]) + int(raw[12]),
                          int(raw[19]), max(0, int(raw[21])))
        except (OSError, ValueError, IndexError):
            continue
    if root_pid not in table:
        raise ValueError("RESOURCE_ROOT_MISSING")
    selected = {root_pid}
    while True:
        updated = selected | {p for p, v in table.items() if v[0] in selected}
        if updated == selected:
            break
        selected = updated
    ticks = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    return {"rss_bytes": sum(table[p][3] * page for p in selected),
            "process_count": len(selected),
            "cpu_seconds_live_tree": sum(table[p][1] / ticks for p in selected),
            "identity": {(p, table[p][2]) for p in selected}}
