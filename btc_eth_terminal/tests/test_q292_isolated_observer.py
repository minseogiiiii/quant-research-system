"""Linux process faults against original collectors, synthetic feed only."""
import os
import signal
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from q292_dual_gateway import DualRoute, cursor
from q292_isolated_observer import (
    MAX_REPLY,
    ObserverProcess,
    decode_observation,
    receive_reply,
)


def running(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


class IsolationContracts(unittest.TestCase):
    def test_worker_death_stall_and_orphan_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="q292iso_") as name:
            root = Path(name)
            sources = []
            try:
                for label in ("primary", "secondary"):
                    sources.append(ObserverProcess(label, root / label, 30, True))
                p, s = sources
                route = DualRoute(p, s)

                def until(label):
                    end = time.monotonic() + 12
                    while time.monotonic() < end:
                        result = route.read()
                        if result["active_source"] == label:
                            return result
                        time.sleep(.12)
                    self.fail("ISOLATED_ROUTE_NOT_READY:" + label)

                first = until("primary")
                self.assertNotEqual(p.process.pid, s.process.pid)
                children = Path(
                    f"/proc/{p.process.pid}/task/{p.process.pid}/children"
                ).read_text().split()
                self.assertEqual(len(children), 1)
                node_pid = int(children[0])
                self.assertTrue(running(node_pid))
                p.process.kill()
                p.process.wait(timeout=4)
                second = until("secondary")
                self.assertNotEqual(cursor(first), cursor(second))
                self.assertFalse(second["zero_gap_certified"])
                self.assertEqual(set(second["states"]), {"BTC-USD", "ETH-USD"})
                deadline = time.monotonic() + 5
                while running(node_pid) and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertFalse(running(node_pid), "orphan collector survived")

                os.kill(s.process.pid, signal.SIGSTOP)
                start = time.monotonic()
                down = route.read()
                self.assertLess(time.monotonic() - start, 2.5)
                self.assertIsNone(down["states"])
                self.assertTrue(s.failed)
                os.kill(s.process.pid, signal.SIGCONT)
                # A delayed response must not resurrect an invalidated IPC.
                time.sleep(.2)
                self.assertIsNone(route.read()["states"])
            finally:
                for source in sources:
                    if source.process.poll() is None:
                        os.kill(source.process.pid, signal.SIGCONT)
                    source.close()

    def test_bounded_frame_and_deadline(self):
        left, right = socket.socketpair()
        with left, right:
            right.sendall(struct.pack("!I", MAX_REPLY + 1))
            with self.assertRaises(ValueError):
                receive_reply(left, time.monotonic() + .2)
        left, right = socket.socketpair()
        with left, right:
            right.sendall(struct.pack("!I", 20) + b"{")
            with self.assertRaises(TimeoutError):
                receive_reply(left, time.monotonic() + .1)

    def test_malformed_identity_rejected(self):
        with self.assertRaises(ValueError):
            decode_observation({"source": {}, "pair": {}}, "primary", True)
        self.assertIsNone(decode_observation(None, "primary", True))


if __name__ == "__main__":
    unittest.main()
