"""阶段共享小工具与日志器。

三个阶段的 mixin 共用同一 logger 名称 ``gradehelper.grader``：
``RunLogMixin._append_stage_event`` 按宿主类的模块名取 logger，而 Grader 类仍定义在
``gradehelper.grader``，这样阶段日志/控制台输出与拆分前保持一致。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import ItemScore, ParsedReport, RuleItem
from ..parser import select_paths

logger = logging.getLogger("gradehelper.grader")


def _llm_item_scored(item: ItemScore | None) -> bool:
    """LLM 评分项是否已有分数：结果表中该单元格为空即视为未评分。"""
    return item is not None and item.score is not None


def _report_needs_llm_grading(student_id: str,
                              existing_scores: dict[str, dict[str, ItemScore]],
                              llm_rules: list[RuleItem]) -> bool:
    """只要有一项 LLM 评分项未评分，就需要对该报告重新评分。"""
    row = existing_scores.get(student_id) or {}
    return any(not _llm_item_scored(row.get(rule.key)) for rule in llm_rules)


def _run_mode_label(incremental: bool, files: list[str]) -> str:
    """阶段配置的「模式」：范围（全部/指定）× 方式（全量/增量）。"""
    return ("指定" if files else "全部") + ("增量" if incremental else "全量")


def _files_label(files: list[str]) -> str:
    """阶段配置的「指定文件」：文件数与名单（名单过长时只列前 10 个）。"""
    shown = files[:10]
    text = "、".join(shown)
    if len(files) > len(shown):
        text += f" 等 {len(files)} 个"
    return f"{len(files)} 个（{text}）"


def _select_reports(reports: list[ParsedReport],
                    names: list[str]) -> tuple[list[ParsedReport], list[str]]:
    """按 ``--files`` 名单筛选已加载的报告（主名或完整 md 名，忽略大小写）。

    返回 ``(命中的报告, 未命中的名字)``；``names`` 为空表示不过滤。
    """
    if not names:
        return list(reports), []
    matched, unmatched = select_paths(
        [Path(report.source_path) for report in reports], names)
    allowed = {str(path) for path in matched}
    return ([report for report in reports if str(Path(report.source_path)) in allowed],
            unmatched)
