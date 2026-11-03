"""结果文件独占占用测试：doc / sen 阶段运行期间阻止其它程序打开结果文件。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.file_claim import ResultFileClaim  # noqa: E402

windows_only = unittest.skipUnless(sys.platform == "win32", "独占占用仅在 Windows 生效")


@windows_only
class TestResultFileClaim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "result.xlsx"
        self.path.write_bytes(b"fake-xlsx")

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_not_claimed(self):
        # 首次运行结果文件还不存在：没有可占用的对象，但不报错
        claim = ResultFileClaim(self.tmp / "missing.xlsx")
        self.assertFalse(claim.acquire())
        self.assertFalse(claim.held)

    def test_none_path_is_not_claimed(self):
        claim = ResultFileClaim(None)
        self.assertFalse(claim.acquire())
        claim.release()  # 未持有时释放不抛异常

    def test_claim_blocks_writers_and_allows_readers(self):
        claim = ResultFileClaim(self.path)
        self.assertTrue(claim.acquire())
        try:
            with self.assertRaises(PermissionError):
                with self.path.open("r+b"):
                    pass
            with self.path.open("rb") as fh:
                self.assertEqual(fh.read(), b"fake-xlsx")
        finally:
            claim.release()

    def test_second_claim_reports_file_in_use(self):
        first = ResultFileClaim(self.path)
        self.assertTrue(first.acquire())
        try:
            with self.assertRaises(RuntimeError) as ctx:
                ResultFileClaim(self.path).acquire()
            self.assertIn("正被其它程序", str(ctx.exception))
            quiet = ResultFileClaim(self.path)
            self.assertFalse(quiet.acquire(raise_on_error=False))
            self.assertFalse(quiet.held)
        finally:
            first.release()

    def test_release_makes_file_writable_again(self):
        claim = ResultFileClaim(self.path)
        claim.acquire()
        claim.release()
        claim.release()  # 重复释放不抛异常
        with self.path.open("r+b"):
            pass

    def test_retarget_moves_claim_to_new_file(self):
        other = self.tmp / "other.xlsx"
        other.write_bytes(b"other")
        claim = ResultFileClaim(self.path)
        claim.acquire()
        self.assertTrue(claim.retarget(other))
        self.assertEqual(claim.path, other)
        try:
            with self.assertRaises(PermissionError):
                with other.open("r+b"):
                    pass
            with self.path.open("r+b"):  # 旧文件已释放
                pass
        finally:
            claim.release()

    def test_context_manager_releases(self):
        with ResultFileClaim(self.path):
            pass
        with self.path.open("r+b"):
            pass


if __name__ == "__main__":
    unittest.main()
