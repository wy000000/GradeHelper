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

    def test_retry_callback_records_intermediate_failure(self):
        attempts: dict[str, int] = {}
        retries = []

        def worker(path: Path) -> str:
            attempts[path.name] = attempts.get(path.name, 0) + 1
            if path.name == "b.txt" and attempts[path.name] == 1:
                raise OSError("暂时失败")
            return path.name

        run_file_batch(
            stage="doc", items=[Path("a.txt"), Path("b.txt")], worker=worker,
            workers=1, path_of=lambda path: path, unit="份", max_attempts=2,
            progress_enabled=False, on_retry=retries.append,
        )

        self.assertEqual([f.path.name for f in retries], ["b.txt"])
        self.assertEqual(retries[0].attempts, 1)
        self.assertIn("暂时失败", retries[0].reason)

    def test_retry_callback_not_called_without_retry(self):
        retries = []

        def worker(path: Path) -> str:
            raise RuntimeError("始终失败")

        with self.assertLogs("gradehelper.batch", level="ERROR"):
            run_file_batch(
                stage="doc", items=[Path("bad.txt")], worker=worker, workers=1,
                path_of=lambda path: path, unit="份", max_attempts=1,
                progress_enabled=False, on_retry=retries.append,
            )

        self.assertEqual(retries, [])

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

    def _grader(self, retry_rounds: int = 1) -> Grader:
        grader = Grader(AppConfig(
            llm=EndpointConfig(batch_size=2),
            paths=PathsConfig(
                report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
                report_name_separator="-",
                result_name_format="{course}-{tagDep}-{reportName}-评分结果.xlsx",
                parse_workers=2, retry_rounds=retry_rounds,
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
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("重试成功"), "1 份（2021002.md）")
        # 中间错误仍写入日志（每轮一条，不省略）
        self.assertEqual(len(entry.warnings), 1)
        self.assertIn("2021002.md", entry.warnings[0])
        self.assertIn("第 1 次评分失败", entry.warnings[0])
        self.assertEqual(entry.extra.get("报告"), "处理 3 份 / 成功 3 份 / 失败 0 份")

    def test_retry_failure_not_double_counted(self):
        """同一文档多轮重试失败只算一条；重试成功则从失败数中移除。"""
        calls: dict[str, int] = {}

        def fake_grade(_client, report, _llm_rules):
            calls[report.student_id] = calls.get(report.student_id, 0) + 1
            if report.student_id == "2021002" and calls[report.student_id] <= 2:
                raise RuntimeError("临时失败")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader = self._grader(retry_rounds=2)  # 共 3 次尝试
        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.grader.read_rules", return_value=self.rules):
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        self.assertEqual(calls["2021002"], 3)
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.failed_count, 0)
        self.assertEqual(entry.extra.get("重试成功"), "1 份（2021002.md）")
        # 中间错误每轮都记入日志：不再用最新一条覆盖旧记录
        self.assertEqual(len(entry.warnings), 2)
        self.assertIn("第 1 次评分失败", entry.warnings[0])
        self.assertIn("第 2 次评分失败", entry.warnings[1])
        self.assertFalse(any(item.error for item in grader.stage_logs))
        self.assertNotIn("失败文档", entry.extra)

    def test_persistent_failure_is_skipped(self):
        def fake_grade(_client, report, _llm_rules):
            if report.student_id == "2021002":
                raise RuntimeError("始终失败")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader = self._grader(retry_rounds=2)  # 共 3 次尝试
        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.grader.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        items = read_result_items(self.result_path)
        self.assertEqual(items["2021001"]["event"].score, 12.0)
        self.assertEqual(items["2021003"]["event"].score, 12.0)
        self.assertEqual(items["2021002"]["event"].score, 0.0)  # 未评分，保留原值
        # 中途不再为失败文档单独写记录：文档名只出现在总结的「失败文档」与按时序条目里
        failures = [entry for entry in grader.stage_logs
                    if entry.error and "始终失败" in entry.error]
        self.assertEqual(len(failures), 0)
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("评分失败"), "1 份")
        self.assertEqual(entry.extra.get("失败文档"), "2021002.md")
        # 中间错误仍记入日志（最终失败的文档也保留每轮重试记录，不省略）
        self.assertEqual(len(entry.warnings), 2)
        self.assertIn("第 1 次评分失败", entry.warnings[0])
        self.assertIn("第 2 次评分失败", entry.warnings[1])

    def test_log_records_config_events_then_summary(self):
        """sen 日志：开始配置 → 按时序出错信息（含文档名）→ 总结；控制台输出同样总结。"""
        def fake_grade(_client, report, _llm_rules):
            if report.student_id == "2021002":
                raise RuntimeError("始终失败")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader = self._grader(retry_rounds=1)  # 共 2 次尝试
        grader._grade_with_llm_batch = fake_grade
        log_path = self.tmp / "sen.log"
        grader._log_path = log_path
        with patch("gradehelper.grader.read_rules", return_value=self.rules), \
                self.assertLogs(level="INFO") as captured:
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        text = log_path.read_text(encoding="utf-8")
        start_pos = text.find("状态: 开始")
        event_pos = text.find("级别: 警告")
        summary_pos = text.find("用时: ")
        self.assertGreater(start_pos, 0)
        self.assertGreater(event_pos, start_pos)    # 配置信息在最前
        self.assertGreater(summary_pos, event_pos)  # 总结在最后
        # 开始记录包含配置信息
        self.assertIn("报告目录: ", text[:event_pos])
        self.assertIn("base_url: ", text[:event_pos])
        self.assertIn("并发: ", text[:event_pos])
        # 按时序的中间条目保留文档名与出错信息（重试警告 + 最终错误各一条）
        middle = text[event_pos:summary_pos]
        self.assertIn("2021002.md: 第 1 次评分失败", middle)
        self.assertIn("级别: 错误", middle)
        self.assertIn("2021002.md: 评分失败（重试后仍失败，已跳过）: 始终失败", middle)
        # 中途不再写单独的失败记录；总结只保留中间错误条数与最终失败文档名
        self.assertNotIn("状态: 失败 - 始终失败", text)
        self.assertIn("中间错误: 1 条", text[summary_pos:])
        self.assertNotIn("  - 2021002.md: 第 1 次评分失败", text[summary_pos:])
        self.assertIn("失败文档: 2021002.md", text[summary_pos:])
        # 控制台最后输出与 log 相同的总结信息
        console = "\n".join(captured.output)
        self.assertIn("中间错误: 1 条", console)
        self.assertIn("失败文档: 2021002.md", console)

    def test_console_summary_printed_when_stage_aborts(self):
        """阶段中途失败（如写表失败）时控制台仍输出份数与失败文档全名。"""
        def fake_grade(_client, report, _llm_rules):
            if report.student_id == "2021002":
                raise RuntimeError("始终失败")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader = self._grader(retry_rounds=0)  # 只尝试 1 轮
        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.grader.read_rules", return_value=self.rules), \
                patch("gradehelper.grader.write_results_atomic",
                      side_effect=RuntimeError("磁盘已满")), \
                self.assertLogs("gradehelper.grader", level="ERROR") as logs, \
                self.assertRaises(RuntimeError):
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        console = "\n".join(logs.output)
        self.assertIn("处理 3 份文档：成功 2 份，失败 1 份", console)
        self.assertIn("失败文档：2021002.md", console)


if __name__ == "__main__":
    unittest.main()
