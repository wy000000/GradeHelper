"""增量模式测试：doc 按结果文件记录筛选、pic 跳过已有描述、sen 跳过已评分。"""

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
    Grader, _llm_item_scored, _report_needs_llm_grading,
)
from gradehelper.models import ItemScore, ParsedReport, RuleItem  # noqa: E402
from gradehelper.parser import has_image_caption  # noqa: E402

NAME_FORMAT = "{course}-{klass}-{student_id}-{stdName}-{reportName}"
BASE = "云安全-安全2401-{sid}-张三-课程报告"
IMAGE_CONTEXT = "[图片描述 P{n}: {caption}]"


def _config(batch: int = 2) -> AppConfig:
    return AppConfig(
        llm=EndpointConfig(batch_size=batch),
        vllm=EndpointConfig(batch_size=batch),
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


class TestIncrementalArgument(unittest.TestCase):
    def test_default_is_false(self):
        self.assertFalse(cli.build_parser().parse_args([]).incremental)

    def test_explicit_true_false(self):
        parser = cli.build_parser()
        self.assertTrue(parser.parse_args(["--incremental", "true"]).incremental)
        self.assertFalse(parser.parse_args(["--incremental", "FALSE"]).incremental)

    def test_invalid_value_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--incremental", "yes"])

    def test_main_passes_flag_to_grader(self):
        with patch.object(cli, "Grader") as grader_cls, \
                patch.object(cli, "load_config"), \
                patch.object(cli, "install_logging_bridge"):
            grader_cls.return_value.run.return_value = None
            self.assertEqual(cli.main(["--incremental", "true"]), 0)
        self.assertTrue(grader_cls.return_value.run.call_args.kwargs["incremental"])


class TestIncrementalHelpers(unittest.TestCase):
    def test_has_image_caption(self):
        md = ("正文\n[[PIC:P3]]\n[图片描述 P3: 一张网络拓扑图]\n"
              "[[PIC:P4]]\n")
        self.assertTrue(has_image_caption(md, IMAGE_CONTEXT, 3))
        self.assertFalse(has_image_caption(md, IMAGE_CONTEXT, 4))
        # 模板无法定位描述块时不能判定为已有描述
        self.assertFalse(has_image_caption(md, "", 3))
        self.assertFalse(has_image_caption(md, "[图片描述 P{n}]", 3))


class TestDocIncremental(unittest.TestCase):
    """doc 增量以结果文件 result 表的学号记录为准，与 md 是否存在无关。"""

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
        with patch("gradehelper.grader.parse_all", return_value=parsed) as parse_mock, \
                patch("gradehelper.grader.read_rules",
                      return_value=self.rules) as rules_mock, \
                patch("gradehelper.grader.run_program_rules",
                      return_value=({}, [])), \
                patch("gradehelper.grader.write_results_atomic") as write_mock:
            out = grader.stage_doc(self.tmp, self.template, output=self.result_path)
        return out, parse_mock, rules_mock, write_mock

    def test_missing_result_file_parses_all_documents(self):
        _docx(self.tmp, "2021001")
        _docx(self.tmp, "2021002")
        grader = Grader(_config())
        grader.incremental = True

        parsed = [_report(self.tmp, "2021001"), _report(self.tmp, "2021002")]
        out, parse_mock, rules_mock, write_mock = self._run_doc(grader, parsed)

        self.assertEqual(out, self.result_path)
        # 结果文件不存在：paths=None 表示扫描整个目录
        self.assertIsNone(parse_mock.call_args.kwargs["paths"])
        rules_mock.assert_called_once()
        write_mock.assert_called_once()

    def test_existing_result_without_records_parses_all_documents(self):
        doc1 = _docx(self.tmp, "2021001")
        doc2 = _docx(self.tmp, "2021002")
        # 结果文件存在，但 result 表只有表头、没有学号记录
        write_results(self.result_path, [], self.rules, {}, mode="upsert")

        grader = Grader(_config())
        grader.incremental = True
        parsed = [_report(self.tmp, "2021001"), _report(self.tmp, "2021002")]

        out, parse_mock, rules_mock, write_mock = self._run_doc(grader, parsed)

        self.assertEqual(out, self.result_path)
        self.assertEqual(parse_mock.call_args.kwargs["paths"], [doc1, doc2])
        rules_mock.assert_called_once()
        write_mock.assert_called_once()
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "0 个结果文件中已有记录的文档")

    def test_only_documents_missing_from_result_are_parsed(self):
        _docx(self.tmp, "2021001")
        pending = _docx(self.tmp, "2021002")
        self._seed(["2021001"])

        grader = Grader(_config())
        grader.incremental = True
        parsed = [_report(self.tmp, "2021002")]

        out, parse_mock, rules_mock, write_mock = self._run_doc(grader, parsed)

        self.assertEqual(out, self.result_path)
        self.assertEqual(parse_mock.call_args.kwargs["paths"], [pending])
        rules_mock.assert_called_once()
        write_mock.assert_called_once()
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "1 个结果文件中已有记录的文档")
        self.assertEqual(entry.processed_count, 1)
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 全部增量", log_text)
        self.assertNotIn("增量模式:", log_text)

    def test_all_documents_recorded_skips_parse_rules_and_write(self):
        _docx(self.tmp, "2021001")
        _docx(self.tmp, "2021002")
        self._seed(["2021001", "2021002"])

        grader = Grader(_config())
        grader.incremental = True

        # parse_all 收到空路径列表，返回空表示没有待解析文档
        out, parse_mock, rules_mock, write_mock = self._run_doc(grader, [])

        self.assertEqual(out, self.result_path)
        self.assertEqual(parse_mock.call_args.kwargs["paths"], [])
        rules_mock.assert_not_called()
        write_mock.assert_not_called()
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "2 个结果文件中已有记录的文档")
        self.assertEqual(entry.processed_count, 0)

    def test_empty_directory_still_raises(self):
        grader = Grader(_config())
        grader.incremental = True

        with patch("gradehelper.grader.parse_all", return_value=[]), \
                patch("gradehelper.grader.read_rules"), \
                patch("gradehelper.grader.write_results_atomic"):
            with self.assertRaises(RuntimeError):
                grader.stage_doc(self.tmp, self.template, output=self.result_path)


