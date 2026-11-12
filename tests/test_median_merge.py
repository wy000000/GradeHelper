"""median 子命令测试：文件匹配、总分解析、中位数规则、学号一致性、日志与 CLI。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook, load_workbook  # noqa: E402

from gradehelper.__main__ import build_parser, main  # noqa: E402
from gradehelper.excel_io import write_results  # noqa: E402
from gradehelper.median_merge import (  # noqa: E402
    MedianError, find_inputs, read_result_table, run_median,
)
from gradehelper.models import ItemScore, ParsedReport, RuleItem  # noqa: E402

BASE = "云安全-系部专业-云安全课程报告-评分结果"
STUDENTS = [("2021001", "张三"), ("2021002", "李四")]


def _report(sid: str, name: str, klass: str = "安全2401") -> ParsedReport:
    return ParsedReport(course="云安全", klass=klass, student_id=sid, std_name=name,
                        report_name="课程报告", source_path=f"{sid}.docx",
                        md_text="正文")


def _rules() -> list[RuleItem]:
    return [RuleItem(key="baseScore", default="25", weight=1.0, method="code",
                     evaluation="", comment="")]


def _write_result(path: Path, students: list[tuple[str, str]],
                  score: float, marker: str) -> None:
    """写一份评分结果（总分为无缓存公式，评语作来源标记）。"""
    reports = [_report(sid, name) for sid, name in students]
    scores = {sid: {"baseScore": ItemScore(key="baseScore", score=score,
                                           comment=marker)}
              for sid, _ in students}
    write_results(path, reports, _rules(), scores, mode="upsert")


def _write_manual(path: Path, headers: list, rows: list) -> None:
    """直接写一个 result 表（数值总分/自定义列序，不经 write_results）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "result"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


class _MedianTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _out(self) -> Path:
        return self.tmp / f"{BASE}-median.xlsx"

    def _log_text(self) -> str:
        return (self.tmp / f"{BASE}-median.log").read_text(encoding="utf-8")


class TestFindInputs(_MedianTestCase):
    def test_matches_only_dash_n_and_sorts_numerically(self):
        for n in (1, 2, 10):
            (self.tmp / f"{BASE}-{n}.xlsx").write_bytes(b"x")
        (self.tmp / f"{BASE}.xlsx").write_bytes(b"x")
        (self.tmp / f"{BASE}-median.xlsx").write_bytes(b"x")
        (self.tmp / "无关文件-1.xlsx").write_bytes(b"x")

        matched, ignored = find_inputs(BASE, self.tmp)
        self.assertEqual([n for n, _ in matched], [1, 2, 10])
        self.assertIn(f"{BASE}.xlsx", ignored)
        self.assertIn(f"{BASE}-median.xlsx", ignored)
        self.assertNotIn("无关文件-1.xlsx", ignored)

    def test_no_match_returns_empty(self):
        matched, ignored = find_inputs(BASE, self.tmp)
        self.assertEqual(matched, [])
        self.assertEqual(ignored, [])


