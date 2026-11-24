"""Linux parent-bound exec; close the parent-death setup race before exec."""
from __future__ import annotations

import ctypes
import os
import signal
import sys


def main():
    if not sys.platform.startswith("linux") or len(sys.argv) < 3:
        raise SystemExit("LINUX_PARENT_BOUND_EXEC_REQUIRED")
    parent = int(sys.argv[1])
    if parent <= 1 or os.getppid() != parent:
        raise SystemExit("PARENT_ALREADY_GONE")
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG: kernel delivers SIGTERM when the creating thread dies.
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG_FAILED")
    if os.getppid() != parent:
        raise SystemExit("PARENT_GONE_DURING_SETUP")
    os.execv(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
