"""结果文件原子写测试：先写临时文件再替换，失败时不损坏原结果文件。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.excel_io import (  # noqa: E402
    read_result_items, write_results, write_results_atomic,
)
from gradehelper.file_claim import ResultFileClaim  # noqa: E402
from gradehelper.models import ItemScore, ParsedReport, RuleItem  # noqa: E402

windows_only = unittest.skipUnless(sys.platform == "win32", "文件占用仅在 Windows 生效")


def _report(student_id: str = "2021001") -> ParsedReport:
    return ParsedReport(
        course="云安全",
        klass="安全2401",
        student_id=student_id,
        std_name="张三",
        report_name="课程报告",
        source_path="2021001.docx",
        md_text="正文",
    )


def _rules() -> list[RuleItem]:
    return [
        RuleItem(key="baseScore", default="25", weight=1.0, method="code",
                 evaluation="", comment="报告已提交，+25分"),
        RuleItem(key="event", default="0-15", weight=0.15, method="LLM",
                 evaluation="事件描述", comment=""),
    ]


def _scores(base: float = 25.0, event: float = 10.0) -> dict[str, dict[str, ItemScore]]:
    return {
        "2021001": {
            "baseScore": ItemScore(key="baseScore", score=base, comment="规则评语"),
            "event": ItemScore(key="event", score=event, confidence=0.9, comment="LLM 评语"),
        }
    }


class TestWriteResultsAtomic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "result.xlsx"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_new_file_and_keeps_no_temp(self):
        write_results_atomic(self.path, [_report()], _rules(), _scores(), mode="upsert")

        self.assertEqual(read_result_items(self.path)["2021001"]["event"].score, 10.0)
        self.assertEqual(list(self.tmp.glob("*.tmp*")), [])

    def test_upsert_keeps_existing_rows(self):
        write_results(self.path, [_report("2021001")], _rules(), _scores(), mode="upsert")
        write_results_atomic(self.path, [_report("2021002")], _rules(),
                             {"2021002": _scores()["2021001"]}, mode="upsert")

        items = read_result_items(self.path)
        self.assertIn("2021001", items)
        self.assertIn("2021002", items)

    def test_retry_after_transient_lock(self):
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "另一个程序正在使用此文件，进程无法访问。", str(dst))
            return real_replace(src, dst)

        with patch("gradehelper.excel_io.os.replace", side_effect=flaky_replace):
            write_results_atomic(self.path, [_report()], _rules(), _scores(),
                                 mode="upsert", retries=3, retry_delay=0)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(read_result_items(self.path)["2021001"]["event"].score, 10.0)

    @windows_only
    def test_locked_target_raises_and_keeps_original_with_temp_copy(self):
        write_results(self.path, [_report()], _rules(), _scores(base=25.0, event=10.0),
                      mode="upsert")
        claim = ResultFileClaim(self.path)
        self.assertTrue(claim.acquire())
        try:
            with self.assertRaises(RuntimeError) as ctx:
                # claim 在外层持有 → 模拟 Excel 一直占着结果文件
                write_results_atomic(self.path, [_report()], _rules(),
                                     _scores(base=25.0, event=12.0),
                                     mode="upsert", retries=1, retry_delay=0)
        finally:
            claim.release()

        self.assertIn("临时文件", str(ctx.exception))
        # 原结果文件未被破坏，新结果完整保留在临时文件中
        self.assertEqual(read_result_items(self.path)["2021001"]["event"].score, 10.0)
        temps = list(self.tmp.glob("*.tmp*"))
        self.assertEqual(len(temps), 1)
        self.assertEqual(read_result_items(temps[0])["2021001"]["event"].score, 12.0)

    @windows_only
    def test_claim_is_reacquired_after_write(self):
        claim = ResultFileClaim(self.path)
        self.assertFalse(claim.acquire())  # 文件不存在时不持有
        write_results_atomic(self.path, [_report()], _rules(), _scores(),
                             mode="upsert", claim=claim)
        self.assertTrue(self.path.is_file())

        self.assertTrue(claim.acquire())
        write_results_atomic(self.path, [_report()], _rules(), _scores(event=13.0),
                             mode="upsert", claim=claim)
        self.assertTrue(claim.held)  # 写入后重新占用成功
        claim.release()
        self.assertEqual(read_result_items(self.path)["2021001"]["event"].score, 13.0)


if __name__ == "__main__":
    unittest.main()