class TestReadStructure(_MedianTestCase):
    def test_formula_without_cache_resolved(self):
        path = self.tmp / f"{BASE}-1.xlsx"
        _write_result(path, STUDENTS, score=70.0, marker="第1次")
        table = read_result_table(path)
        self.assertAlmostEqual(table.rows["2021001"].total, 70.0)
        self.assertEqual(table.rows["2021001"].std_name, "张三")
        self.assertTrue(str(table.rows["2021001"].formula).startswith("="))

    def test_numeric_total_used(self):
        headers = ["学号", "姓名", "班级", "总分", "baseScore评分", "baseScore评语"]
        path = self.tmp / f"{BASE}-1.xlsx"
        _write_manual(path, headers,
                      [["2021001", "张三", "安全2401", 70, 70, "第1次"]])
        table = read_result_table(path)
        self.assertAlmostEqual(table.rows["2021001"].total, 70.0)
        self.assertIsNone(table.rows["2021001"].formula)

    def test_header_order_tolerance(self):
        headers = ["总分", "学号", "姓名", "班级", "baseScore评分", "baseScore评语"]
        path = self.tmp / f"{BASE}-1.xlsx"
        _write_manual(path, headers, [[66, "2021001", "张三", "安全2401", 66, "x"]])
        table = read_result_table(path)
        self.assertEqual(table.rows["2021001"].std_name, "张三")
        self.assertAlmostEqual(table.rows["2021001"].total, 66.0)

    def test_missing_result_sheet_raises(self):
        wb = Workbook()
        wb.active.title = "data"
        path = self.tmp / f"{BASE}-1.xlsx"
        wb.save(path)
        with self.assertRaisesRegex(MedianError, "缺少 'result' sheet"):
            read_result_table(path)

    def test_missing_required_column_raises(self):
        path = self.tmp / f"{BASE}-1.xlsx"
        _write_manual(path, ["姓名", "班级", "总分"], [["张三", "安全2401", 70]])
        with self.assertRaisesRegex(MedianError, "缺少「学号」列"):
            read_result_table(path)

    def test_duplicate_sid_raises(self):
        path = self.tmp / f"{BASE}-1.xlsx"
        _write_manual(path, ["学号", "姓名", "班级", "总分"],
                      [["2021001", "张三", "安全2401", 70],
                       ["2021001", "张三", "安全2401", 80]])
        with self.assertRaisesRegex(MedianError, "学号重复"):
            read_result_table(path)


