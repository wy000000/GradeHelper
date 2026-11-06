"""doc 阶段 md 清理测试：去除空行，以及被跳过图片不保留占位符。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper import batch as batch_module  # noqa: E402
from gradehelper.config import AppConfig, PathsConfig  # noqa: E402
from gradehelper.grader import Grader  # noqa: E402
from gradehelper.models import ItemScore, ParsedReport, ReportImage, RuleItem  # noqa: E402
from gradehelper.parser import remove_skipped_placeholders, strip_blank_lines  # noqa: E402

BASE = "云安全-安全2401-2021001-张三-课程报告"


def _full(index: int, ext: str = "png") -> str:
    """解析阶段写入 md 的完整占位符格式。"""
    return f"[[PIC:{BASE}-P{index}.{ext}]]"


def _grader() -> Grader:
    return Grader(AppConfig(paths=PathsConfig(
        report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
        report_name_separator="-",
        result_name_format="{course}-{tagDep}-评分结果-{reportName}.xlsx",
    )))


class TestRemoveSkippedPlaceholders(unittest.TestCase):
    def test_full_format_removes_skipped_keeps_rest(self):
        md = f"{_full(1)}\n正文\n{_full(2)}\n{_full(3)}"
        out = remove_skipped_placeholders(md, 2)
        self.assertNotIn(_full(1), out)
        self.assertNotIn(_full(2), out)
        self.assertIn(_full(3), out)
        self.assertIn("正文", out)

    def test_short_format(self):
        out = remove_skipped_placeholders("[[PIC:P1]]与[[PIC:P2]]与[[PIC:P3]]", 1)
        self.assertNotIn("[[PIC:P1]]", out)
        self.assertIn("[[PIC:P2]]", out)
        self.assertIn("[[PIC:P3]]", out)

    def test_zero_skip_keeps_every_placeholder(self):
        md = f"{_full(1)}{_full(2)}"
        self.assertEqual(remove_skipped_placeholders(md, 0), md)

    def test_two_digit_index_not_confused_with_single_digit(self):
        # 跳过 1 张时，P10 不应被当作 P1 删除
        md = _full(10)
        self.assertEqual(remove_skipped_placeholders(md, 1), md)

    def test_unparsable_placeholder_kept(self):
        md = "[[PIC:无编号占位符]]"
        self.assertEqual(remove_skipped_placeholders(md, 3), md)


class TestStripBlankLines(unittest.TestCase):
    def test_removes_blank_and_whitespace_lines(self):
        md = "第一段\n\n   \n第二段\n\n\n第三段\n"
        self.assertEqual(strip_blank_lines(md), "第一段\n第二段\n第三段\n")

    def test_keeps_table_and_list_lines(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n- 列表项"
        self.assertEqual(strip_blank_lines(md),
                         "| a | b |\n|---|---|\n| 1 | 2 |\n- 列表项\n")

    def test_all_blank_returns_empty(self):
        self.assertEqual(strip_blank_lines("\n\n  \n"), "")

    def test_no_blank_lines_only_adds_trailing_newline(self):
        self.assertEqual(strip_blank_lines("正文"), "正文\n")


class TestStageDocCleansMd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.result_path = self.tmp / "result.xlsx"

    def tearDown(self):
        self._tmp.cleanup()

    def test_doc_md_has_no_blank_lines_and_no_skipped_placeholders(self):
        md = (f"{_full(1)}\n\n{_full(2)}\n\n# 标题\n\n正文第一段\n\n\n"
              f"正文第二段\n\n{_full(3)}\n")
        report = ParsedReport(
            course="云安全",
            klass="安全2401",
            student_id="2021001",
            std_name="张三",
            report_name="课程报告",
            source_path=str(self.tmp / f"{BASE}.docx"),
            md_text=md,
            images=[ReportImage(index=i, ext="png", data=b"") for i in (1, 2, 3)],
        )
        rule = RuleItem(key="baseScore", default="25", weight=1.0, method="rule",
                        evaluation="", comment="报告已提交，+25分")
        scores = {"2021001": {"baseScore": ItemScore(key="baseScore", score=25.0,
                                                    comment="报告已提交，+25分")}}

        with patch("gradehelper.grader.parse_all", return_value=[report]), \
                patch("gradehelper.grader.read_rules", return_value=[rule]), \
                patch("gradehelper.grader.run_program_rules", return_value=(scores, [])):
            grader = _grader()
            grader.stage_doc(self.tmp, self.template, skip_images=2,
                             output=self.result_path)

        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("文档"), "处理 1 份 / 成功 1 份 / 失败 0 份")
        self.assertEqual(entry.extra.get("图片"), "解析 3 张 / 保留 1 张")
        self.assertEqual(entry.extra.get("跳过图片"), "前 2 张")
        self.assertEqual((entry.processed_count, entry.succeeded_count,
                          entry.failed_count), (1, 1, 0))

        saved = (self.tmp / f"{BASE}.md").read_text(encoding="utf-8")
        # 跳过图片（P1、P2）的占位符不写入 md，剩余图片（P3）占位符保留
        self.assertNotIn(_full(1), saved)
        self.assertNotIn(_full(2), saved)
        self.assertIn(_full(3), saved)
        # 落盘的 md 不含空行（含仅含空白字符的行）
        self.assertNotIn("\n\n", saved)
        self.assertFalse([line for line in saved.splitlines() if not line.strip()])
        # 规则评分使用的 md 与落盘内容一致
        self.assertEqual(report.md_text, saved)
        # 跳过图片不保存图片文件，剩余图片正常保存
        self.assertEqual([img.index for img in report.images], [3])
        self.assertFalse((self.tmp / f"{BASE}-P1.png").exists())
        self.assertTrue((self.tmp / f"{BASE}-P3.png").exists())


class TestLoadReportsCleansSkippedPlaceholders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_legacy_md_skipped_placeholders_removed_on_load(self):
        # 模拟旧版 doc 阶段产物：磁盘上的 md 仍残留被跳过图片的占位符
        (self.tmp / f"{BASE}.md").write_text(
            f"{_full(1)}\n正文\n{_full(3)}\n", encoding="utf-8")
        (self.tmp / f"{BASE}-P3.png").write_bytes(b"img")

        reports = _grader()._load_reports_for_pic(self.tmp, skip_images=2)

        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertNotIn(_full(1), report.md_text)
        self.assertIn(_full(3), report.md_text)
        self.assertEqual([img.index for img in report.images], [3])

    def test_load_progress_is_labeled_as_loading(self):
        """加载 md 的进度条带「加载MD文档」后缀，避免与同阶段后续评分进度混淆。"""
        (self.tmp / f"{BASE}.md").write_text(
            f"{_full(3)}\n正文\n", encoding="utf-8")
        (self.tmp / f"{BASE}-P3.png").write_bytes(b"img")

        labels: list[str] = []
        real = batch_module.ParallelProgress

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, stage, *args, **kwargs):
                labels.append(stage)
                super().__init__(stage, *args, **kwargs)

        with patch.object(batch_module, "ParallelProgress", Recording):
            _grader()._load_reports_for_pic(self.tmp, skip_images=0, stage="sen")

        self.assertEqual(labels, ["sen 加载MD文档"])


class TestLoadReportsWarnings(unittest.TestCase):
    """pic/sen 加载 md 时，缺失图片等中间解析错误写入 report.warnings。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_picture_is_recorded_as_warning(self):
        (self.tmp / f"{BASE}.md").write_text(
            f"{_full(3)}\n正文\n", encoding="utf-8")
        # 不创建 P3 图片文件

        with self.assertLogs("gradehelper.report_io", level="WARNING"):
            reports = _grader()._load_reports_for_pic(self.tmp, skip_images=0)

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].images, [])
        self.assertTrue(any("未找到图片文件" in w for w in reports[0].warnings))


