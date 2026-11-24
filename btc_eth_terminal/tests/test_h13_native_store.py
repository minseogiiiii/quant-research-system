import tempfile
import unittest
from pathlib import Path
from h13_native_store import import_archive,inventory,locked,capture
from h2_native_benchmark import generate,BASE
class Store(unittest.TestCase):
    def test_atomic_admission_overlap_and_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);store=root/"store"
            a=root/"a";b=root/"b";c=root/"c"
            pa,*_=generate(a,150000,levels=8)
            pb,*_=generate(b,150000,levels=8,start_ms=BASE+60000)
            pc,*_=generate(c,160000,levels=8,start_ms=BASE+1)
            with self.assertRaises(ValueError):import_archive(store,a,pa)
            x=import_archive(store,a,pa,fixture=True)
            y=import_archive(store,b,pb,fixture=True)
            self.assertTrue((store/"sessions"/pa/"raw"/"manifest.json").is_file())
            self.assertTrue((store/"sessions"/pb/"dataset"/"packets.parquet").is_file())
            with self.assertRaises(ValueError):import_archive(store,a,pa,fixture=True)
            with self.assertRaisesRegex(ValueError,"H4_OVERLAP"):import_archive(store,c,pc,fixture=True)
            r=inventory(store,fixture=True);self.assertEqual(r["catalog"]["session_count"],2)
            self.assertEqual(r["unpublished_attempts"],1)
            self.assertFalse(r["catalog"]["oos_approved"])
            self.assertEqual(x["source_kind"],"SYNTHETIC_TEST_ONLY")
            self.assertNotEqual(x["dataset_receipt_sha256"],y["dataset_receipt_sha256"])
            with self.assertRaises(ValueError):inventory(store)
    def test_lock_and_private_path(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"store"
            with locked(root):
                with self.assertRaises(BlockingIOError):
                    with locked(root):pass
            root.chmod(0o755)
            with self.assertRaises(ValueError):inventory(root)
            with self.assertRaises(ValueError):capture(root,1,50_000_000)

    def test_full_raw_reaudit_detects_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=root/"raw";store=root/"store"
            pin,*_=generate(raw,150000,levels=8)
            import_archive(store,raw,pin,fixture=True)
            r=inventory(store,fixture=True,reaudit=True)
            self.assertTrue(r["raw_chunks_reaudited"])
            self.assertEqual(len(r["raw_audits"]),1)
            chunk=next((store/"sessions"/pin/"raw"/"chunks").glob("*.jsonl"))
            data=chunk.read_bytes();chunk.write_bytes(data[:-1]+b"x")
            self.assertFalse(inventory(store,fixture=True)["raw_chunks_reaudited"])
            with self.assertRaises(ValueError):inventory(store,fixture=True,reaudit=True)
