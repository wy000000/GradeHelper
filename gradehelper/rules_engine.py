"""规则引擎：rule 表驱动的分派与批处理执行。

按规划文档要求：逐次对单个 rule 评分项进行批处理（外层 rule、内层报告），
而不是对单文件的所有 rule 评分项批处理。
"""

from __future__ import annotations

import logging

from .models import ItemScore, ParsedReport, RuleItem
from . import rules as rules_pkg

logger = logging.getLogger(__name__)


def run_program_rules(reports: list[ParsedReport], rule_items: list[RuleItem],
                      ) -> tuple[dict[str, dict[str, ItemScore]], list[str]]:
    """执行所有『处理方法=rule』的程序化评分项。

    返回 (scores: 学号->评分项->ItemScore, warnings)。
    """
    program_rules = [r for r in rule_items if not r.is_llm]
    scores: dict[str, dict[str, ItemScore]] = {r.student_id: {} for r in reports}
    warnings: list[str] = []

    for rule in program_rules:                      # 外层：单个 rule 项
        kind = rules_pkg.resolve(rule)
        if kind is None:
            msg = f"评分项 '{rule.key}' 处理方法为 rule，但没有注册对应的程序实现，已跳过"
            logger.warning(msg)
            warnings.append(msg)
            continue

        if kind == "batch":                          # 跨报告批处理（如 copied）
            try:
                outcome = rules_pkg.BATCH_RULE_HANDLERS[rule.key](reports, rule)
            except Exception as exc:                 # noqa: BLE001 单项失败不中断
                logger.error("规则 %s 批处理失败: %s", rule.key, exc)
                warnings.append(f"规则 {rule.key} 执行失败: {exc}")
                continue
            for sid, (score, comment) in outcome.items():
                if sid in scores:
                    scores[sid][rule.key] = ItemScore(key=rule.key, score=score, comment=comment)
            continue

        # 内层：遍历所有报告（单 rule 的全量批处理）
        for report in reports:                       # noqa: B007 报告顺序即批处理顺序
            try:
                score, comment = rules_pkg.RULE_HANDLERS[rule.key](report, rule)
            except Exception as exc:                 # noqa: BLE001
                logger.error("规则 %s 对 %s 执行失败: %s", rule.key, report.student_id, exc)
                warnings.append(f"规则 {rule.key} 对学号 {report.student_id} 执行失败: {exc}")
                continue
            scores[report.student_id][rule.key] = ItemScore(key=rule.key, score=score, comment=comment)

    return scores, warnings
