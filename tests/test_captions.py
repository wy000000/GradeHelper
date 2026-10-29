"""图片描述嵌入与 LLM 输出解析测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.llm_client import parse_json_object  # noqa: E402
from gradehelper.models import ParsedReport, ReportImage  # noqa: E402
from gradehelper.parser import embed_captions  # noqa: E402


class TestEmbedCaptions(unittest.TestCase):
    def test_embed(self):
        report = ParsedReport("云安全", "安全2401", "2024001", "张三", "d.docx",
                              "开头\n[[PIC:P1]]\n结尾")
        report.images = [ReportImage(index=1, ext="png", data=b"", caption="一张网络拓扑图")]
        md = embed_captions(report.md_text, report.images, "[图片描述 P{n}: {caption}]")
        self.assertIn("[图片描述 P1: 一张网络拓扑图]", md)
        self.assertNotIn("[[PIC:P1]]", md)

    def test_cleanup_placeholder(self):
        self.assertNotIn("[[PIC:P9]]", embed_captions("[[PIC:P9]]", [], "[图片描述 P{n}: {caption}]"))


class TestParseJsonObject(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_object('{"score": 10, "confidence": 0.9, "comment": "好"}'),
                         {"score": 10, "confidence": 0.9, "comment": "好"})

    def test_fenced(self):
        data = parse_json_object('```json\n{"score": 5, "confidence": 0.5, "comment": "一般"}\n```')
        self.assertEqual(data["score"], 5)

    def test_with_prefix(self):
        data = parse_json_object('评审意见如下：{"score": 3, "confidence": 0.6, "comment": "较差"} 完毕')
        self.assertEqual(data["score"], 3)


if __name__ == "__main__":
    unittest.main()
