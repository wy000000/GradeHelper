"""LLM 评分结果完整性测试：截断/缺项必须视为失败，错误携带完整原始输出。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.llm_client import parse_json_object  # noqa: E402
from gradehelper.llm_grading import LLMContentError, grade_with_llm_batch  # noqa: E402
from gradehelper.models import ParsedReport, RuleItem  # noqa: E402


class _FakeClient:
    def __init__(self, data=None, error: Exception | None = None,
                 text: str | None = None):
        self.data = data
        self.error = error
        self.text = text

    def chat(self, _messages, **_overrides):
        if self.error is not None:
            raise self.error
        if self.text is not None:
            return self.text
        return json.dumps(self.data, ensure_ascii=False)


def _rules() -> list[RuleItem]:
    return [
        RuleItem(key="event", default="0-100", weight=0.15, method="LLM",
                 evaluation="事件描述", comment=""),
        RuleItem(key="eventCause", default="0-100", weight=0.15, method="LLM",
                 evaluation="事件原因", comment=""),
    ]


def _report() -> ParsedReport:
    return ParsedReport(course="云安全", klass="安全2401", student_id="2021001",
                        std_name="张三", report_name="课程报告",
                        source_path="2021001.md", md_text="正文")


class TestParseJsonObject(unittest.TestCase):
    def test_truncated_json_raises(self):
        with self.assertRaises(ValueError):
            parse_json_object('{"event": {"score": 80, "comment": "好"}, '
                              '"eventCause": {"score":')


class TestGradeWithLlmBatch(unittest.TestCase):
    def test_missing_items_is_failure(self):
        client = _FakeClient(data={"event": {"score": 80, "confidence": 0.9}})
        with self.assertRaises(ValueError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertIn("eventCause", str(ctx.exception))
        self.assertIn("不完整", str(ctx.exception))

    def test_missing_items_error_carries_full_raw_text(self):
        """缺少评分项的错误必须携带完整原始输出（附属日志据此保存非截断信息）。"""
        raw = ('{"event": {"score": 80, "confidence": 0.9, "comment": "'
               + "很长的评语" * 100 + '"}}')
        client = _FakeClient(text=raw)
        with self.assertRaises(LLMContentError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertEqual(ctx.exception.raw_text, raw)  # 未截断
        # 主日志用的 str() 仍是简短原因，不夹带原文
        self.assertNotIn("很长的评语", str(ctx.exception))

    def test_invalid_entries_error_carries_full_raw_text(self):
        """某项不是对象 / score 缺失的错误同样携带完整原始输出。"""
        raw = json.dumps({
            "event": {"score": 80, "confidence": 0.9},
            "eventCause": {"confidence": 0.8},  # score 缺失
        }, ensure_ascii=False)
        client = _FakeClient(text=raw)
        with self.assertRaises(LLMContentError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertEqual(ctx.exception.raw_text, raw)

    def test_json_parse_error_is_failure(self):
        client = _FakeClient(error=ValueError("无法从 LLM 输出中解析 JSON（截断）"))
        with self.assertRaises(ValueError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertIn("解析 JSON", str(ctx.exception))

    def test_missing_score_is_failure(self):
        """某一项缺少 score（截断/字段缺失）不能兜底成最低分，按处理失败上抛。"""
        client = _FakeClient(data={
            "event": {"score": 80, "confidence": 0.9},
            "eventCause": {"confidence": 0.8},
        })
        with self.assertRaises(ValueError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertIn("eventCause", str(ctx.exception))
        self.assertIn("score", str(ctx.exception))

    def test_non_numeric_score_is_failure(self):
        client = _FakeClient(data={
            "event": {"score": "八十分"},
            "eventCause": {"score": 70},
        })
        with self.assertRaises(ValueError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertIn("event", str(ctx.exception))

    def test_non_object_item_is_failure(self):
        client = _FakeClient(data={
            "event": {"score": 80},
            "eventCause": "理由：内容不足",
        })
        with self.assertRaises(ValueError) as ctx:
            grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertIn("eventCause", str(ctx.exception))

    def test_complete_json_succeeds(self):
        client = _FakeClient(data={
            "event": {"score": 80, "confidence": 0.9, "comment": "好"},
            "eventCause": {"score": 70, "confidence": 0.8, "comment": "一般"},
        })
        scores = grade_with_llm_batch(client, _report(), _rules(), "prompt")
        self.assertEqual(scores["event"].score, 80.0)
        self.assertEqual(scores["eventCause"].score, 70.0)
        self.assertEqual(scores["event"].comment, "好")


if __name__ == "__main__":
    unittest.main()
