"""内容规则测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.models import ParsedReport, RuleItem  # noqa: E402
from gradehelper.rules.content import (  # noqa: E402
    has_requirement, summary_docker, summary_sql, too_many_list, window_similarity,
)


def make_report(md: str) -> ParsedReport:
    return ParsedReport(course="云安全", klass="安全2401", student_id="2024001", name="张三",
                        source_path="dummy.docx", md_text=md)


def make_rule(key: str, default: str) -> RuleItem:
    return RuleItem(key=key, default=default, weight=1.0, method="rule", evaluation="", comment="")


REQUIREMENT_MD = (
    "前言\n"
    "1、针对某一云安全问题进行论述，叙述其产生的原因，带来的危害，解决的方法。\n"
    "2、对本门课程的总结，包括收益、建议等。\n"
    "正文"
)
REQUIREMENT_EVALUATION = (
    "包含题目。报告前半部分包含相似度不少于90%的下面字符串。“1、针对某一云安全问题进行论述，"
    "叙述其产生的原因，带来的危害，解决的方法。\\n2、对本门课程的总结，包括收益、建议等。”"
    "如果不包含，-3。"
)


class TestTooManyList(unittest.TestCase):
    def test_list_abuse(self):
        md = "- 列表项一aaaaaaaaaaaaaaaaaaaaaaaaaaaa\n- 列表项二bbbbbbbbbbbbbbbbbbbbbbbbbbbb\n短段落"
        self.assertEqual(too_many_list(make_report(md), make_rule("tooManyList", "-4"))[0], -4.0)

    def test_normal(self):
        md = "这是一大段正文" * 50 + "\n- 一个短列表"
        self.assertEqual(too_many_list(make_report(md), make_rule("tooManyList", "-4"))[0], 0.0)

    def test_code_excluded(self):
        md = "```\n- 代码里的列表aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n```\n" + "正文段落" * 30
        self.assertEqual(too_many_list(make_report(md), make_rule("tooManyList", "-4"))[0], 0.0)


class TestHasRequirement(unittest.TestCase):
    def setUp(self):
        self.rule = RuleItem(key="hasRequirement", default="-3", weight=1.0, method="rule",
                             evaluation=REQUIREMENT_EVALUATION, comment="")

    def test_contains(self):
        self.assertEqual(has_requirement(make_report(REQUIREMENT_MD), self.rule)[0], 0.0)

    def test_missing(self):
        self.assertEqual(has_requirement(make_report("完全无关的正文"), self.rule)[0], -3.0)

    def test_window_similarity(self):
        self.assertTrue(window_similarity("前面有一些字然后题目要求原文在此处后面还有一些字",
                                          "题目要求原文", 0.9))
        self.assertFalse(window_similarity("完全无关的内容", "题目要求原文", 0.9))


class TestSummaryKeywords(unittest.TestCase):
    def test_keywords(self):
        md = "正文\n\n## 总结\n通过本课程学习了 docker 容器技术，也了解了 MongoDB 和 SQLite。"
        self.assertEqual(summary_docker(make_report(md), make_rule("summaryDocker", "2"))[0], 2.0)
        self.assertEqual(summary_sql(make_report(md), make_rule("summarySql", "2"))[0], 2.0)

    def test_absent(self):
        no = make_report("正文\n\n## 总结\n没有涉及重点内容。")
        self.assertEqual(summary_docker(no, make_rule("summaryDocker", "2"))[0], 0.0)
        self.assertEqual(summary_sql(no, make_rule("summarySql", "2"))[0], 0.0)


if __name__ == "__main__":
    unittest.main()
