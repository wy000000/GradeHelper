"""LLM 评分：评分项列表、批量 Prompt 渲染与单份报告批量评分。"""

from __future__ import annotations

import logging

from .llm_client import LLMClient, parse_json_object
from .models import ItemScore, ParsedReport, RuleItem

logger = logging.getLogger(__name__)


class LLMContentError(ValueError):
    """LLM 返回的 JSON 数据不完整（能解析但缺项/字段无效）。

    ``str(self)`` 只保留简短原因，供主日志/控制台输出；
    ``self.raw_text`` 保留完整原始文本，供附属详情日志使用。
    """

    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


def grade_with_llm_batch(client: LLMClient, report: ParsedReport,
                         llm_rules: list[RuleItem], batch_prompt: str) -> dict[str, ItemScore]:
    """对单个文件的所有 LLM 评分项一次性提交，返回 {key: ItemScore}。

    失败不自行捕获，异常向上抛出。client.chat 内部已按 cfg.max_retries
    重试，耗尽后会抛出 RuntimeError，由 stage_sen / main 决定退出程序。

    JSON 不完整（被截断）一律视为该文档**处理失败**：解析失败、缺少评分项、
    某一项不是对象、score 缺失或不是数字，都抛 ValueError 交给批处理入队重试，
    不做“取最低分”之类的静默兜底，避免失败文档被当成评分成功写进结果表。

    解析失败抛 JsonParseError、数据不完整抛 LLMContentError：两者都携带
    ``raw_text``（LLM 完整原始输出），附属详情日志据此保存非截断的全部信息。
    """
    items_text = build_llm_items_section(llm_rules)
    prompt = render_llm_batch_prompt(
        batch_prompt, items_text, report.md_text)
    text = client.chat([{"role": "user", "content": prompt}])
    data = parse_json_object(text)

    if not isinstance(data, dict):
        raise LLMContentError(
            f"LLM 返回的 JSON 不是对象: {type(data).__name__}", raw_text=text)
    missing = [rule.key for rule in llm_rules if rule.key not in data]
    if missing:
        raise LLMContentError(
            "LLM 返回的 JSON 不完整（可能被截断），缺少评分项: " + "、".join(missing),
            raw_text=text)

    result: dict[str, ItemScore] = {}
    invalid: list[str] = []
    for rule in llm_rules:
        entry = data.get(rule.key)
        if not isinstance(entry, dict):
            invalid.append(f"{rule.key}（返回的不是对象: {type(entry).__name__}）")
            continue
        score = to_float(entry.get("score"))
        if score is None:
            invalid.append(f"{rule.key}（score 缺失或不是数字）")
            continue
        confidence = to_float(entry.get("confidence"))
        comment = str(entry.get("comment") or rule.comment or "").strip()
        result[rule.key] = ItemScore(key=rule.key, score=score,
                                     confidence=confidence, comment=comment)
        logger.debug("  %s / %s -> score=%s confidence=%s (batch)",
                     report.student_id, rule.key, score, confidence)
    if invalid:
        raise LLMContentError(
            "LLM 返回的 JSON 数据不完整（可能被截断），视为处理失败: "
            + "、".join(invalid),
            raw_text=text)
    return result


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_llm_items_section(llm_rules: list[RuleItem]) -> str:
    """评分项列表：每行“- 评分项编号；分数范围；评分标准”。"""
    lines: list[str] = []
    for rule in llm_rules:
        lo, hi = rule.score_range()
        score_range = f"{lo:g}-{hi:g}" if lo is not None and hi is not None else "按评分标准"
        lines.append(f"- {rule.key}；{score_range}；{rule.evaluation}")
    return "\n".join(lines)


def render_llm_batch_prompt(template: str, items_section: str, report_md: str) -> str:
    """渲染 llm_batch Prompt，只替换约定的 {items_section} 与 {report} 占位符。

    模板中的其他花括号（如输出示例 {"score": ...}）必须原样保留，
    因此不能使用 str.format()；先替换 {items_section} 再替换 {report}，
    避免报告正文中的字面占位符被二次替换。
    """
    return (template
            .replace("{items_section}", items_section)
            .replace("{report}", report_md))
