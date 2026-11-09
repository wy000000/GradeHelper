"""内容规则测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.models import ParsedReport, RuleItem  # noqa: E402
from gradehelper.rules.content import (  # noqa: E402
    has_requirement, too_many_list, window_similarity,
)


def make_report(md: str) -> ParsedReport:
    return ParsedReport(course="云安全", klass="安全2401", student_id="2024001",
                        std_name="张三", report_name="",
                        source_path="dummy.docx", md_text=md)


def make_rule(key: str, default: str) -> RuleItem:
    return RuleItem(key=key, default=default, weight=1.0, method="code", evaluation="", comment="")


REQUIREMENT_MD = (
    "前言\n"
    "1、针对某一云安全问题进行论述，叙述其产生的原因，带来的危害，解决的方法。\n"
    "2、对本门课程的总结，包括收益、建议等。\n"
    "正文"
)
REQUIREMENT_EVALUATION = (
    "包含题目。报告前半部分包含相似度不少于90%的下面字符串。“1、针对某一云安全问题进行论述，"
    "叙述其产生的原因，带来的危害，解决的方法。\\n2、对本门课程的总结，包括收益、建议等。”"
    "如果不包含扣分。"
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
        self.rule = RuleItem(key="hasRequirement", default="-3", weight=1.0, method="code",
                             evaluation=REQUIREMENT_EVALUATION, comment="")

    def test_contains(self):
        self.assertEqual(has_requirement(make_report(REQUIREMENT_MD), self.rule)[0], 0.0)

    def test_missing(self):
        self.assertEqual(has_requirement(make_report("完全无关的正文"), self.rule)[0], -3.0)

    def test_ignores_full_and_half_width_punctuation_and_spaces(self):
        # 正文使用半角标点/全角空格，题目要求使用全角标点，归一化后应完全一致
        md = (
            "前言\n"
            "1,\u3000针对某一云安全问题进行论述, 叙述其产生的原因, 带来的危害, 解决的方法.\n"
            "2, 对本门课程的总结, 包括收益, 建议等.\n"
            "正文"
        )
        self.assertEqual(has_requirement(make_report(md), self.rule)[0], 0.0)

    def test_second_half_not_matched(self):
        # 题目要求只出现在正文后半段，不再通过
        md = "无关正文" * 100 + REQUIREMENT_MD
        self.assertEqual(has_requirement(make_report(md), self.rule)[0], -3.0)

    def test_window_similarity(self):
        self.assertTrue(window_similarity("前面有一些字然后题目要求原文在此处后面还有一些字",
                                          "题目要求原文", 0.9))
        self.assertFalse(window_similarity("完全无关的内容", "题目要求原文", 0.9))

    def test_similar_window_found_regardless_of_text_offset(self):
        # 报告表述与题目要求仅有个别字差异（相似度 >= 90% 但不是完全子串）：
        # 无论正文整体平移多少（如 doc 阶段去除空行、移除跳过图片的占位符），都应命中。
        # 旧实现用固定步长网格扫描，偏移量不是步长整数倍时会漏判。
        pattern = "1、针对某一云安全问题进行论述，叙述其产生的原因，带来的危害，解决的方法。"
        variant = "1、针对某一云安全问题进行论述，叙述其产生的原因，带来的危患，解决的手段。"
        self.assertNotIn(variant, pattern)
        for pad in range(5):
            text = "甲" * pad + variant + "正文内容" * 8
            self.assertTrue(window_similarity(text, pattern, 0.9),
                            msg=f"偏移 {pad} 个字符时漏判")


if __name__ == "__main__":
    unittest.main()