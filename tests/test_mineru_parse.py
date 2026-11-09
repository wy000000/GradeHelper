"""MinerU 解析辅助逻辑测试：图片占位、档位选择与后缀配置。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import PathsConfig, _normalize_document_suffixes  # noqa: E402
from gradehelper.parser import (  # noqa: E402
    _materialize_mineru_images,
    _mineru_tier,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png"


class TestMaterializeMineruImages(unittest.TestCase):
    def test_markdown_refs_and_unreferenced_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            images = out / "images"
            images.mkdir()
            (images / "page_0_image_1.png").write_bytes(PNG)
            (images / "page_1_image_2.jpg").write_bytes(b"\xff\xd8\xff" + b"fake-jpg")
            (images / "unused.png").write_bytes(PNG + b"-unused")
            md = "# 报告\n\n![图1](images/page_0_image_1.png)\n\n正文\n"

            text, parsed = _materialize_mineru_images(
                md, out, "课程-班级-学号-姓名-报告", [])

            self.assertIn("[[PIC:课程-班级-学号-姓名-报告-P1.png]]", text)
            self.assertNotIn("page_0_image_1.png", text)
            self.assertEqual([img.index for img in parsed], [1, 2, 3])
            self.assertEqual([img.ext for img in parsed], ["png", "jpg", "png"])
            # 未在 Markdown 中引用的图片追加到文末，避免丢失
            self.assertIn("[[PIC:课程-班级-学号-姓名-报告-P2.jpg]]", text)
            self.assertIn("[[PIC:课程-班级-学号-姓名-报告-P3.png]]", text)
            self.assertTrue(text.rstrip().endswith("]]"))

    def test_image_comment_placeholder_is_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            images = out / "images"
            images.mkdir()
            (images / "page_0_image_1.png").write_bytes(PNG)
            md = "第一段\n\n<!-- image -->\n\n第二段\n"

            text, parsed = _materialize_mineru_images(md, out, "base", [])

            self.assertEqual(len(parsed), 1)
            self.assertIn("<!-- image -->", md)
            self.assertNotIn("<!-- image -->", text)
            self.assertIn("[[PIC:base-P1.png]]", text)

    def test_missing_reference_is_removed_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            md = "前文\n\n![缺失](images/missing.png)\n\n后文\n"
            warnings: list[str] = []

            text, parsed = _materialize_mineru_images(md, out, "base", warnings)

            self.assertEqual(parsed, [])
            self.assertNotIn("missing.png", text)
            self.assertTrue(any("读取 MinerU 图片失败" in w for w in warnings))


class TestMineruTier(unittest.TestCase):
    def test_pdf_uses_configured_tier(self):
        self.assertEqual(_mineru_tier(Path("a.pdf"), "basic"), "basic")
        self.assertEqual(_mineru_tier(Path("a.PDF"), "advanced"), "advanced")

    def test_invalid_tier_falls_back_to_standard(self):
        self.assertEqual(_mineru_tier(Path("a.pdf"), "unknown"), "standard")

    def test_office_always_flash(self):
        self.assertEqual(_mineru_tier(Path("a.docx"), "advanced"), "flash")
        self.assertEqual(_mineru_tier(Path("a.doc"), "advanced"), "flash")


class TestDocumentSuffixConfig(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(PathsConfig().document_suffixes, [".docx", ".doc", ".pdf"])

    def test_normalizes_list_and_string(self):
        self.assertEqual(_normalize_document_suffixes(["docx", ".PDF", "docx"]),
                         [".docx", ".pdf"])
        self.assertEqual(_normalize_document_suffixes("docx, doc;pdf"),
                         [".docx", ".doc", ".pdf"])

    def test_empty_falls_back_to_default(self):
        self.assertEqual(_normalize_document_suffixes([]),
                         [".docx", ".doc", ".pdf"])

    def test_invalid_mineru_tier_normalized(self):
        self.assertEqual(PathsConfig(mineru_tier="FAST").mineru_tier, "standard")
        self.assertEqual(PathsConfig(mineru_tier="flash").mineru_tier, "flash")


if __name__ == "__main__":
    unittest.main()
