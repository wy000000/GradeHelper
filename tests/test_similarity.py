"""相似度规则（copied）测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.models import ParsedReport, RuleItem  # noqa: E402
from gradehelper.rules.similarity import copied, euclidean_similarity  # noqa: E402


def make_report(md: str, sid: str, name: str) -> ParsedReport:
    return ParsedReport(course="云安全", klass="安全2401", student_id=sid, name=name,
                        source_path="dummy.docx", md_text=md)


class TestEuclideanSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(euclidean_similarity("相同的报告内容ABC", "相同的报告内容ABC"), 1.0)

    def test_different(self):
        self.assertLess(euclidean_similarity("甲报告完全不同", "乙报告大相径庭内容迥异"), 0.9)


class TestCopiedBatch(unittest.TestCase):
    def test_batch(self):
        rule = RuleItem(key="copied", default="-5", weight=1.0, method="rule", evaluation="", comment="")
        a = make_report("完全一样的报告正文" * 50, "2024001", "张三")
        b = make_report("完全一样的报告正文" * 50, "2024002", "李四")
        c = make_report("独一无二的报告内容，与任何人都不相似。" * 20, "2024003", "王五")
        result = copied([a, b, c], rule)
        self.assertEqual(result["2024001"][0], -5.0)
        self.assertEqual(result["2024002"][0], -5.0)
        self.assertEqual(result["2024003"][0], 0.0)
        self.assertIn("2024002", result["2024001"][1])


if __name__ == "__main__":
    unittest.main()