class TestMedianRules(_MedianTestCase):
    def test_odd_count_takes_middle_row(self):
        samples = [(70, "第1次"), (80, "第2次"), (90, "第3次")]
        for n, (score, marker) in enumerate(samples, start=1):
            _write_result(self.tmp / f"{BASE}-{n}.xlsx", STUDENTS, score, marker)
        out = run_median(BASE, directory=self.tmp)
        self.assertEqual(out, self._out())
        table = read_result_table(out)
        for sid, _name in STUDENTS:
            self.assertAlmostEqual(table.rows[sid].total, 80.0)
            self.assertEqual(table.rows[sid].values[5], "第2次")  # 整行复制来源

    def test_even_count_takes_higher(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", STUDENTS, 70, "第1次")
        _write_result(self.tmp / f"{BASE}-2.xlsx", STUDENTS, 90, "第2次")
        run_median(BASE, directory=self.tmp)
        table = read_result_table(self._out())
        for sid, _name in STUDENTS:
            self.assertAlmostEqual(table.rows[sid].total, 90.0)
            self.assertEqual(table.rows[sid].values[5], "第2次")

    def test_tie_uses_smallest_n(self):
        samples = [(80, "第1次"), (80, "第2次"), (60, "第3次")]
        for n, (score, marker) in enumerate(samples, start=1):
            _write_result(self.tmp / f"{BASE}-{n}.xlsx", STUDENTS, score, marker)
        run_median(BASE, directory=self.tmp)
        table = read_result_table(self._out())
        for sid, _name in STUDENTS:
            self.assertAlmostEqual(table.rows[sid].total, 80.0)
            self.assertEqual(table.rows[sid].values[5], "第1次")

    def test_output_keeps_total_formula(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", STUDENTS, 70, "第1次")
        run_median(BASE, directory=self.tmp)
        ws = load_workbook(self._out(), data_only=False)["result"]
        self.assertEqual(ws.cell(row=2, column=4).value, "=E2*1.0")

    def test_numeric_totals_end_to_end(self):
        headers = ["学号", "姓名", "班级", "总分", "baseScore评分", "baseScore评语"]
        _write_manual(self.tmp / f"{BASE}-1.xlsx", headers,
                      [["2021001", "张三", "安全2401", 70, 70, "第1次"],
                       ["2021002", "李四", "安全2401", 30, 30, "第1次"]])
        _write_manual(self.tmp / f"{BASE}-2.xlsx", headers,
                      [["2021001", "张三", "安全2401", 90, 90, "第2次"],
                       ["2021002", "李四", "安全2401", 50, 50, "第2次"]])
        run_median(BASE, directory=self.tmp)
        table = read_result_table(self._out())
        self.assertAlmostEqual(table.rows["2021001"].total, 90.0)
        self.assertAlmostEqual(table.rows["2021002"].total, 50.0)
        ws = load_workbook(self._out(), data_only=False)["result"]
        self.assertEqual(ws.cell(row=2, column=4).value, 90)  # 数值总分写常量


class TestConsistency(_MedianTestCase):
    def test_sid_mismatch_raises_lists_triplets_no_output(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", STUDENTS, 70, "第1次")
        _write_result(self.tmp / f"{BASE}-2.xlsx",
                      [("2021001", "张三"), ("2021003", "王五")], 80, "第2次")
        with self.assertRaises(MedianError) as ctx:
            run_median(BASE, directory=self.tmp)
        msg = str(ctx.exception)
        self.assertIn("学号集合不一致", msg)
        self.assertIn("2021002-李四-安全2401", msg)
        self.assertIn("2021003-王五-安全2401", msg)
        self.assertIn(f"{BASE}-2.xlsx", msg)
        self.assertIn(f"{BASE}-1.xlsx", msg)
        self.assertIn("共 2 条异常", msg)
        self.assertFalse(self._out().exists())  # 不再后续处理
        log = self._log_text()
        self.assertIn("学号集合不一致", log)
        self.assertIn("共 2 条异常", log)

    def test_identity_mismatch_raises(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", [("2021001", "张三")], 70, "第1次")
        _write_result(self.tmp / f"{BASE}-2.xlsx", [("2021001", "张四")], 80, "第2次")
        with self.assertRaises(MedianError) as ctx:
            run_median(BASE, directory=self.tmp)
        msg = str(ctx.exception)
        self.assertIn("姓名/班级跨文件不一致", msg)
        self.assertIn("张四", msg)
        self.assertFalse(self._out().exists())


class TestOutputAndLog(_MedianTestCase):
    def test_default_names_and_rerun_ignores_own_output(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", STUDENTS, 70, "第1次")
        _write_result(self.tmp / f"{BASE}-2.xlsx", STUDENTS, 90, "第2次")
        out = run_median(BASE, directory=self.tmp)
        self.assertEqual(out.name, f"{BASE}-median.xlsx")
        self.assertTrue((self.tmp / f"{BASE}-median.log").is_file())
        run_median(BASE, directory=self.tmp)  # 再次运行：输出与日志均不带 -n，被忽略
        log = self._log_text()
        self.assertEqual(log.count("median完成"), 2)
        self.assertIn("忽略", log)

    def test_no_input_files_raises(self):
        with self.assertRaisesRegex(MedianError, "未找到匹配"):
            run_median(BASE, directory=self.tmp)
        self.assertFalse(self._out().exists())
        self.assertIn("未找到匹配", self._log_text())


class TestCli(_MedianTestCase):
    def test_median_subcommand_parses(self):
        args = build_parser().parse_args(["median", BASE])
        self.assertEqual(args.command, "median")
        self.assertEqual(args.base, BASE)
        self.assertIsNone(args.dir)
        self.assertIsNone(args.output)
        args = build_parser().parse_args(
            ["median", f"{BASE}.xlsx", "--dir", "D:\\data", "--output", "out.xlsx"])
        self.assertEqual(args.base, f"{BASE}.xlsx")
        self.assertEqual(args.dir, "D:\\data")
        self.assertEqual(args.output, "out.xlsx")

    def test_grading_invocation_unchanged(self):
        args = build_parser().parse_args(["--template", "t.xlsx", "--stage", "sen"])
        self.assertIsNone(args.command)
        self.assertEqual(args.template, "t.xlsx")
        self.assertEqual(args.stage, "sen")

    def test_main_dispatch_success(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", STUDENTS, 70, "第1次")
        _write_result(self.tmp / f"{BASE}-2.xlsx", STUDENTS, 90, "第2次")
        code = main(["median", BASE, "--dir", str(self.tmp)])
        self.assertEqual(code, 0)
        self.assertTrue(self._out().is_file())

    def test_main_dispatch_mismatch_returns_1(self):
        _write_result(self.tmp / f"{BASE}-1.xlsx", STUDENTS, 70, "第1次")
        _write_result(self.tmp / f"{BASE}-2.xlsx",
                      [("2021001", "张三"), ("2021003", "王五")], 80, "第2次")
        code = main(["median", BASE, "--dir", str(self.tmp)])
        self.assertEqual(code, 1)
        self.assertFalse(self._out().exists())


if __name__ == "__main__":
    unittest.main()