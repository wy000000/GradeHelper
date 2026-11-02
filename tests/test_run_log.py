"""增量运行日志测试：标题格式、阶段落盘、历史保留、失败状态。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import AppConfig, PathsConfig  # noqa: E402
from gradehelper.grader import Grader, StageLogEntry  # noqa: E402
from gradehelper.models import ParsedReport  # noqa: E402


def _report(course: str = "云安全", student_id: str = "2021001",
            report_name: str = "课程报告", tag_dep: str = "计算机系") -> ParsedReport:
    return ParsedReport(
        course=course,
        klass="安全2401",
        student_id=student_id,
        std_name="张三",
        report_name=report_name,
        source_path=f"{student_id}.docx",
        md_text="正文",
        tag_dep=tag_dep,
    )


class TestStageLogEntry(unittest.TestCase):
    def test_title_format(self):
        entry = StageLogEntry(
            stage="doc",
            start_time="2026-11-03 10:00:00",
            end_time="2026-11-03 10:00:05",
            duration_seconds=5.0,
            processed_count=1,
            start_student_id="2021001",
            end_student_id="2021001",
            course="云安全",
            tag_dep="计算机系",
            report_name="课程报告",
        )
        self.assertEqual(entry.title, "云安全-计算机系-课程报告-doc")

    def test_title_fallback(self):
        entry = StageLogEntry(
            stage="sen",
            start_time="2026-11-03 10:00:00",
            end_time="2026-11-03 10:00:05",
            duration_seconds=5.0,
            processed_count=0,
            start_student_id=None,
            end_student_id=None,
        )
        self.assertEqual(entry.title, "未知课程-系部专业-未知报告-sen")


class TestBuildStageEntry(unittest.TestCase):
    def setUp(self):
        self.grader = Grader(AppConfig(paths=PathsConfig()))
        self.start = datetime(2026, 11, 3, 10, 0, 0)
        self.end = datetime(2026, 11, 3, 10, 1, 0)

    def test_uses_first_report_and_batch_note(self):
        reports = [
            _report(course="云安全", student_id="2021001"),
            _report(course="网络攻防", student_id="2021002"),
            _report(course="云安全", student_id="2021003", report_name="期中报告"),
        ]
        entry = self.grader._build_stage_entry(
            stage="pic", reports=reports, tag_dep="计算机系",
            start_dt=self.start, end_dt=self.end, elapsed=60.0, error=None,
            extra={"图片总数": "3 张"},
        )
        self.assertEqual(entry.title, "云安全-计算机系-课程报告-pic")
        self.assertEqual(entry.start_student_id, "2021001")
        self.assertEqual(entry.end_student_id, "2021003")
        self.assertEqual(entry.extra["批次"], "2 个课程 / 2 种报告")
        self.assertEqual(entry.extra["图片总数"], "3 张")

    def test_no_reports_uses_cli_tag_dep(self):
        entry = self.grader._build_stage_entry(
            stage="doc", reports=[], tag_dep="外语系",
            start_dt=self.start, end_dt=self.end, elapsed=1.0,
            error="目录为空",
        )
        self.assertEqual(entry.title, "未知课程-外语系-未知报告-doc")
        self.assertEqual(entry.error, "目录为空")


class TestAppendRunLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log_path = self.tmp / "run.log"
        self.grader = Grader(AppConfig(paths=PathsConfig(log_path=str(self.log_path))))
        self.grader._log_path = self.log_path
        self.start = datetime(2026, 11, 3, 10, 0, 0)
        self.end = datetime(2026, 11, 3, 10, 1, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def _read(self) -> str:
        return self.log_path.read_text(encoding="utf-8")

    def _entry(self, stage: str, sid: str, error: str | None = None) -> StageLogEntry:
        return self.grader._build_stage_entry(
            stage=stage, reports=[_report(student_id=sid)], tag_dep="计算机系",
            start_dt=self.start, end_dt=self.end, elapsed=60.0, error=error,
        )

    def test_stage_is_appended_immediately_and_history_kept(self):
        self.log_path.write_text("历史记录\n", encoding="utf-8")

        self.grader._append_stage_log(self._entry("doc", "2021001"))
        first = self._read()
        self.assertTrue(first.startswith("历史记录\n"))
        self.assertIn("云安全-计算机系-课程报告-doc", first)
        self.assertIn("记录时间:", first)
        self.assertIn("状态: 成功", first)
        self.assertTrue(self.grader.stage_logs[0].record_time)

        # 第二个阶段结束后再追加，不覆盖前面的内容
        self.grader._append_stage_log(self._entry("pic", "2021002"))
        second = self._read()
        self.assertTrue(second.startswith(first))
        self.assertIn("云安全-计算机系-课程报告-pic", second)
        self.assertLess(second.index("-doc"), second.index("-pic"))

    def test_failed_stage_status(self):
        self.grader._append_stage_log(self._entry("sen", "2021001", error="连接中断"))
        text = self._read()
        self.assertIn("状态: 失败 - 连接中断", text)

    def test_run_header_and_footer(self):
        self.grader._append_run_header(
            stage="full", reports_dir=self.tmp / "reports",
            template=self.tmp / "模板.xlsx", output=None,
        )
        self.grader._append_stage_log(self._entry("doc", "2021001"))
        self.grader._append_run_footer(
            start_dt=self.start, elapsed=125.5, error=None,
        )
        text = self._read()
        self.assertIn("GradeHelper 运行日志", text)
        self.assertIn("记录时间:", text)
        self.assertIn("执行阶段: full", text)
        self.assertIn(f"报告目录: {self.tmp / 'reports'}", text)
        self.assertIn("总用时: 125.50 秒", text)
        self.assertIn("状态: 成功", text)
        self.assertLess(text.index("运行开始:"), text.index("-doc"))
        self.assertLess(text.index("-doc"), text.index("运行结束:"))

    def test_run_footer_reports_failure(self):
        self.grader._append_run_footer(
            start_dt=self.start, elapsed=1.0, error="KeyboardInterrupt",
        )
        text = self._read()
        self.assertIn("状态: 失败/中断 - KeyboardInterrupt", text)

    def test_log_write_failure_is_best_effort(self):
        self.grader._log_path = self.tmp  # 目录不是文件，open(..., "a") 会失败
        self.grader._append_stage_log(self._entry("doc", "2021001"))
        self.assertTrue(self.grader._log_failed)
        self.assertEqual(len(self.grader.stage_logs), 1)  # 内存记录仍然保留

    def test_run_appends_header_stage_and_footer(self):
        self.log_path.write_text("历史记录\n", encoding="utf-8")
        calls: list[str] = []

        def fake_doc(*args, **kwargs):
            calls.append("doc")
            self.grader._append_stage_log(self._entry("doc", "2021001"))
            return self.tmp / "result.xlsx"

        def fake_pic(*args, **kwargs):
            calls.append("pic")
            self.grader._append_stage_log(self._entry("pic", "2021002"))

        def fake_sen(*args, **kwargs):
            calls.append("sen")
            self.grader._append_stage_log(self._entry("sen", "2021003"))
            return self.tmp / "result.xlsx"

        self.grader.stage_doc = fake_doc  # type: ignore[method-assign]
        self.grader.stage_pic = fake_pic  # type: ignore[method-assign]
        self.grader.stage_sen = fake_sen  # type: ignore[method-assign]

        template = self.tmp / "模板.xlsx"
        self.grader.run(reports_dir=str(self.tmp / "reports"),
                        template=str(template), stage="full")

        text = self._read()
        self.assertEqual(calls, ["doc", "pic", "sen"])
        self.assertTrue(text.startswith("历史记录\n"))
        self.assertIn("执行阶段: full", text)
        self.assertLess(text.index("运行开始:"), text.index("-doc"))
        self.assertLess(text.index("-doc"), text.index("-pic"))
        self.assertLess(text.index("-pic"), text.index("-sen"))
        self.assertLess(text.index("-sen"), text.index("运行结束:"))
        self.assertEqual(text.count("GradeHelper 运行日志"), 1)

    def test_item_failure_title_uses_specific_document(self):
        self.grader._append_failure_entry(
            stage="pic", error="timeout", report=_report(student_id="2021009"),
            extra={"图片": "P2"},
        )
        text = self._read()
        self.assertIn("云安全-计算机系-云安全-安全2401-2021009-张三-课程报告-pic", text)
        self.assertIn("图片: P2", text)
        self.assertIn("状态: 失败 - timeout", text)

    def test_document_parse_failure_has_course_and_filename(self):
        document = "云安全-安全2401-2021009-李四-课程报告.docx"
        self.grader._append_failure_entry(
            stage="doc", error="文档损坏", document=document,
            course="云安全", tag_dep="计算机系", report_name="课程报告",
            student_id="2021009",
        )
        text = self._read()
        self.assertIn(f"云安全-计算机系-{document}-doc", text)
        self.assertIn("开始学号: 2021009", text)
        self.assertIn("状态: 失败 - 文档损坏", text)

    @staticmethod
    def _run_block(record_time: datetime, marker: str, pad: int = 0,
                   run_start: datetime | None = None) -> str:
        stamp = record_time.strftime("%Y-%m-%d %H:%M:%S")
        start_stamp = (run_start or record_time).strftime("%Y-%m-%d %H:%M:%S")
        return (
            "=" * 60 + "\n"
            "GradeHelper 运行日志\n"
            f"记录时间: {stamp}\n"
            f"运行开始: {start_stamp}\n"
            f"标记: {marker}\n"
            + "x" * pad + "\n"
            + "=" * 60 + "\n\n"
        )

    def test_trim_removes_records_older_than_retention_when_log_large(self):
        now = datetime.now()
        old = self._run_block(now - timedelta(days=2), "OLD", pad=40_000)
        recent = self._run_block(now - timedelta(hours=1), "RECENT")
        self.log_path.write_text(old + recent, encoding="utf-8")
        self.assertGreater(self.log_path.stat().st_size, 32 * 1024)

        self.grader._trim_log()

        text = self._read()
        self.assertNotIn("标记: OLD", text)
        self.assertIn("标记: RECENT", text)

    def test_trim_keeps_old_records_when_log_small(self):
        now = datetime.now()
        old = self._run_block(now - timedelta(days=2), "OLD")
        recent = self._run_block(now - timedelta(hours=1), "RECENT")
        self.log_path.write_text(old + recent, encoding="utf-8")
        self.assertLess(self.log_path.stat().st_size, 32 * 1024)

        self.grader._trim_log()

        text = self._read()
        self.assertIn("标记: OLD", text)
        self.assertIn("标记: RECENT", text)

    def test_trim_uses_record_time_not_run_start(self):
        now = datetime.now()
        # 记录时间很旧，但运行开始时间很新：应按记录时间删除
        old_record = self._run_block(now - timedelta(days=2), "OLD", pad=40_000,
                                     run_start=now)
        # 记录时间很新，但运行开始时间很旧：应按记录时间保留
        recent_record = self._run_block(now - timedelta(hours=1), "RECENT",
                                        run_start=now - timedelta(days=2))
        self.log_path.write_text(old_record + recent_record, encoding="utf-8")

        self.grader._trim_log()

        text = self._read()
        self.assertNotIn("标记: OLD", text)
        self.assertIn("标记: RECENT", text)

    def test_trim_drops_old_format_without_record_time(self):
        now = datetime.now()
        old_record = (
            "=" * 60 + "\n"
            "GradeHelper 运行日志\n"
            "生成时间: 2026-11-01 10:00:00\n"
            "标记: OLD_FORMAT\n"
            + "x" * 40_000 + "\n"
            + "=" * 60 + "\n\n"
        )
        recent_record = self._run_block(now - timedelta(hours=1), "RECENT")
        self.log_path.write_text(old_record + recent_record, encoding="utf-8")

        self.grader._trim_log()

        text = self._read()
        self.assertNotIn("标记: OLD_FORMAT", text)
        self.assertIn("标记: RECENT", text)

    def test_trim_uses_configured_size_and_retention(self):
        now = datetime.now()
        old = self._run_block(now - timedelta(hours=2), "OLD", pad=40_000)
        recent = self._run_block(now - timedelta(minutes=5), "RECENT")
        self.log_path.write_text(old + recent, encoding="utf-8")

        # 把阈值提高到 100 KB：当前日志不够大，不清理
        self.grader.config.paths.log_max_size_kb = 100
        self.grader._trim_log()
        self.assertIn("标记: OLD", self._read())

        # 阈值恢复 32 KB，但保留期改为 1 小时：2 小时前的记录被清理
        self.grader.config.paths.log_max_size_kb = 32
        self.grader.config.paths.log_retention_days = 1 / 24
        self.grader._trim_log()
        text = self._read()
        self.assertNotIn("标记: OLD", text)
        self.assertIn("标记: RECENT", text)

    def test_trim_removes_individual_old_stage_record(self):
        now = datetime.now()
        old_stamp = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        recent_stamp = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        old_record = (
            "旧文档-doc\n"
            f"记录时间: {old_stamp}\n"
            "状态: 成功\n"
            + "x" * 40_000 + "\n"
            + "-" * 60 + "\n\n"
        )
        recent_record = (
            "新文档-pic\n"
            f"记录时间: {recent_stamp}\n"
            "状态: 成功\n"
            + "-" * 60 + "\n\n"
        )
        self.log_path.write_text(old_record + recent_record, encoding="utf-8")

        self.grader._trim_log()

        text = self._read()
        self.assertNotIn("旧文档-doc", text)
        self.assertIn("新文档-pic", text)

    def test_run_trims_before_appending_new_header(self):
        now = datetime.now()
        old = self._run_block(now - timedelta(days=2), "OLD", pad=40_000)
        recent = self._run_block(now - timedelta(hours=1), "RECENT")
        self.log_path.write_text(old + recent, encoding="utf-8")

        self.grader.stage_doc = lambda *args, **kwargs: self.tmp / "result.xlsx"  # type: ignore[method-assign]
        self.grader.run(reports_dir=str(self.tmp / "reports"),
                        template=str(self.tmp / "模板.xlsx"), stage="doc")

        text = self._read()
        self.assertNotIn("标记: OLD", text)
        self.assertIn("标记: RECENT", text)
        self.assertEqual(text.count("GradeHelper 运行日志"), 2)  # recent + 本次运行


if __name__ == "__main__":
    unittest.main()
