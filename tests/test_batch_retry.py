"""通用批处理测试：多线程、异常队列、自动重试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.batch import run_file_batch  # noqa: E402
from gradehelper.cancel import LLMAborted  # noqa: E402
from gradehelper.config import AppConfig, EndpointConfig, PathsConfig  # noqa: E402
from gradehelper.excel_io import read_result_items, write_results  # noqa: E402
from gradehelper.grader import Grader  # noqa: E402
from gradehelper.models import ItemScore, ParsedReport, RuleItem  # noqa: E402
from gradehelper.parser import parse_all  # noqa: E402


class TestRunFileBatch(unittest.TestCase):
    def test_failed_item_is_retried_and_results_keep_order(self):
        attempts: dict[str, int] = {}

        def worker(path: Path) -> str:
            attempts[path.name] = attempts.get(path.name, 0) + 1
            if path.name == "b.txt" and attempts[path.name] == 1:
                raise OSError("暂时失败")
            return f"ok:{path.name}"

        items = [Path("a.txt"), Path("b.txt"), Path("c.txt")]
        outcome = run_file_batch(
            stage="doc", items=items, worker=worker, workers=2,
            path_of=lambda path: path, label_of=lambda path: path.name,
            unit="份", max_attempts=2, progress_enabled=False,
        )

        self.assertEqual(outcome.results, ["ok:a.txt", "ok:b.txt", "ok:c.txt"])
        self.assertEqual(len(outcome.failures), 0)
        self.assertEqual(attempts["b.txt"], 2)

    def test_persistent_failure_does_not_interrupt_others(self):
        final_failures = []

        def worker(path: Path) -> str:
            if path.name == "bad.txt":
                raise RuntimeError("始终失败")
            return path.name

        items = [Path("good.txt"), Path("bad.txt")]
        with self.assertLogs("gradehelper.batch", level="ERROR"):
            outcome = run_file_batch(
                stage="doc", items=items, worker=worker, workers=2,
                path_of=lambda path: path, unit="份", max_attempts=2,
                progress_enabled=False, on_failure=final_failures.append,
            )

        self.assertEqual(outcome.results, ["good.txt"])
        self.assertEqual(len(outcome.failures), 1)
        self.assertEqual([f.path.name for f in final_failures], ["bad.txt"])
        self.assertEqual(final_failures[0].attempts, 2)

    def test_abort_exception_propagates_and_is_not_queued(self):
        def worker(path: Path) -> str:
            raise LLMAborted("用户取消")

        with self.assertRaises(LLMAborted):
            run_file_batch(
                stage="doc", items=[Path("a.txt")], worker=worker, workers=1,
                path_of=lambda path: path, unit="份", max_attempts=3,
                progress_enabled=False,
            )


class TestParseAllRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parse_failure_is_retried_before_reporting(self):
        bad = self.tmp / "云安全-安全2401-2021001-张三-课程报告.docx"
        bad.write_bytes(b"docx")
        calls = {"n": 0}

        def fake_parse(path, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("docling 暂时失败")
            from gradehelper.models import ParsedReport
            return ParsedReport(
                course="云安全", klass="安全2401", student_id="2021001",
                std_name="张三", report_name="课程报告",
                source_path=str(path), md_text="正文",
            )

        errors = []
        with patch("gradehelper.parser.parse_report", fake_parse):
            reports = parse_all(
                self.tmp,
                name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
                separator="-",
                on_error=lambda path, exc: errors.append(path),
                workers=2, max_attempts=2,
            )

        self.assertEqual(len(reports), 1)
        self.assertEqual(errors, [])
        self.assertEqual(calls["n"], 2)


class TestSenRetry(unittest.TestCase):
    """sen 单份评分失败进入异常队列，重试后仍失败也不影响其它报告。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "result.xlsx"
        self.rules = [
            RuleItem(key="baseScore", default="25", weight=1.0, method="rule",
                     evaluation="", comment="规则评语"),
            RuleItem(key="event", default="0-15", weight=0.15, method="LLM",
                     evaluation="事件描述", comment=""),
        ]
        self.reports = [
            ParsedReport(
                course="云安全", klass="安全2401", student_id=sid, std_name="张三",
                report_name="课程报告", source_path=str(self.tmp / f"{sid}.md"),
                md_text="正文",
            )
            for sid in ("2021001", "2021002", "2021003")
        ]
        seed = {
            report.student_id: {
                "baseScore": ItemScore(key="baseScore", score=25.0, comment="规则评语"),
                "event": ItemScore(key="event", score=0.0, comment=""),
            }
            for report in self.reports
        }
        write_results(self.result_path, self.reports, self.rules, seed, mode="upsert")

    def tearDown(self):
        self._tmp.cleanup()

    def _grader(self) -> Grader:
        grader = Grader(AppConfig(
            llm=EndpointConfig(batch_size=2),
            paths=PathsConfig(
                report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
                report_name_separator="-",
                result_name_format="{course}-{tagDep}-{reportName}-评分结果.xlsx",
                parse_workers=2, retry_rounds=1,
            ),
        ))
        grader.result_path_for_test = self.result_path  # type: ignore[attr-defined]
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        return grader

    def test_transient_failure_is_retried(self):
        calls: dict[str, int] = {}

        def fake_grade(_client, report, _llm_rules):
            calls[report.student_id] = calls.get(report.student_id, 0) + 1
            if report.student_id == "2021002" and calls[report.student_id] == 1:
                raise RuntimeError("临时失败")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader = self._grader()
        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.grader.read_rules", return_value=self.rules):
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        items = read_result_items(self.result_path)
        for report in self.reports:
            self.assertEqual(items[report.student_id]["event"].score, 12.0)
        self.assertEqual(calls["2021002"], 2)
        self.assertFalse(any(entry.error for entry in grader.stage_logs))

    def test_persistent_failure_is_skipped(self):
        def fake_grade(_client, report, _llm_rules):
            if report.student_id == "2021002":
                raise RuntimeError("始终失败")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader = self._grader()
        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.grader.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        items = read_result_items(self.result_path)
        self.assertEqual(items["2021001"]["event"].score, 12.0)
        self.assertEqual(items["2021003"]["event"].score, 12.0)
        self.assertEqual(items["2021002"]["event"].score, 0.0)  # 未评分，保留原值
        failures = [entry for entry in grader.stage_logs
                    if entry.error and "始终失败" in entry.error]
        self.assertEqual(len(failures), 1)
        self.assertEqual(grader.stage_logs[-1].extra.get("评分失败"), "1 份")


if __name__ == "__main__":
    unittest.main()
