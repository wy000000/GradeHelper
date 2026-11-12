"""指定文件（--files）测试：名单解析、三阶段筛选与「模式」日志。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper import __main__ as cli  # noqa: E402
from gradehelper.config import (  # noqa: E402
    AppConfig, EndpointConfig, PathsConfig, PromptsConfig,
)
from gradehelper.excel_io import read_result_items, write_results  # noqa: E402
from gradehelper.grader import (  # noqa: E402
    Grader, _files_label, _run_mode_label,
)
from gradehelper.models import ItemScore, ParsedReport, RuleItem  # noqa: E402
from gradehelper.parser import normalize_file_names, select_paths  # noqa: E402

NAME_FORMAT = "{course}-{klass}-{student_id}-{stdName}-{reportName}"
BASE = "云安全-安全2401-{sid}-张三-课程报告"
IMAGE_CONTEXT = "[图片描述 P{n}: {caption}]"


def _config(batch: int = 2) -> AppConfig:
    return AppConfig(
        llm=EndpointConfig(llm_workers=batch, vllm_workers=batch),
        vllm=EndpointConfig(llm_workers=batch, vllm_workers=batch),
        paths=PathsConfig(
            report_name_format=NAME_FORMAT,
            report_name_separator="-",
            result_name_format="{course}-{tagDep}-{reportName}-评分结果.xlsx",
            parse_workers=2,
        ),
        prompts=PromptsConfig(image_context=IMAGE_CONTEXT),
    )


def _docx(tmp: Path, sid: str) -> Path:
    path = tmp / f"{BASE.format(sid=sid)}.docx"
    path.write_bytes(b"")
    return path


def _rules() -> list[RuleItem]:
    return [
        RuleItem(key="baseScore", default="25", weight=1.0, method="code",
                 evaluation="", comment="报告已提交，+25分"),
        RuleItem(key="event", default="0-15", weight=0.15, method="LLM",
                 evaluation="事件描述", comment=""),
        RuleItem(key="eventCause", default="0-15", weight=0.15, method="LLM",
                 evaluation="原因分析", comment=""),
    ]


def _report(tmp: Path, sid: str) -> ParsedReport:
    return ParsedReport(
        course="云安全", klass="安全2401", student_id=sid, std_name="张三",
        report_name="课程报告", source_path=str(tmp / f"{BASE.format(sid=sid)}.md"),
        md_text="正文",
    )


class TestNormalizeFileNames(unittest.TestCase):
    def test_default_and_none_tokens_mean_no_filter(self):
        self.assertEqual(normalize_file_names(None), [])
        self.assertEqual(normalize_file_names([]), [])
        self.assertEqual(normalize_file_names(["none"]), [])
        self.assertEqual(normalize_file_names("NONE"), [])

    def test_splits_comma_semicolon_and_whitespace(self):
        self.assertEqual(normalize_file_names(["a.docx,b.docx"]),
                         ["a.docx", "b.docx"])
        self.assertEqual(normalize_file_names(["a.docx;b.docx"]),
                         ["a.docx", "b.docx"])
        self.assertEqual(normalize_file_names(["a.docx", "b.docx c.docx"]),
                         ["a.docx", "b.docx", "c.docx"])
        self.assertEqual(normalize_file_names("a.docx, b.docx"),
                         ["a.docx", "b.docx"])

    def test_deduplicates_and_keeps_order(self):
        self.assertEqual(normalize_file_names(["b.docx,a.docx,b.docx"]),
                         ["b.docx", "a.docx"])


class TestSelectPaths(unittest.TestCase):
    def test_matches_stem_full_name_and_ignores_case(self):
        paths = [Path("dir/云安全-2021001-张三-课程报告.docx"),
                 Path("dir/云安全-2021002-李四-课程报告.docx")]
        matched, unmatched = select_paths(
            paths, ["云安全-2021001-张三-课程报告"])
        self.assertEqual(matched, [paths[0]])
        self.assertEqual(unmatched, [])

        matched, unmatched = select_paths(
            paths, ["云安全-2021002-李四-课程报告.docx".lower()])
        self.assertEqual(matched, [paths[1]])

        matched, unmatched = select_paths(paths, ["不存在的文件"])
        self.assertEqual(matched, [])
        self.assertEqual(unmatched, ["不存在的文件"])

    def test_empty_names_returns_all(self):
        paths = [Path("a.docx"), Path("b.docx")]
        self.assertEqual(select_paths(paths, []), (paths, []))

    def test_full_docx_name_matches_same_stem_md(self):
        """名单写完档 .docx 名时，pic/sen 的同名 .md 也应命中。"""
        paths = [Path("dir/云安全-2021001-张三-课程报告.md")]
        matched, unmatched = select_paths(
            paths, ["云安全-2021001-张三-课程报告.docx"])
        self.assertEqual(matched, paths)
        self.assertEqual(unmatched, [])


class TestModeLabel(unittest.TestCase):
    def test_four_modes(self):
        self.assertEqual(_run_mode_label(False, []), "全部全量")
        self.assertEqual(_run_mode_label(True, []), "全部增量")
        self.assertEqual(_run_mode_label(False, ["a"]), "指定全量")
        self.assertEqual(_run_mode_label(True, ["a"]), "指定增量")

    def test_files_label(self):
        self.assertEqual(_files_label(["a", "b"]), "2 个（a、b）")
        self.assertEqual(_files_label([f"f{i}" for i in range(12)]),
                         "12 个（f0、f1、f2、f3、f4、f5、f6、f7、f8、f9 等 12 个）")


class TestFilesArgument(unittest.TestCase):
    def test_default_is_none(self):
        self.assertIsNone(cli.build_parser().parse_args([]).files)

    def test_explicit_none_token_means_no_filter(self):
        args = cli.build_parser().parse_args(["--files", "none"])
        self.assertEqual(normalize_file_names(args.files), [])

    def test_space_separated_values(self):
        args = cli.build_parser().parse_args(["--files", "a.docx", "b.docx"])
        self.assertEqual(args.files, ["a.docx", "b.docx"])

    def test_comma_separated_value(self):
        args = cli.build_parser().parse_args(["--files", "a.docx,b.docx"])
        self.assertEqual(normalize_file_names(args.files), ["a.docx", "b.docx"])

    def test_main_passes_files_to_grader(self):
        with patch.object(cli, "Grader") as grader_cls, \
                patch.object(cli, "load_config"), \
                patch.object(cli, "install_logging_bridge"):
            grader_cls.return_value.run.return_value = None
            self.assertEqual(cli.main(["--files", "a.docx"]), 0)
        self.assertEqual(grader_cls.return_value.run.call_args.kwargs["files"],
                         ["a.docx"])


class TestDocSpecifiedFiles(unittest.TestCase):
    """doc 阶段：名单收窄处理范围，增量判定仍以结果表学号记录为准。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "result.xlsx"
        self.rules = _rules()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, sids: list[str]) -> None:
        reports = [_report(self.tmp, sid) for sid in sids]
        rows = {
            sid: {"baseScore": ItemScore(key="baseScore", score=25.0)}
            for sid in sids
        }
        write_results(self.result_path, reports, self.rules, rows, mode="upsert")

    def _run_doc(self, grader: Grader, parsed: list[ParsedReport]):
        grader._log_path = self.tmp / "run.log"
        grader._save_parsed_batch = lambda reports, **kwargs: list(reports)
        with patch("gradehelper.stages.doc.parse_all", return_value=parsed) as parse_mock, \
                patch("gradehelper.stages.doc.read_rules",
                      return_value=self.rules) as rules_mock, \
                patch("gradehelper.stages.doc.run_program_rules",
                      return_value=({}, [])), \
                patch("gradehelper.stages.doc.write_results_atomic") as write_mock:
            out = grader.stage_doc(self.tmp, self.template, output=self.result_path)
        return out, parse_mock, rules_mock, write_mock

    def test_specified_full_parses_only_listed_documents(self):
        doc1 = _docx(self.tmp, "2021001")
        _docx(self.tmp, "2021002")
        grader = Grader(_config())
        grader.files = [doc1.stem]

        out, parse_mock, rules_mock, write_mock = self._run_doc(
            grader, [_report(self.tmp, "2021001")])

        self.assertEqual(out, self.result_path)
        self.assertEqual(parse_mock.call_args.kwargs["paths"], [doc1])
        rules_mock.assert_called_once()
        write_mock.assert_called_once()
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 指定全量", log_text)
        self.assertRegex(log_text, r"运行开始: [^\n]*\n模式: 指定全量")
        self.assertIn("指定文件: 1 个（", log_text)
        self.assertNotIn("增量模式:", log_text)

    def test_specified_incremental_only_parses_unrecorded_listed_documents(self):
        doc1 = _docx(self.tmp, "2021001")
        pending = _docx(self.tmp, "2021002")
        _docx(self.tmp, "2021003")
        self._seed(["2021001"])
        grader = Grader(_config())
        grader.incremental = True
        grader.files = [doc1.stem, pending.stem]

        out, parse_mock, rules_mock, write_mock = self._run_doc(
            grader, [_report(self.tmp, "2021002")])

        self.assertEqual(out, self.result_path)
        # 名单收窄到 2021001/2021002，其中 2021001 已有记录 → 只解析 2021002
        self.assertEqual(parse_mock.call_args.kwargs["paths"], [pending])
        rules_mock.assert_called_once()
        write_mock.assert_called_once()
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "1 个结果文件中已有记录的文档")
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 指定增量", log_text)
        self.assertRegex(log_text, r"运行开始: [^\n]*\n模式: 指定增量")

    def test_unmatched_name_warns_but_others_continue(self):
        doc1 = _docx(self.tmp, "2021001")
        grader = Grader(_config())
        grader.files = [doc1.stem, "不存在的文件"]

        _, parse_mock, _, _ = self._run_doc(grader, [_report(self.tmp, "2021001")])

        self.assertEqual(parse_mock.call_args.kwargs["paths"], [doc1])
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("指定的文件未找到（或不符合命名格式）: 不存在的文件", log_text)

    def test_all_names_unmatched_raises(self):
        _docx(self.tmp, "2021001")
        grader = Grader(_config())
        grader.files = ["不存在的文件"]
        grader._log_path = self.tmp / "run.log"

        with patch("gradehelper.stages.doc.parse_all", return_value=[]) as parse_mock, \
                patch("gradehelper.stages.doc.read_rules"), \
                patch("gradehelper.stages.doc.write_results_atomic"):
            with self.assertRaises(RuntimeError) as ctx:
                grader.stage_doc(self.tmp, self.template, output=self.result_path)

        self.assertIn("指定的文件在报告目录中没有找到", str(ctx.exception))
        parse_mock.assert_not_called()


class TestPicSpecifiedFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.rules = _rules()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_report(self, sid: str, *, described: bool, index: int = 3) -> Path:
        base = BASE.format(sid=sid)
        md_path = self.tmp / f"{base}.md"
        block = (f"[图片描述 P{index}: 旧描述]\n" if described else "")
        md_path.write_text(
            f"正文\n[[PIC:{base}-P{index}.png]]\n{block}", encoding="utf-8")
        (self.tmp / f"{base}-P{index}.png").write_bytes(b"\x89PNG")
        return md_path

    @staticmethod
    def _fake_caption_images(received: list[tuple[str, int]]):
        def _fake(reports, _cfg, _prompt, cancel=None, on_error=None, on_batch=None,
                  **_kwargs):
            for report in reports:
                for image in report.images:
                    received.append((report.student_id, image.index))
                    image.caption = f"图{image.index}描述"
            return len(received)
        return _fake

    def test_only_listed_report_is_sent_to_vllm(self):
        self._write_report("2021001", described=False)
        self._write_report("2021002", described=False)

        received: list[tuple[str, int]] = []
        grader = Grader(_config())
        grader.files = [BASE.format(sid="2021001")]
        grader._log_path = self.tmp / "run.log"

        with patch("gradehelper.stages.pic.caption_images",
                   self._fake_caption_images(received)), \
                patch("gradehelper.stages.pic.read_rules", return_value=self.rules):
            grader.stage_pic(self.tmp, self.template)

        self.assertEqual(received, [("2021001", 3)])
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 指定全量", log_text)
        self.assertRegex(log_text, r"运行开始: [^\n]*\n模式: 指定全量")

    def test_specified_incremental_skips_described_within_list(self):
        self._write_report("2021001", described=True)
        self._write_report("2021002", described=False)

        received: list[tuple[str, int]] = []
        grader = Grader(_config())
        grader.incremental = True
        grader.files = [BASE.format(sid="2021001"), BASE.format(sid="2021002")]
        grader._log_path = self.tmp / "run.log"

        with patch("gradehelper.stages.pic.caption_images",
                   self._fake_caption_images(received)), \
                patch("gradehelper.stages.pic.read_rules", return_value=self.rules):
            grader.stage_pic(self.tmp, self.template)

        self.assertEqual(received, [("2021002", 3)])
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "1 张已有描述图片（1 份报告）")
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 指定增量", log_text)
        self.assertRegex(log_text, r"运行开始: [^\n]*\n模式: 指定增量")


