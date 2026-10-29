"""文件名解析与结构性规则测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.models import ParsedReport, RuleItem  # noqa: E402
from gradehelper.parser import parse_filename  # noqa: E402
from gradehelper.rules.structure import has_catalogue, has_name_id, redundant_rname  # noqa: E402


def make_report(md: str, sid: str = "2024001", name: str = "张三",
                klass: str = "安全2401", course: str = "云安全") -> ParsedReport:
    return ParsedReport(course=course, klass=klass, student_id=sid, name=name,
                        source_path="dummy.docx", md_text=md)


def make_rule(key: str, default: str, method: str = "rule",
              evaluation: str = "", comment: str = "") -> RuleItem:
    return RuleItem(key=key, default=default, weight=1.0, method=method,
                    evaluation=evaluation, comment=comment)


class TestFilename(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_filename(Path("云安全-安全2401-2024001-张三.docx")),
                         ("云安全", "安全2401", "2024001", "张三"))

    def test_course_with_dash(self):
        self.assertEqual(parse_filename(Path("云原生-安全-安全2401-2024001-张三.docx")),
                         ("云原生-安全", "安全2401", "2024001", "张三"))

    def test_invalid(self):
        self.assertIsNone(parse_filename(Path("report.docx")))
        self.assertIsNone(parse_filename(Path("云安全-安全2401-2024001-张三.pdf")))
        self.assertIsNone(parse_filename(Path("~$云安全-安全2401-2024001-张三.docx")))


class TestStructureRules(unittest.TestCase):
    def test_has_name_id(self):
        rule = make_rule("hasNameID", "-2")
        ok = make_report("学号 2024001 张三 的报告")
        self.assertEqual(has_name_id(ok, rule)[0], 0.0)
        bad = make_report("没有学号姓名的报告")
        self.assertEqual(has_name_id(bad, rule)[0], -2.0)

    def test_redundant_rname(self):
        rule = make_rule("redundantRName", "-2")
        bad = make_report("云安全课程报告\n云安全实验报告\n张三\n正文")
        self.assertEqual(redundant_rname(bad, rule)[0], -2.0)
        ok = make_report("云安全课程报告\n张三\n正文")
        self.assertEqual(redundant_rname(ok, rule)[0], 0.0)

    def test_has_catalogue(self):
        rule = make_rule("hasCatalogue", "2")
        md = "# 报告\n\n## 目录\n一、概述......1\n二、分析......3\n三、总结......5\n\n正文"
        self.assertEqual(has_catalogue(make_report(md), rule)[0], 2.0)
        self.assertEqual(has_catalogue(make_report("没有目录的正文"), rule)[0], 0.0)


class TestRuleRange(unittest.TestCase):
    def test_range_parsing(self):
        self.assertEqual(make_rule("x", "0-15").score_range(), (0.0, 15.0))
        self.assertEqual(make_rule("x", "-2").score_range(), (-2.0, -2.0))
        self.assertEqual(make_rule("x", "25").score_range(), (25.0, 25.0))


if __name__ == "__main__":
    unittest.main()