class TestPicIncremental(unittest.TestCase):
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
        def _fake(reports, _cfg, _prompt, cancel=None, on_error=None, on_batch=None):
            for report in reports:
                for image in report.images:
                    received.append((report.student_id, image.index))
                    image.caption = f"图{image.index}描述"
            return len(received)
        return _fake

    def test_only_missing_captions_are_sent_to_vllm(self):
        described_md = self._write_report("2021001", described=True)
        missing_md = self._write_report("2021002", described=False)
        described_before = described_md.read_text(encoding="utf-8")
        missing_before = missing_md.read_text(encoding="utf-8")

        received: list[tuple[str, int]] = []
        grader = Grader(_config())
        grader.incremental = True
        grader._log_path = self.tmp / "run.log"

        with patch("gradehelper.grader.caption_images",
                   self._fake_caption_images(received)), \
                patch("gradehelper.grader.read_rules", return_value=self.rules):
            grader.stage_pic(self.tmp, self.template)

        self.assertEqual(received, [("2021002", 3)])
        self.assertEqual(described_md.read_text(encoding="utf-8"), described_before)
        self.assertEqual(missing_md.read_text(encoding="utf-8"), missing_before)
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("增量跳过"), "1 张已有描述图片（1 份报告）")
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 全部增量", log_text)
        self.assertNotIn("增量模式:", log_text)

    def test_full_mode_still_sends_all_images(self):
        self._write_report("2021001", described=True)
        self._write_report("2021002", described=False)

        received: list[tuple[str, int]] = []
        grader = Grader(_config())
        grader.incremental = False

        with patch("gradehelper.grader.caption_images",
                   self._fake_caption_images(received)), \
                patch("gradehelper.grader.read_rules", return_value=self.rules):
            grader.stage_pic(self.tmp, self.template)

        self.assertEqual(received, [("2021001", 3), ("2021002", 3)])


