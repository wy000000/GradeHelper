"""相似度规则：copied（基于欧氏距离的相似度，判断是否完全复制）。"""

from __future__ import annotations

import math
import re
from collections import Counter

from ..models import ParsedReport, RuleItem

THRESHOLD = 0.90  # 相似度阈值


def _char_ngram_vector(text: str, n: int = 2) -> Counter:
    """字符 n-gram 词频向量（忽略空白）。"""
    compact = re.sub(r"\s+", "", text)
    return Counter(compact[i:i + n] for i in range(max(0, len(compact) - n + 1)))


def _l2_normalize(vec: Counter) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0:
        return {}
    return {k: v / norm for k, v in vec.items()}


def euclidean_similarity(text_a: str, text_b: str, n: int = 2) -> float:
    """基于欧氏距离的相似度：对 L2 归一化后的字符 n-gram 向量计算 1/(1+d)。

    相同文本 -> 1.0；欧氏距离对长度敏感，适合判断“完全复制”。
    """
    vec_a = _l2_normalize(_char_ngram_vector(text_a, n))
    vec_b = _l2_normalize(_char_ngram_vector(text_b, n))
    keys = set(vec_a) | set(vec_b)
    dist = math.sqrt(sum((vec_a.get(k, 0.0) - vec_b.get(k, 0.0)) ** 2 for k in keys))
    return 1.0 / (1.0 + dist)


def copied(reports: list[ParsedReport], rule: RuleItem) -> dict[str, tuple[float, str]]:
    """批处理规则：两两比较所有报告，相似度 >= 90% 的各扣分并互相注明学号。"""
    penalty = rule.default_numeric() or 0.0
    results: dict[str, tuple[float, str]] = {}
    suspects: dict[str, set[str]] = {r.student_id: set() for r in reports}
    max_sim: dict[str, float] = {r.student_id: 0.0 for r in reports}

    for i, a in enumerate(reports):
        for b in reports[i + 1:]:
            sim = euclidean_similarity(a.md_text, b.md_text)
            if sim >= THRESHOLD:
                suspects[a.student_id].add(b.student_id)
                suspects[b.student_id].add(a.student_id)
            max_sim[a.student_id] = max(max_sim[a.student_id], sim)
            max_sim[b.student_id] = max(max_sim[b.student_id], sim)

    for report in reports:
        sid = report.student_id
        if suspects[sid]:
            others = "、".join(sorted(suspects[sid]))
            template = rule.comment or "高度雷同。基于欧氏距离的相似度>=90%。{penalty}分。相似学号：{others}"
            comment = template.replace("{penalty}", f"{penalty:g}")
            if "{others}" in comment:
                comment = comment.replace("{others}", others)
            elif "xxxx" in comment:
                comment = comment.replace("xxxx", others)
            else:
                comment = f"{comment} 相似学号：{others}。"
            results[sid] = (penalty, comment)
        else:
            results[sid] = (0.0, f"与其他报告最高相似度 {max_sim[sid]:.0%}，未达 90%，不扣分。")
    return results
