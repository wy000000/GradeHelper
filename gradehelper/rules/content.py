"""内容规则：tooManyList / hasRequirement / summaryDocker / summarySql。"""

from __future__ import annotations

import difflib
import re

from ..models import ParsedReport, RuleItem

CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")
LIST_LINE = re.compile(r"^\s*([-*·•]|\d+[.、)]|[（(]\d+[)）])\s*\S")

# 模板中的题目要求（评估方法列给出的原文）
REQUIREMENT_TEXT = (
    "1、针对某一云安全问题进行论述，叙述其产生的原因，带来的危害，解决的方法。\n"
    "2、对本门课程的总结，包括收益、建议等。"
)


def strip_code(md: str) -> str:
    """分离程序代码内容：移除代码块与行内代码。"""
    return INLINE_CODE.sub("", CODE_FENCE.sub("", md))


def is_list_line(line: str) -> bool:
    return bool(LIST_LINE.match(line))


def too_many_list(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """List 滥用：非代码 List 内容长度超过非代码正文段落内容长度则扣分。"""
    body = strip_code(report.md_text)
    list_chars = 0
    para_chars = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if is_list_line(stripped):
            list_chars += len(re.sub(r"^\s*([-*·•]|\d+[.、)]|[（(]\d+[)）])\s*", "", stripped))
        else:
            para_chars += len(stripped)
    if para_chars > 0 and list_chars > para_chars:
        penalty = rule.default_numeric() or 0.0
        return penalty, (rule.comment or f"List 内容长度({list_chars})超过正文段落长度({para_chars})，"
                                         f"{penalty:+g}分。")
    return 0.0, f"List 使用正常（List {list_chars} 字，正文 {para_chars} 字），不扣分。"


def has_requirement(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """包含题目：报告前半部分需包含与题目要求相似度 >=90% 的文本。

    “前半部分”按文本起始位置判断：匹配窗口的起点须落在全文前 50%。
    """
    text = strip_code(report.md_text)
    requirement = report_requirement(rule, text)
    if window_similarity(text, requirement, threshold=0.9, max_start_ratio=0.5):
        return 0.0, "报告前半部分包含题目要求原文，不扣分。"
    penalty = rule.default_numeric() or 0.0
    return penalty, rule.comment or f"未按要求写明题目要求，{penalty:+g}分。"


def report_requirement(rule: RuleItem, _first_half: str) -> str:
    """从 rule 的评估方法中提取题目要求文本；提取失败用内置默认。"""
    text = rule.evaluation
    match = re.search(r"[“\"](.+?)[”\"]", text, re.DOTALL)
    candidate = match.group(1) if match else ""
    candidate = re.sub(r"\s*如果不包含.*$", "", candidate).strip()
    return candidate if len(candidate) >= 10 else REQUIREMENT_TEXT


def window_similarity(text: str, pattern: str, threshold: float = 0.9,
                      max_start_ratio: float = 0.5) -> bool:
    """text 中是否存在长度相近的滑动窗口，与 pattern 的相似度 >= threshold。

    max_start_ratio 限定窗口起点须落在文本前该比例内（用于“报告前半部分”语义）。
    """
    pat = re.sub(r"\s+", "", pattern)
    if not pat:
        return False
    compact = re.sub(r"\s+", "", text)
    if pat in compact:
        return True
    n, m = len(compact), len(pat)
    if n <= m:
        return difflib.SequenceMatcher(None, compact, pat).ratio() >= threshold
    max_start = max(0, int(n * max_start_ratio))
    step = max(1, m // 10)
    for start in range(0, min(n - m + 1, max_start + 1), step):
        window = compact[start:start + m]
        if difflib.SequenceMatcher(None, window, pat).ratio() >= threshold:
            return True
    return False


def _last_section(report: ParsedReport, ratio: float = 0.2) -> str:
    """最后总结部分：正文末尾 ratio 比例，或从“总结”类标题开始。"""
    text = strip_code(report.md_text)
    tail = text[int(len(text) * (1 - ratio)):]
    # 若末尾区域存在总结类标题，从最后一个标题处截取
    matches = list(re.finditer(r"^#{1,4}.*?(总结|结语|体会|收获|感悟).*$", text, re.MULTILINE))
    if matches:
        tail = text[matches[-1].start():]
    return tail


def summary_docker(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """最后总结部分包含 docker 则加分。"""
    tail = _last_section(report)
    if re.search(r"docker", tail, re.IGNORECASE):
        score = rule.default_numeric() or 0.0
        return score, rule.comment or f"总结部分涉及重点内容 docker，+{score:g}分。"
    return 0.0, "总结部分未涉及 docker，不加分。"


def summary_sql(report: ParsedReport, rule: RuleItem) -> tuple[float, str]:
    """最后总结部分包含 NoSQL/MongoDB/SQLite/hbase 任意一项则加分。"""
    tail = _last_section(report)
    hit = [kw for kw in ("NoSQL", "MongoDB", "SQLite", "HBase", "hbase") if kw.lower() in tail.lower()]
    if hit:
        score = rule.default_numeric() or 0.0
        return score, rule.comment or f"总结部分涉及重点内容{hit[0]}，+{score:g}分。"
    return 0.0, "总结部分未涉及 NoSQL 相关内容，不加分。"