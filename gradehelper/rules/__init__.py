"""程序化规则实现包。

规则函数签名（单报告版）：
    handler(report: ParsedReport, rule: RuleItem) -> (score, comment)

批处理版（需要全部报告参与的规则，如 copied）：
    batch_handler(reports: list[ParsedReport], rule: RuleItem) -> dict[学号, (score, comment)]

在 RULE_HANDLERS / BATCH_RULE_HANDLERS 中按 rule 表『评分项』列的名称注册；
换模板时未注册的程序化规则会被跳过并告警，LLM 项走通用通道。
"""

from __future__ import annotations

from typing import Callable

from ..models import ParsedReport, RuleItem
from .content import has_requirement, summary_docker, summary_sql, too_many_list
from .similarity import copied
from .structure import base_score, has_catalogue, has_name_id, redundant_rname

# 单报告规则：评分项名 -> handler(report, rule) -> (score, comment)
RULE_HANDLERS: dict[str, Callable[[ParsedReport, RuleItem], tuple[float, str]]] = {
    "baseScore": base_score,
    "hasNameID": has_name_id,
    "redundantRName": redundant_rname,
    "tooManyList": too_many_list,
    "hasCatalogue": has_catalogue,
    "hasRequirement": has_requirement,
    "summaryDocker": summary_docker,
    "summarySql": summary_sql,
}

# 批处理规则（跨报告）：评分项名 -> handler(reports, rule) -> {学号: (score, comment)}
BATCH_RULE_HANDLERS: dict[str, Callable[..., dict[str, tuple[float, str]]]] = {
    "copied": copied,
}


def resolve(rule: RuleItem) -> str | None:
    """返回规则类型：'batch' / 'single' / None（无程序实现）。"""
    if rule.key in BATCH_RULE_HANDLERS:
        return "batch"
    if rule.key in RULE_HANDLERS:
        return "single"
    return None
