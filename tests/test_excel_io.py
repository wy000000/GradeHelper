"""结果文件读写测试：规则评语写入，以及 sen 阶段保留 doc 阶段的规则评语。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import AppConfig, PathsConfig  # noqa: E402
from gradehelper.excel_io import read_result_items, write_results  # noqa: E402
from gradehelper.grader import Grader  # noqa: E402
from gradehelper.models import ItemScore, ParsedReport, RuleItem  # noqa: E402


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


def _rules(with_llm: bool = True) -> list[RuleItem]:
    rules = [
        RuleItem(key="baseScore", default="25", weight=1.0, method="code",
                 evaluation="", comment="报告已提交，+25分"),
    ]
    if with_llm:
        rules.append(RuleItem(key="event", default="0-15", weight=0.15,
                              method="LLM", evaluation="事件描述", comment=""))
    return rules


class TestWriteResults(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rule_comments_are_written_and_read_back(self):
        path = self.tmp / "result.xlsx"
        rules = _rules()
        scores = {
            "2021001": {
                "baseScore": ItemScore(key="baseScore", score=25.0,
                                       comment="报告已提交，+25分"),
                "event": ItemScore(key="event", score=10.0, confidence=0.9,
                                   comment="事件描述完整"),
            }
        }
        write_results(path, [_report()], rules, scores, mode="upsert")

        items = read_result_items(path)
        self.assertEqual(items["2021001"]["baseScore"].score, 25.0)
        self.assertEqual(items["2021001"]["baseScore"].comment, "报告已提交，+25分")
        self.assertEqual(items["2021001"]["event"].score, 10.0)
        self.assertEqual(items["2021001"]["event"].confidence, 0.9)
        self.assertEqual(items["2021001"]["event"].comment, "事件描述完整")

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(read_result_items(self.tmp / "missing.xlsx"), {})

    def test_read_can_filter_rule_keys(self):
        path = self.tmp / "result.xlsx"
        scores = {
            "2021001": {
                "baseScore": ItemScore(key="baseScore", score=25.0, comment="规则评语"),
                "event": ItemScore(key="event", score=10.0, confidence=0.9,
                                   comment="LLM 评语"),
            }
        }
        write_results(path, [_report()], _rules(), scores, mode="upsert")

        items = read_result_items(path, rule_keys=["baseScore"])
        self.assertIn("baseScore", items["2021001"])
        self.assertNotIn("event", items["2021001"])


class TestDocWritesRuleComments(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.result_path = self.tmp / "result.xlsx"
        self.template = self.tmp / "template.xlsx"

    def tearDown(self):
        self._tmp.cleanup()

    def test_doc_stage_writes_rule_comments(self):
        rules = _rules(with_llm=True)
        report = _report()
        scores = {
            "2021001": {
                "baseScore": ItemScore(key="baseScore", score=25.0,
                                       comment="报告已提交，+25分"),
            }
        }
        grader = Grader(AppConfig(paths=PathsConfig(
            report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
            report_name_separator="-",
            result_name_format="{course}-{tagDep}-评分结果-{reportName}.xlsx",
        )))

        with patch("gradehelper.stages.doc.parse_all", return_value=[report]), \
                patch("gradehelper.stages.doc.read_rules", return_value=rules), \
                patch("gradehelper.stages.doc.run_program_rules",
                      return_value=(scores, [])), \
                patch.object(Grader, "_save_parsed", staticmethod(lambda reports: None)):
            grader.stage_doc(self.tmp, self.template, skip_images=0,
                             output=self.result_path)

        items = read_result_items(self.result_path)
        self.assertEqual(items["2021001"]["baseScore"].score, 25.0)
        self.assertEqual(items["2021001"]["baseScore"].comment, "报告已提交，+25分")


class TestSenKeepsRuleComments(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.result_path = self.tmp / "result.xlsx"
        self.template = self.tmp / "template.xlsx"

    def tearDown(self):
        self._tmp.cleanup()

    def _grader(self) -> Grader:
        return Grader(AppConfig(paths=PathsConfig(
            report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
            report_name_separator="-",
            result_name_format="{course}-{tagDep}-评分结果-{reportName}.xlsx",
        )))

    def test_sen_keeps_rule_comment_when_updating_llm_item(self):
        rules = _rules(with_llm=True)
        report = _report()
        write_results(self.result_path, [report], rules, {
            "2021001": {
                "baseScore": ItemScore(key="baseScore", score=25.0,
                                       comment="doc 阶段规则评语"),
                "event": ItemScore(key="event", score=0.0, comment=""),
            }
        }, mode="upsert")

        grader = self._grader()
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]
        grader._grade_with_llm_batch = lambda client, rpt, llm_rules: {
            "event": ItemScore(key="event", score=12.0, confidence=0.9,
                               comment="sen 阶段 LLM 评语")
        }

        with patch("gradehelper.stages.sen.read_rules", return_value=rules):
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        items = read_result_items(self.result_path)
        self.assertEqual(items["2021001"]["baseScore"].comment, "doc 阶段规则评语")
        self.assertEqual(items["2021001"]["event"].comment, "sen 阶段 LLM 评语")

    def test_sen_without_llm_rules_keeps_rule_comment(self):
        rules = _rules(with_llm=False)
        report = _report()
        write_results(self.result_path, [report], rules, {
            "2021001": {
                "baseScore": ItemScore(key="baseScore", score=25.0,
                                       comment="doc 阶段规则评语"),
            }
        }, mode="upsert")

        grader = self._grader()
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        with patch("gradehelper.stages.sen.read_rules", return_value=rules):
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        items = read_result_items(self.result_path)
        self.assertEqual(items["2021001"]["baseScore"].comment, "doc 阶段规则评语")


if __name__ == "__main__":
    unittest.main()