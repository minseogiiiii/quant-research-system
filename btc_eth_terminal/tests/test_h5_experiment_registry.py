import sqlite3
import tempfile
import unittest
from pathlib import Path

from h5_experiment_registry import append, create, sha, snapshot


def plan(n=2):
    return dict(
        schema="h5_experiment_plan_v1",
        source_kind="SYNTHETIC_TEST_ONLY",
        dataset_catalog_sha256="a" * 64,
        strategy_code_sha256="b" * 64,
        cost_policy_sha256="c" * 64,
        split_policy_sha256="d" * 64,
        family="fixture",
        seed=42,
        trials=[dict(trial_id=sha({"i": i}), parameters={"i": i}) for i in range(n)],
    )


class Registry(unittest.TestCase):
    def test_state_and_retained_failure(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.db"
            p = plan()
            pin, head = create(path, p)
            a, b = [t["trial_id"] for t in p["trials"]]
            with self.assertRaises(ValueError):
                append(path, pin, head, dict(event="FAIL", trial_id=a, reason="BAD"))
            old = head
            head = append(path, pin, head, dict(event="START", trial_id=a))
            with self.assertRaises(ValueError):
                append(path, pin, old, dict(event="START", trial_id=b))
            head = append(
                path, pin, head, dict(event="FAIL", trial_id=a, reason="MISSING_DATA")
            )
            head = append(path, pin, head, dict(event="START", trial_id=b))
            head = append(
                path,
                pin,
                head,
                dict(
                    event="COMPLETE",
                    trial_id=b,
                    return_series_sha256="e" * 64,
                    result_sha256="f" * 64,
                ),
            )
            r = snapshot(path, pin, head)
            self.assertTrue(r["all_terminal"])
            self.assertFalse(r["all_complete"])
            self.assertEqual(r["state"][a], "FAILED")
            with self.assertRaises(ValueError):
                append(path, pin, head, dict(event="START", trial_id=a))

    def test_tamper_and_external_pin(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.db"
            pin, head = create(path, plan())
            with self.assertRaises(ValueError):
                snapshot(path, "0" * 64, head)
            with sqlite3.connect(path) as db:
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute("DELETE FROM journal")
                db.execute("DROP TRIGGER no_update")
                db.execute("UPDATE journal SET digest=?", ("0" * 64,))
            with self.assertRaises(ValueError):
                snapshot(path, pin, head)
