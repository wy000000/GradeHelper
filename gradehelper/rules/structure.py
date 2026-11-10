"""结构性规则：baseScore / stdDoc / useTmpl / hasNameID /
redundantRName / hasCatalogue / hasReference。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import ParsedReport, RuleItem


def base_score(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """报告存在即 +25（能解析出来即视为已提交）。"""
    score = rule.default_numeric() or 0.0
    return score, rule.comment or f"报告已提交，+{score:g}分"


def std_doc(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """文档格式：原始文档后缀必须是 .docx，否则按默认值扣分。"""
    if Path(report.source_path).suffix.lower() == ".docx":
        return 0.0, "报告按要求以 .docx 格式提交，不扣分。"
    penalty = rule.default_numeric() or 0.0
    return penalty, rule.comment or f"报告未按要求的 .docx 格式提交，{penalty:+g}分。"


# 指定模板封面中的学校英文名：忽略大小写，字符之间允许任意数量空白
UNIVERSITY_NAME = re.compile(
    r"J\s*I\s*A\s*N\s*G\s*S\s*U\s*U\s*N\s*I\s*V\s*E\s*R\s*S\s*I\s*T\s*Y",
    re.IGNORECASE,
)


def use_template(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """是否使用指定模板：真实学号之前须出现“JIANGSU UNIVERSITY”。

    只认实际学号（文件名解析出的 report.student_id），不把报告里的
    “学号”二字当作学号；找不到学号同样按未使用模板扣分。
    学校名不区分大小写，字符之间允许任意数量空白（含换行/制表符）。
    """
    text = report.md_text
    sid = str(report.student_id).strip()
    pos = text.find(sid) if sid else -1
    if pos < 0:
        penalty = rule.default_numeric() or 0.0
        return penalty, rule.comment or f"报告中未找到学号，{penalty:+g}分。"
    if UNIVERSITY_NAME.search(text[:pos]):
        return 0.0, "报告使用指定模板，不扣分。"
    penalty = rule.default_numeric() or 0.0
    return penalty, rule.comment or f"报告未使用指定模板，{penalty:+g}分。"


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


# 独立标题行前缀：可选的章节编号、列表标记等
SECTION_PREFIX = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百\d]+[章节部分]?\s*[、.．,，:：]?\s*"
    r"|[一二三四五六七八九十百]+[章节部分]?\s*[、.．,，:：]?\s*"
    r"|\d+(?:\.\d+)*\s*[、.．,，:：)]?\s*"
    r"|[（(][一二三四五六七八九十百\d]+[)）]\s*"
    r"|[-*+·•]\s*"
    r")"
)
CATALOGUE_LINE = re.compile(r"目录(?:[（(][^）)]*[)）])?[:：]?")
# “参考文献”类：命中给该项满分
REFERENCE_FULL = re.compile(
    r"(?:参考文献|references?|bibliography|workscited)"
    r"(?:[（(][^）)]*[)）])?[:：]?",
    re.IGNORECASE,
)
# “参考论文”“参考资料”类：命中仅给 50% 分数
REFERENCE_HALF = re.compile(
    r"(?:参考论文|参考资料)"
    r"(?:[（(][^）)]*[)）])?[:：]?",
    re.IGNORECASE,
)


def has_catalogue(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """包含目录：报告前半部分存在单独成段/成行的“目录”。

    “目录”以标题形式出现给该项满分；非标题形式但独行出现给 70%。
    忽略行首尾及字符间的 1 个或多个空格（含全角空格），不要求标题或列表格式。
    """
    text = report.md_text
    half = max(1, len(text) // 2)
    offset = 0
    base_score = rule.default_numeric() or 0.0
    best_score = 0.0
    best_is_title = False
    for raw_line in text.splitlines(keepends=True):
        if offset >= half:
            break
        is_catalogue, is_title = _catalogue_line_info(raw_line)
        if not is_catalogue:
            offset += len(raw_line)
            continue
        multiplier = 1.0 if is_title else 0.7
        score = base_score * multiplier
        if score > best_score:
            best_score = score
            best_is_title = is_title
        offset += len(raw_line)
    if best_score > 0:
        best_score = round(best_score, 2)
        if best_is_title:
            comment = rule.comment or f"报告包含目录章节，+{best_score:g}分。"
        else:
            comment = rule.comment or f"报告包含目录（非标题形式），+{best_score:g}分。"
        return best_score, comment
    return 0.0, "报告未检测到目录章节，不加分。"


def _catalogue_line_info(raw_line: str) -> tuple[bool, bool]:
    """判断一行是否为单独的“目录”及其是否为标题形式。

    返回 (is_catalogue, is_title)：
    - is_catalogue: 行文去掉标记/空白后是否为“目录”
    - is_title: 是否为标题形式（Markdown 标题或带章节编号/列表标记的前缀）
    忽略标题/列表标记、强调符和空白。
    """
    is_catalogue = bool(CATALOGUE_LINE.fullmatch(_normalise_title_line(raw_line)))
    if not is_catalogue:
        return False, False
    return True, _is_title_line(raw_line)


def _plain_text(line: str) -> str:
    """去掉 Markdown 标题标记与强调符号，返回行内纯文本。"""
    text = re.sub(r"^#{1,6}\s*", "", line.strip())
    return text.replace("*", "").replace("_", "").strip()


def _normalise_title_line(raw_line: str) -> str:
    """去掉标题/列表标记、强调符和所有空白，返回用于比对的行文本。"""
    line = _plain_text(raw_line).strip("#")
    line = SECTION_PREFIX.sub("", line)
    line = line.replace("~", "").replace("`", "")
    return re.sub(r"\s+", "", line)


def _is_title_line(raw_line: str) -> bool:
    """判断一行是否为标题形式。

    标题形式包括：Markdown 一级至六级标题（# ## 等），或
    带章节编号/列表标记的前缀（如 六、 / 1. / - / * 等）。
    """
    stripped = raw_line.strip()
    if not stripped:
        return False
    if re.match(r"^#{1,6}\s", stripped):
        return True
    if SECTION_PREFIX.match(stripped):
        return True
    return False


def _reference_line_info(raw_line: str) -> tuple[str | None, bool]:
    """判断单独成行的参考文献类章节及其是否为标题形式。

    返回 (kind, is_title)：
    - kind: "full" 表示“参考文献”等；"half" 表示“参考论文”“参考资料”；None 表示不匹配
    - is_title: 是否为标题形式
    忽略标题/列表标记、强调符和空白。
    """
    norm = _normalise_title_line(raw_line)
    if REFERENCE_FULL.fullmatch(norm):
        kind = "full"
    elif REFERENCE_HALF.fullmatch(norm):
        kind = "half"
    else:
        return None, False
    return kind, _is_title_line(raw_line)


def has_reference(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """包含参考文献：全文存在单独成段/成行的“参考文献”。

    “参考文献”以标题形式出现给该项满分；非标题形式但独行出现给 70%。
    “参考论文”“参考资料”再在此基础上乘 70%（标题形式 70%、非标题形式 49%）。
    全文取最高分；若未检测到，不加分。
    忽略行首尾及字符间的 1 个或多个空格（含全角空格），不要求标题或列表格式。
    """
    base_score = rule.default_numeric() or 0.0
    best_score = 0.0
    best_kind: str | None = None
    best_is_title = False
    for line in report.md_text.splitlines():
        kind, is_title = _reference_line_info(line)
        if kind is None:
            continue
        multiplier = 1.0 if is_title else 0.7
        if kind == "half":
            multiplier *= 0.7
        score = base_score * multiplier
        if score > best_score:
            best_score = score
            best_kind = kind
            best_is_title = is_title
    if best_score > 0:
        best_score = round(best_score, 2)
        if best_kind == "full":
            if best_is_title:
                comment = rule.comment or f"报告包含参考文献章节，+{best_score:g}分。"
            else:
                comment = rule.comment or f"报告包含参考文献（非标题形式），+{best_score:g}分。"
        else:
            if best_is_title:
                comment = rule.comment or f"报告包含参考论文/参考资料章节，+{best_score:g}分。"
            else:
                comment = rule.comment or f"报告包含参考论文/参考资料（非标题形式），+{best_score:g}分。"
        return best_score, comment
    return 0.0, "报告未检测到参考文献章节，不加分。"