class TestStageDocRecordsParseWarnings(unittest.TestCase):
    """doc 阶段把 report.warnings（如 docling 回退）写入阶段日志。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.result_path = self.tmp / "result.xlsx"

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_warnings_are_written_to_stage_log(self):
        report = ParsedReport(
            course="云安全", klass="安全2401", student_id="2021001", std_name="张三",
            report_name="课程报告", source_path=str(self.tmp / f"{BASE}.docx"),
            md_text="正文\n", warnings=["docling 解析失败，回退 python-docx: boom"],
        )
        rule = RuleItem(key="baseScore", default="25", weight=1.0, method="rule",
                        evaluation="", comment="报告已提交，+25分")
        scores = {"2021001": {"baseScore": ItemScore(key="baseScore", score=25.0,
                                                    comment="报告已提交，+25分")}}
        grader = _grader()
        with patch("gradehelper.grader.parse_all", return_value=[report]), \
                patch("gradehelper.grader.read_rules", return_value=[rule]), \
                patch("gradehelper.grader.run_program_rules", return_value=(scores, [])):
            grader.stage_doc(self.tmp, self.template, skip_images=2,
                             output=self.result_path)

        entry = grader.stage_logs[-1]
        self.assertTrue(any("docling 解析失败" in w for w in entry.warnings))


if __name__ == "__main__":
    unittest.main()
