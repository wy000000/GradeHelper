"""文件名解析与结构性规则测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.models import ParsedReport, RuleItem  # noqa: E402
from gradehelper.parser import parse_filename, scan_reports  # noqa: E402
from gradehelper.rules.structure import (  # noqa: E402
    has_catalogue,
    has_name_id,
    has_reference,
    redundant_rname,
    std_doc,
    use_template,
)


def make_report(md: str, sid: str = "2024001", name: str = "张三",
                klass: str = "安全2401", course: str = "云安全") -> ParsedReport:
    return ParsedReport(course=course, klass=klass, student_id=sid, std_name=name,
                        report_name="",
                        source_path="dummy.docx", md_text=md)


def make_rule(key: str, default: str, method: str = "code",
              evaluation: str = "", comment: str = "") -> RuleItem:
    return RuleItem(key=key, default=default, weight=1.0, method=method,
                    evaluation=evaluation, comment=comment)


class TestFilename(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_filename(Path("云安全-安全2401-2024001-张三-期中报告.docx")),
                         ("云安全", "安全2401", "2024001", "张三", "期中报告"))

    def test_default_suffixes_include_doc_and_pdf(self):
        for suffix in (".docx", ".doc", ".pdf"):
            self.assertEqual(
                parse_filename(Path(f"云安全-安全2401-2024001-张三-期中报告{suffix}")),
                ("云安全", "安全2401", "2024001", "张三", "期中报告"),
                msg=suffix)

    def test_custom_suffixes(self):
        path = Path("云安全-安全2401-2024001-张三-期中报告.pdf")
        self.assertIsNone(parse_filename(path, suffixes=(".docx",)))
        self.assertIsNotNone(parse_filename(path, suffixes=(".pdf",)))

    def test_course_with_dash(self):
        self.assertEqual(parse_filename(Path("云原生-安全-安全2401-2024001-张三-期中报告.docx")),
                         ("云原生-安全", "安全2401", "2024001", "张三", "期中报告"))

    def test_invalid(self):
        self.assertIsNone(parse_filename(Path("report.docx")))
        self.assertIsNone(parse_filename(Path("云安全-安全2401-2024001-张三-期中报告.txt")))
        self.assertIsNone(parse_filename(Path("~$云安全-安全2401-2024001-张三.docx")))


class TestScanReports(unittest.TestCase):
    def test_only_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            top = root / "云安全-安全2401-2024001-张三-期中报告.docx"
            top.write_bytes(b"docx")
            pdf = root / "云安全-安全2401-2024001-张三-期中报告.pdf"
            pdf.write_bytes(b"pdf")
            legacy = root / "云安全-安全2401-2024001-张三-期中报告.doc"
            legacy.write_bytes(b"doc")
            ignored = root / "云安全-安全2401-2024001-张三-期中报告.txt"
            ignored.write_bytes(b"txt")
            nested_dir = root / "sub"
            nested_dir.mkdir()
            nested = nested_dir / "云安全-安全2401-2024002-李四-期中报告.docx"
            nested.write_bytes(b"docx")

            self.assertEqual(scan_reports(root), [legacy, top, pdf])

    def test_custom_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "云安全-安全2401-2024001-张三-期中报告.pdf"
            pdf.write_bytes(b"pdf")
            docx = root / "云安全-安全2401-2024001-张三-期中报告.docx"
            docx.write_bytes(b"docx")

            self.assertEqual(scan_reports(root, suffixes=(".pdf",)), [pdf])


class TestStructureRules(unittest.TestCase):
    def test_std_doc(self):
        rule = make_rule("stdDoc", "-2",
                         comment="报告未按要求的.docx格式提交，扣分。")
        self.assertEqual(std_doc(make_report("正文"), rule)[0], 0.0)
        for suffix in (".doc", ".pdf", ".txt"):
            report = make_report("正文")
            report.source_path = f"dummy{suffix}"
            score, comment = std_doc(report, rule)
            self.assertEqual(score, -2.0, msg=suffix)
            self.assertEqual(comment, rule.comment, msg=suffix)

    def test_std_doc_case_insensitive_suffix(self):
        rule = make_rule("stdDoc", "-2")
        report = make_report("正文")
        report.source_path = "dummy.DOCX"
        self.assertEqual(std_doc(report, rule)[0], 0.0)

    def test_use_template(self):
        rule = make_rule("useTmpl", "-2", comment="报告未使用指定模板，扣分。")
        ok_texts = (
            "J I A N G S U    U N I V E R S I T Y\n学号：2024001\n姓名：张三",
            "jiangsu university\n学号 2024001",
            "J\tI\tA\tN\tG\tS\tU\nU N I V E R S I T Y\n2024001",
        )
        for text in ok_texts:
            score, comment = use_template(make_report(text), rule)
            self.assertEqual(score, 0.0, msg=text)
            self.assertNotEqual(comment, rule.comment, msg=text)

        bad_texts = (
            "江苏大学\n学号：2024001\n姓名：张三",          # 学校名缺失
            "2024001\nJ I A N G S U U N I V E R S I T Y",  # 学校名在学号之后
            "J I A N G S U\n2024001",                       # 学校名不完整
        )
        for text in bad_texts:
            score, comment = use_template(make_report(text), rule)
            self.assertEqual(score, -2.0, msg=text)
            self.assertEqual(comment, rule.comment, msg=text)

    def test_use_template_uses_real_student_id_not_label(self):
        rule = make_rule("useTmpl", "-2", comment="报告未使用指定模板，扣分。")
        # “学号”二字出现在学校名之前不算数，必须看真实学号的位置
        text = "学号：\nJ I A N G S U    U N I V E R S I T Y\n2024001"
        self.assertEqual(use_template(make_report(text), rule)[0], 0.0)

    def test_use_template_missing_student_id(self):
        rule = make_rule("useTmpl", "-2", comment="报告未使用指定模板，扣分。")
        text = "J I A N G S U    U N I V E R S I T Y\n姓名：张三"
        score, comment = use_template(make_report(text), rule)
        self.assertEqual(score, -2.0)
        self.assertEqual(comment, rule.comment)

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

    def test_has_catalogue_plain_title_with_tab_entries(self):
        rule = make_rule("hasCatalogue", "2")
        md = "封面\n\n目录\n一、概述\t1\n二、分析\t3\n三、总结\t5\n\n## 正文\n正文"
        self.assertEqual(has_catalogue(make_report(md), rule)[0], 2.0)

    def test_has_catalogue_without_title_not_counted(self):
        # 只有目录条目、没有单独的“目录”行时不加分
        rule = make_rule("hasCatalogue", "2")
        md = "封面\n\n一、概述......1\n二、分析......3\n三、总结......5\n\n" + "正文" * 50
        self.assertEqual(has_catalogue(make_report(md), rule)[0], 0.0)

    def test_has_catalogue_plain_title_heading_entries(self):
        rule = make_rule("hasCatalogue", "2")
        md = ("封面\n\n目    录\n"
              "#### 一、事件概述\t2\n#### 1.1 事件经过\t2\n#### 二、问题原因\t4\n\n"
              "## 一、事件概述\n正文")
        self.assertEqual(has_catalogue(make_report(md), rule)[0], 2.0)

    def test_has_catalogue_title_alone_counts(self):
        # “目录”单独成行即可，不要求下面有目录内容
        rule = make_rule("hasCatalogue", "2")
        for md in ("# 报告\n\n## 目录\n\n## 正文\n正文内容",
                   "# 报告\n\n目录\n\n## 正文\n正文内容",
                   "封面\n\n## 目录\n"):
            self.assertEqual(has_catalogue(make_report(md), rule)[0], 2.0, msg=md)

    def test_has_catalogue_ignores_spaces_and_format(self):
        rule = make_rule("hasCatalogue", "2")
        for md in ("封面\n\n- 目录\n正文",
                   "封面\n\n**目录**\n正文",
                   "封面\n\n目录：\n正文",
                   "封面\n\n目录（Contents）\n正文",
                   "封面\n\n一、目 录\n正文",
                   "封面\n\n1. 目\u3000录\n正文",
                   "封面\n\n  目  录 \n正文",
                   "封面\n\n\u3000目\u3000\u3000录\u3000\n正文"):
            self.assertEqual(has_catalogue(make_report(md), rule)[0], 2.0, msg=md)

    def test_has_catalogue_requires_standalone_line(self):
        rule = make_rule("hasCatalogue", "2")
        for md in ("本报告目录如下\n正文", "目录结构如下\n正文"):
            self.assertEqual(has_catalogue(make_report(md), rule)[0], 0.0, msg=md)

    def test_has_catalogue_second_half_not_counted(self):
        rule = make_rule("hasCatalogue", "2")
        md = "正文" * 200 + "\n目录\n" + "正文" * 100
        self.assertEqual(has_catalogue(make_report(md), rule)[0], 0.0)

    def test_has_reference(self):
        rule = make_rule("hasReference", "2")
        md = "# 报告\n\n## 参考文献\n[1] 张三. 云安全研究[J]. 2024."
        self.assertEqual(has_reference(make_report(md), rule)[0], 2.0)

    def test_has_reference_plain_and_numbered_title(self):
        rule = make_rule("hasReference", "2")
        for md in ("正文\n\n**参考文献**\n[1] 文献一", "正文\n\n六、参考文献\n[1] 文献一",
                   "正文\n\nReferences\n[1] A paper", "正文\n\n参考文献：\n[1] 文献一"):
            self.assertEqual(has_reference(make_report(md), rule)[0], 2.0, msg=md)

    def test_has_reference_score_comes_from_rule_default(self):
        md = "正文\n\n## 参考文献\n[1] 文献一"
        for default, expected in (("2", 2.0), ("5", 5.0), ("3.5", 3.5)):
            rule = make_rule("hasReference", default)
            self.assertEqual(has_reference(make_report(md), rule)[0], expected,
                             msg=f"默认值 {default}")

    def test_has_reference_title_alone_counts(self):
        # “参考文献”单独成行即可，不要求下面有内容
        rule = make_rule("hasReference", "2")
        for md in ("正文\n\n## 参考文献", "正文\n\n## 参考文献\n\n",
                   "正文\n\n## 参考文献\n\n## 附录\n附录内容",
                   "正文\n\n参考文献：\n\n"):
            self.assertEqual(has_reference(make_report(md), rule)[0], 2.0, msg=md)

    def test_has_reference_ignores_spaces_and_format(self):
        rule = make_rule("hasReference", "2")
        for md in ("正文\n\n- 参考文献\n[1] 文献一",
                   "正文\n\n**参考文献**\n[1] 文献一",
                   "正文\n\n一、参 考 文 献\n[1] 文献一",
                   "正文\n\n1. 参\u3000考\u3000文\u3000献\n[1] 文献一",
                   "正文\n\n  参  考  文  献 \n[1] 文献一",
                   "正文\n\n\u3000参\u3000\u3000考\u3000文\u3000\u3000献\u3000\n[1] 文献一"):
            self.assertEqual(has_reference(make_report(md), rule)[0], 2.0, msg=md)

    def test_has_reference_anywhere_in_text(self):
        # 全文任意位置出现“参考文献”行都算
        rule = make_rule("hasReference", "2")
        md = "正文" * 200 + "\n参考文献\n" + "正文" * 100
        self.assertEqual(has_reference(make_report(md), rule)[0], 2.0)

    def test_has_reference_requires_standalone_line(self):
        rule = make_rule("hasReference", "2")
        for md in ("参考文献如下\n[1] 文献一", "参考文献......12\n正文"):
            self.assertEqual(has_reference(make_report(md), rule)[0], 0.0, msg=md)

    def test_has_reference_absent(self):
        rule = make_rule("hasReference", "2")
        for md in ("没有参考文献的正文", "本文参考文献将在后续补充。",
                   "参考文献......12\n正文"):
            self.assertEqual(has_reference(make_report(md), rule)[0], 0.0, msg=md)


class TestRuleRange(unittest.TestCase):
    def test_range_parsing(self):
        self.assertEqual(make_rule("x", "0-15").score_range(), (0.0, 15.0))
        self.assertEqual(make_rule("x", "-2").score_range(), (-2.0, -2.0))
        self.assertEqual(make_rule("x", "25").score_range(), (25.0, 25.0))


if __name__ == "__main__":
    unittest.main()