class TestSenSpecifiedFiles(unittest.TestCase):
    """sen 指定增量：名单收窄范围，是否评分仍看结果表中 LLM 项是否为空。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "result.xlsx"
        self.rules = _rules()
        self.reports = [_report(self.tmp, sid)
                        for sid in ("2021001", "2021002", "2021003")]

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self, rows: dict[str, dict[str, ItemScore]]) -> None:
        write_results(self.result_path, self.reports, self.rules, rows, mode="upsert")

    @staticmethod
    def _scored() -> dict[str, ItemScore]:
        return {
            "baseScore": ItemScore(key="baseScore", score=25.0),
            "event": ItemScore(key="event", score=11.0, confidence=0.9),
            "eventCause": ItemScore(key="eventCause", score=7.0, confidence=0.8),
        }

    @staticmethod
    def _unscored() -> dict[str, ItemScore]:
        return {
            "baseScore": ItemScore(key="baseScore", score=25.0),
            "event": ItemScore(key="event", score=None),
            "eventCause": ItemScore(key="eventCause", score=None),
        }

    def test_only_listed_report_is_graded(self):
        self._seed({sid: self._unscored() for sid in
                    ("2021001", "2021002", "2021003")})
        graded: list[str] = []

        def fake_grade(_client, report, _llm_rules):
            graded.append(report.student_id)
            return {
                "event": ItemScore(key="event", score=11.0, confidence=0.9),
                "eventCause": ItemScore(key="eventCause", score=7.0, confidence=0.8),
            }

        grader = Grader(_config())
        # 名单写 doc 阶段的完整 .docx 名，sen 阶段按主名命中同名 .md
        grader.files = [BASE.format(sid="2021002") + ".docx"]
        grader._log_path = self.tmp / "run.log"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        grader._grade_with_llm_batch = fake_grade

        with patch("gradehelper.stages.sen.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        self.assertEqual(graded, ["2021002"])
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 指定全量", log_text)
        self.assertRegex(log_text, r"运行开始: [^\n]*\n模式: 指定全量")

    def test_specified_incremental_skips_scored_within_list(self):
        self._seed({
            "2021001": self._scored(),
            "2021002": self._unscored(),
            "2021003": self._unscored(),
        })
        graded: list[str] = []

        def fake_grade(_client, report, _llm_rules):
            graded.append(report.student_id)
            return {
                "event": ItemScore(key="event", score=11.0, confidence=0.9),
                "eventCause": ItemScore(key="eventCause", score=7.0, confidence=0.8),
            }

        grader = Grader(_config())
        grader.incremental = True
        grader.files = [BASE.format(sid="2021001"), BASE.format(sid="2021002")]
        grader._log_path = self.tmp / "run.log"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        grader._grade_with_llm_batch = fake_grade

        with patch("gradehelper.stages.sen.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        # 名单内 2021001/2021002；2021001 已评分 → 只评分 2021002
        self.assertEqual(graded, ["2021002"])
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "1 份 LLM 项已评分报告")
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 指定增量", log_text)
        self.assertRegex(log_text, r"运行开始: [^\n]*\n模式: 指定增量")

    def test_specified_incremental_without_result_row_skips_without_llm(self):
        """名单内文件在结果表中没有行：记录错误并跳过，不提交 LLM、不退出。"""
        write_results(self.result_path, [self.reports[0]], self.rules,
                      {"2021001": self._scored()}, mode="upsert")

        grader = Grader(_config())
        grader.incremental = True
        grader.files = [BASE.format(sid="2021002")]
        grader._log_path = self.tmp / "run.log"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        grader._grade_with_llm_batch = lambda *args, **kwargs: self.fail(
            "学号不在结果表时不应提交 LLM 请求")

        with patch("gradehelper.stages.sen.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        # 不再抛异常退出；缺行报告记为失败并提示先跑 doc
        self.assertEqual(out, self.result_path)
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("学号不在结果文件 result 表中", log_text)
        self.assertIn("2021002", log_text)
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.failed_count, 1)
        self.assertEqual(entry.extra.get("学号缺失"), "1 份（不在 result 表，已跳过）")

    def test_unmatched_names_raise_before_grading(self):
        self._seed({sid: self._unscored() for sid in
                    ("2021001", "2021002", "2021003")})

        grader = Grader(_config())
        grader.files = ["不存在的文件"]
        grader._log_path = self.tmp / "run.log"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)

        with patch("gradehelper.stages.sen.read_rules", return_value=self.rules):
            with self.assertRaises(RuntimeError) as ctx:
                grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                 skip_images=0)

        self.assertIn("指定的文件没有对应的 md 文件", str(ctx.exception))
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("指定的文件没有对应的 md 文件: 不存在的文件", log_text)


if __name__ == "__main__":
    unittest.main()
