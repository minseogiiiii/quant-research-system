"""Private fsynced per-source lifecycle ledger (not exchange attestation)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from q292_incident_journal import canonical

MAX_ROWS = 20000
MAX_LINE = 2048
ZERO = "0" * 64
STATES = {"ABSENT", "READY", "NOT_READY", "RETIRED", "STOPPED"}


class SourceLedger:
    def __init__(self, path):
        path = Path(path)
        info = path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("SOURCE_LEDGER_DIRECTORY")
        self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                          os.O_NOFOLLOW, 0o600)
        try:
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError:
            os.close(self.fd)
            raise
        self.previous = ZERO
        self.count = 0
        self.last = {}
        self.failed = False

    def record(self, name, attempt, state, session=None):
        if self.failed:
            raise ValueError("SOURCE_LEDGER_FAILED")
        if (name not in ("primary", "secondary") or type(attempt) is not int
                or not 0 <= attempt <= 4 or state not in STATES
                or (state == "READY") != (session is not None)
                or session is not None and not re.fullmatch("[0-9a-f]{32}", session)):
            raise ValueError("SOURCE_LEDGER_SCHEMA")
        digest = hashlib.sha256(session.encode()).hexdigest() if session else None
        value = (attempt, state, digest)
        if value == self.last.get(name):
            return
        if name in self.last and attempt < self.last[name][0]:
            raise ValueError("SOURCE_LEDGER_ATTEMPT_REGRESSION")
        row = {"index": self.count, "mono_ns": time.monotonic_ns(),
               "source": name, "attempt": attempt, "state": state,
               "session_sha256": digest, "previous": self.previous,
               "real_orders_enabled": False}
        chain = hashlib.sha256(canonical(row)).hexdigest()
        raw = canonical({**row, "sha256": chain}) + b"\n"
        if self.count >= MAX_ROWS or len(raw) > MAX_LINE:
            raise ValueError("SOURCE_LEDGER_BOUND")
        try:
            view = memoryview(raw)
            while view:
                n = os.write(self.fd, view)
                if n <= 0:
                    raise OSError("SOURCE_LEDGER_WRITE")
                view = view[n:]
            os.fsync(self.fd)
        except OSError:
            self.failed = True
            raise
        self.previous = chain
        self.count += 1
        self.last[name] = value

    def observe(self, sources, observations):
        for source, observation in zip(sources, observations, strict=True):
            state = ("READY" if observation is not None else
                     "RETIRED" if source.retiring else
                     "NOT_READY" if source.worker is not None else "ABSENT")
            self.record(source.name, source.attempts, state,
                        observation[0].session if observation is not None else None)

    def close(self):
        os.close(self.fd)


def audit(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    previous, count, clock = ZERO, 0, -1
    states = {p: {} for p in ("primary", "secondary")}
    attempts = dict.fromkeys(states, 0)
    torn = False
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_mode & 0o077
                or before.st_size > MAX_ROWS * MAX_LINE):
            raise ValueError("SOURCE_LEDGER_UNSAFE")
        while raw := stream.readline(MAX_LINE + 1):
            if len(raw) > MAX_LINE or count >= MAX_ROWS:
                raise ValueError("SOURCE_LEDGER_BOUND")
            if not raw.endswith(b"\n"):
                torn = True
                break
            row = json.loads(raw)
            chain = row.pop("sha256")
            name, attempt, state = row["source"], row["attempt"], row["state"]
            session = row["session_sha256"]
            if (set(row) != {"index", "mono_ns", "source", "attempt", "state",
                             "session_sha256", "previous", "real_orders_enabled"}
                    or type(row["index"]) is not int or row["index"] != count
                    or type(row["mono_ns"]) is not int or row["mono_ns"] <= clock
                    or name not in states or type(attempt) is not int
                    or not attempts[name] <= attempt <= 4 or state not in STATES
                    or (state == "READY") != (session is not None)
                    or session is not None and not re.fullmatch("[0-9a-f]{64}", session)
                    or row["previous"] != previous
                    or row["real_orders_enabled"] is not False
                    or hashlib.sha256(canonical(row)).hexdigest() != chain):
                raise ValueError("SOURCE_LEDGER_INTEGRITY")
            count += 1
            previous, clock = chain, row["mono_ns"]
            attempts[name] = attempt
            states[name][state] = states[name].get(state, 0) + 1
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("SOURCE_LEDGER_CHANGED")
    return {"records": count, "terminal_sha256": previous, "torn_tail": torn,
            "states": states, "attempts": attempts,
            "source_continuity_certified": False, "real_orders_enabled": False}