class TestSenIncremental(unittest.TestCase):
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

    def test_reports_with_any_unscored_llm_item_are_graded(self):
        self._seed({
            "2021001": {
                "baseScore": ItemScore(key="baseScore", score=25.0),
                "event": ItemScore(key="event", score=12.0, confidence=0.9),
                "eventCause": ItemScore(key="eventCause", score=8.0, confidence=0.8),
            },
            "2021002": {
                "baseScore": ItemScore(key="baseScore", score=25.0),
                "event": ItemScore(key="event", score=None),
                "eventCause": ItemScore(key="eventCause", score=None),
            },
            "2021003": {
                "baseScore": ItemScore(key="baseScore", score=25.0),
                "event": ItemScore(key="event", score=10.0, confidence=0.9),
                "eventCause": ItemScore(key="eventCause", score=None),
            },
        })

        graded: list[str] = []

        def fake_grade(_client, report, _llm_rules):
            graded.append(report.student_id)
            return {
                "event": ItemScore(key="event", score=11.0, confidence=0.9,
                                   comment="LLM 评语"),
                "eventCause": ItemScore(key="eventCause", score=7.0, confidence=0.8,
                                        comment="LLM 评语"),
            }

        grader = Grader(_config())
        grader.incremental = True
        grader._log_path = self.tmp / "run.log"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        grader._grade_with_llm_batch = fake_grade

        with patch("gradehelper.grader.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        self.assertEqual(set(graded), {"2021002", "2021003"})
        items = read_result_items(self.result_path)
        self.assertEqual(items["2021001"]["event"].score, 12.0)
        self.assertEqual(items["2021001"]["eventCause"].score, 8.0)
        self.assertEqual(items["2021002"]["event"].score, 11.0)
        self.assertEqual(items["2021003"]["eventCause"].score, 7.0)
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("报告"), "处理 2 份 / 成功 2 份 / 失败 0 份")
        self.assertEqual(entry.extra.get("增量跳过"), "1 份 LLM 项已评分报告")
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("模式: 全部增量", log_text)
        self.assertNotIn("增量模式:", log_text)

    def test_all_scored_reports_are_skipped_without_llm_or_write(self):
        rows = {
            report.student_id: {
                "baseScore": ItemScore(key="baseScore", score=25.0),
                "event": ItemScore(key="event", score=11.0, confidence=0.9),
                "eventCause": ItemScore(key="eventCause", score=7.0, confidence=0.8),
            }
            for report in self.reports
        }
        self._seed(rows)

        grader = Grader(_config())
        grader.incremental = True
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)

        with patch("gradehelper.grader.read_rules", return_value=self.rules), \
                patch("gradehelper.grader.write_results_atomic") as write_mock:
            grader._grade_with_llm_batch = lambda *args, **kwargs: self.fail(
                "全已评分时不应调用 LLM")
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        write_mock.assert_not_called()
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.processed_count, 0)
        self.assertEqual(entry.extra.get("增量跳过"), "3 份 LLM 项已评分报告")

    def test_scoring_helpers_treat_blank_score_as_unscored(self):
        self.assertFalse(_llm_item_scored(None))
        self.assertFalse(_llm_item_scored(ItemScore(key="event", score=None)))
        # 0 分也是有效评分，不再用 0 表示“未评分”
        self.assertTrue(_llm_item_scored(ItemScore(key="event", score=0.0)))
        self.assertTrue(_llm_item_scored(
            ItemScore(key="event", score=12.0)))

        existing = {
            "2021001": {"event": ItemScore(key="event", score=10.0, confidence=0.9),
                        "eventCause": ItemScore(key="eventCause", score=0.0)},
        }
        llm_rules = [rule for rule in self.rules if rule.is_llm]
        self.assertFalse(_report_needs_llm_grading("2021001", existing, llm_rules))
        # 结果文件中没有该学号（尚未评分）也需要评分
        self.assertTrue(_report_needs_llm_grading("2021002", existing, llm_rules))


if __name__ == "__main__":
    unittest.main()
