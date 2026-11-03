"""LLM 批量 Prompt 测试：评分项列表格式、占位符替换、真实模板可渲染。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import load_config  # noqa: E402
from gradehelper.grader import (  # noqa: E402
    build_llm_items_section, render_llm_batch_prompt,
)
from gradehelper.models import RuleItem  # noqa: E402


def _rule(key: str, default: str, evaluation: str) -> RuleItem:
    return RuleItem(key=key, default=default, weight=1.0, method="LLM",
                    evaluation=evaluation, comment="")


class TestBuildLlmItemsSection(unittest.TestCase):
    def test_order_is_key_range_evaluation(self):
        rules = [
            _rule("event", "0-100", "事件描述完整"),
            _rule("single", "20", "单项标准"),
        ]
        self.assertEqual(build_llm_items_section(rules).splitlines(),
                         ["- event；0-100；事件描述完整",
                          "- single；20-20；单项标准"])


class TestRenderLlmBatchPrompt(unittest.TestCase):
    TEMPLATE = (
        "评分项列表（格式：评分项编号；分数范围；评分标准）：\n"
        "{items_section}\n"
        "value 为：{\"score\": <0-100整数>, \"confidence\": <0~1的数字>}\n"
        "<<<REPORT\n"
        "{report}\n"
        "REPORT>>>"
    )

    def test_placeholders_replaced_and_literal_braces_kept(self):
        out = render_llm_batch_prompt(self.TEMPLATE, "ITEMS", "BODY")
        self.assertIn("ITEMS", out)
        self.assertNotIn("{items_section}", out)
        self.assertNotIn("{report}", out)
        # 输出示例中的字面量花括号必须原样保留
        self.assertIn("{\"score\": <0-100整数>, \"confidence\": <0~1的数字>}", out)
        self.assertIn("<<<REPORT\nBODY\nREPORT>>>", out)

    def test_report_with_literal_placeholder_not_rescanned(self):
        out = render_llm_batch_prompt(self.TEMPLATE, "ITEMS",
                                      "正文含 {items_section} 字面量")
        self.assertIn("正文含 {items_section} 字面量", out)


class TestRealConfigTemplate(unittest.TestCase):
    def test_config_llm_batch_renders(self):
        cfg = load_config()
        self.assertTrue(cfg.prompts.llm_batch)
        out = render_llm_batch_prompt(cfg.prompts.llm_batch, "ITEMS", "BODY_TEXT")
        self.assertNotIn("{items_section}", out)
        self.assertNotIn("{report}", out)
        # 报告正文只嵌入一次
        self.assertEqual(out.count("BODY_TEXT"), 1)
        self.assertIn("{\"score\":", out)
        self.assertIn("<<<REPORT", out)
        self.assertIn("REPORT>>>", out)


if __name__ == "__main__":
    unittest.main()