"""结构性规则：baseScore / hasNameID / redundantRName / hasCatalogue / hasReference。"""

from __future__ import annotations

import re

from ..models import ParsedReport, RuleItem


def base_score(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """报告存在即 +25（能解析出来即视为已提交）。"""
    score = rule.default_numeric() or 0.0
    return score, rule.comment or f"报告已提交，+{score:g}分"


def has_name_id(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """报告文本中必须同时包含学号与姓名，缺任意一个按默认值扣分。"""
    text = report.md_text
    missing = []
    if report.student_id not in text:
        missing.append("学号")
    if report.std_name not in text:
        missing.append("姓名")
    if not missing:
        return 0.0, "报告中包含姓名和学号，不扣分。"
    penalty = rule.default_numeric() or 0.0
    comment = f"报告中不含{'、'.join(missing)}，{penalty:+g}分。"
    return penalty, comment


def redundant_rname(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """题目冗余：第一个姓名出现之前同时包含“课程报告”和“实验报告”则扣分。"""
    text = report.md_text
    pos = text.find(report.std_name)
    prefix = text[:pos] if pos >= 0 else text
    if "课程报告" in prefix and "实验报告" in prefix:
        penalty = rule.default_numeric() or 0.0
        return penalty, rule.comment or f"报告名称冗余，{penalty:+g}分。"
    return 0.0, "报告名称无冗余，不扣分。"


def has_catalogue(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """包含目录：报告前半部分存在“目录”标题且内容非空，或出现目录条目特征。"""
    text = report.md_text
    first_half = text[: len(text) // 2]
    lines = text.splitlines()
    for index, raw_line in enumerate(first_half.splitlines()):
        if not CATALOGUE_TITLE.match(_plain_text(raw_line)):
            continue
        if not _catalogue_section_has_content(lines, index, _heading_level(raw_line)):
            return 0.0, "报告目录内容为空，不加分。"
        score = rule.default_numeric() or 0.0
        return score, rule.comment or f"报告包含目录，+{score:g}分。"
    if count_toc_lines(first_half) >= 3:
        score = rule.default_numeric() or 0.0
        return score, rule.comment or f"报告包含目录，+{score:g}分。"
    return 0.0, "报告未检测到目录，不加分。"


CATALOGUE_TITLE = re.compile(
    r"^(?:"
    r"第?[一二三四五六七八九十百]+[章节部分]?\s*[、.．,，:：]?\s*"
    r"|\d+(?:\.\d+)*\s*[、.．,，:：)]?\s*"
    r")?"
    r"(?:目\s*录|table\s+of\s+contents|contents?)"
    r"(?:\s*[（(][^）)]*[)）])?\s*[:：]?$",
    re.IGNORECASE,
)
REFERENCE_TITLE = re.compile(
    r"^(?:"
    r"第?[一二三四五六七八九十百]+[章节部分]?\s*[、.．,，:：]?\s*"
    r"|\d+(?:\.\d+)*\s*[、.．,，:：)]?\s*"
    r")?"
    r"(?:参考文献|参考资料|references?|bibliography|works\s+cited)"
    r"(?:\s*[（(][^）)]*[)）])?\s*[:：]?$",
    re.IGNORECASE,
)
MD_HEADING = re.compile(r"^(#{1,6})\s+\S")
PIC_ONLY_LINE = re.compile(r"^\[\[PIC:[^\]]+\]\]$")
HTML_COMMENT_ONLY = re.compile(r"^<!--.*-->$")
RULE_SEPARATOR_LINES = {"---", "***", "___"}
# 目录条目：标题文字 + 制表符/点线/连续空格 + 页码
TOC_ENTRY = re.compile(r"^.{1,60}(?:\t+|\s{4,}|\.{3,}|…{1,2})\d{1,3}\s*$")


def _heading_level(line: str) -> int | None:
    match = MD_HEADING.match(line.strip())
    return len(match.group(1)) if match else None


def _plain_text(line: str) -> str:
    """去掉 Markdown 标题标记与强调符号，返回行内纯文本。"""
    text = re.sub(r"^#{1,6}\s*", "", line.strip())
    return text.replace("*", "").replace("_", "").strip()


def _section_has_content(lines: list[str], start: int,
                         title_level: int | None) -> bool:
    """章节标题后是否存在实质内容；遇同级或更高级标题即视为章节结束。"""
    for line in lines[start + 1:]:
        level = _heading_level(line)
        if level is not None and (title_level is None or level <= title_level):
            break
        stripped = line.strip()
        if not stripped:
            continue
        if (PIC_ONLY_LINE.match(stripped) or HTML_COMMENT_ONLY.match(stripped)
                or stripped in RULE_SEPARATOR_LINES):
            continue
        return True
    return False


def _catalogue_section_has_content(lines: list[str], start: int,
                                   title_level: int | None) -> bool:
    """目录标题后是否存在目录条目。

    普通文本标题（无 Markdown 级别）无法靠标题级别判断章节结束，因此把
    非目录样式的标题视为正文开始；目录条目本身可能是 Markdown 标题。
    """
    for line in lines[start + 1:]:
        level = _heading_level(line)
        if level is not None:
            if title_level is not None and level <= title_level:
                break
            if title_level is None and not TOC_ENTRY.match(_plain_text(line)):
                break
        stripped = line.strip()
        if not stripped:
            continue
        if (PIC_ONLY_LINE.match(stripped) or HTML_COMMENT_ONLY.match(stripped)
                or stripped in RULE_SEPARATOR_LINES):
            continue
        return True
    return False


def has_reference(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """包含参考文献：存在“参考文献”章节标题且章节内容非空。"""
    lines = report.md_text.splitlines()
    found = False
    for index, line in enumerate(lines):
        if not REFERENCE_TITLE.match(_plain_text(line)):
            continue
        found = True
        if not _section_has_content(lines, index, _heading_level(line)):
            continue
        score = rule.default_numeric() or 0.0
        return score, rule.comment or f"报告包含参考文献，+{score:g}分。"
    if found:
        return 0.0, "报告参考文献章节为空，不加分。"
    return 0.0, "报告未检测到参考文献章节，不加分。"


def count_toc_lines(text: str) -> int:
    """统计目录样式行数：标题文字 + 点线/制表符/连续空格 + 阿拉伯页码。"""
    return sum(1 for line in text.splitlines() if TOC_ENTRY.match(line.strip()))
