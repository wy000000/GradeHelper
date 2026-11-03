"""doc / sen 阶段开始前的结果文件占用检查测试。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import AppConfig, PathsConfig  # noqa: E402
from gradehelper.grader import Grader  # noqa: E402

BASE = "云安全-安全2401-2021001-张三-课程报告"
RESULT_NAME = "云安全-系部专业-课程报告-评分结果.xlsx"


def _grader(output: str = "") -> Grader:
    return Grader(AppConfig(paths=PathsConfig(
        report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
        report_name_separator="-",
        result_name_format="{course}-{tagDep}-{reportName}-评分结果.xlsx",
        output=output,
    )))


def _locked_open(path: Path):
    """模拟文件被其它程序（Excel）独占：指定路径的 open() 抛 PermissionError。"""
    real_open = pathlib.Path.open

    def fake_open(self, *args, **kwargs):
        if Path(self) == path:
            raise PermissionError(13, "另一个程序正在使用此文件，进程无法访问。", str(self))
        return real_open(self, *args, **kwargs)

    return patch.object(pathlib.Path, "open", fake_open)


class TestEnsureResultWritable(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_skips_check(self):
        Grader._ensure_result_writable(None)

    def test_missing_file_is_writable(self):
        # 首次运行：结果文件还不存在，视为可写
        Grader._ensure_result_writable(self.tmp / "result.xlsx")

    def test_existing_file_is_writable(self):
        path = self.tmp / "result.xlsx"
        path.write_bytes(b"x")
        Grader._ensure_result_writable(path)

    def test_locked_file_raises(self):
        path = self.tmp / "result.xlsx"
        path.write_bytes(b"x")
        with _locked_open(path):
            with self.assertRaises(RuntimeError) as ctx:
                Grader._ensure_result_writable(path)
        message = str(ctx.exception)
        self.assertIn(str(path), message)
        self.assertIn("Excel", message)


class TestResolveResultPathHint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    def test_explicit_output_wins(self):
        out = self.tmp / "custom.xlsx"
        hint = _grader()._resolve_result_path_hint(self.template, self.tmp, out)
        self.assertEqual(hint, out)

    def test_config_output_used_when_cli_missing(self):
        out = self.tmp / "from-config.xlsx"
        hint = _grader(output=str(out))._resolve_result_path_hint(self.template, self.tmp)
        self.assertEqual(hint, out)

    def test_from_docx_filename(self):
        (self.tmp / f"{BASE}.docx").write_bytes(b"docx")
        hint = _grader()._resolve_result_path_hint(self.template, self.tmp)
        self.assertEqual(hint, self.tmp / RESULT_NAME)

    def test_falls_back_to_md_when_no_docx(self):
        # 只跑 sen 阶段时目录里可能只有 md
        (self.tmp / f"{BASE}.md").write_text("正文", encoding="utf-8")
        hint = _grader()._resolve_result_path_hint(self.template, self.tmp)
        self.assertEqual(hint, self.tmp / RESULT_NAME)

    def test_none_without_reports(self):
        hint = _grader()._resolve_result_path_hint(self.template, self.tmp)
        self.assertIsNone(hint)

    def test_none_without_result_name_format(self):
        (self.tmp / f"{BASE}.docx").write_bytes(b"docx")
        grader = Grader(AppConfig(paths=PathsConfig(
            report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
            report_name_separator="-",
        )))
        self.assertIsNone(grader._resolve_result_path_hint(self.template, self.tmp))


class TestStagesAbortWhenResultLocked(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        (self.tmp / f"{BASE}.docx").write_bytes(b"docx")
        self.result_path = self.tmp / RESULT_NAME
        self.result_path.write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_doc_stage_aborts_before_parse(self):
        with _locked_open(self.result_path), \
                patch("gradehelper.grader.parse_all") as parse_all:
            with self.assertRaises(RuntimeError) as ctx:
                _grader().stage_doc(self.tmp, self.template)
        self.assertIn("结果文件无法写入", str(ctx.exception))
        parse_all.assert_not_called()
        # 不落盘解析产物
        self.assertFalse((self.tmp / f"{BASE}.md").exists())

    def test_sen_stage_aborts_before_read_rules(self):
        with _locked_open(self.result_path), \
                patch("gradehelper.grader.read_rules") as read_rules:
            with self.assertRaises(RuntimeError) as ctx:
                _grader().stage_sen(self.tmp, self.template)
        self.assertIn("结果文件无法写入", str(ctx.exception))
        read_rules.assert_not_called()

    def test_doc_stage_runs_when_result_not_locked(self):
        # 结果文件存在但未被占用时，检查通过，继续正常流程
        with patch("gradehelper.grader.parse_all", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                _grader().stage_doc(self.tmp, self.template)
        # 走到“目录中没有符合命名格式的报告”说明未在检查处退出
        self.assertIn("没有符合命名格式的报告", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
