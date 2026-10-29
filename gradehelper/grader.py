"""总编排：解析 → 程序化规则批处理 → vllm 图片识别 → LLM 项评估 → 写 result 表。"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import AppConfig
from .excel_io import read_rules, write_results
from .image_caption import caption_images, save_captions_txt
from .llm_client import LLMClient
from .models import ItemScore, ParsedReport, RuleItem
from .parser import embed_captions, parse_all
from .rules_engine import run_program_rules

logger = logging.getLogger(__name__)


class Grader:
    def __init__(self, config: AppConfig):
        self.config = config

    # ------------------------------------------------------------------ 总流程

    def run(self, reports_dir: str | None = None, template: str | None = None,
            output: str | None = None, skip_llm: bool = False,
            skip_vllm: bool = False, skip_images: int = 2) -> Path:
        cfg = self.config
        reports_dir = reports_dir or cfg.paths.reports_dir
        template = template or cfg.paths.template
        output = output or cfg.paths.output or template

        # 1. 解析（md 与图片驻留内存）
        logger.info("【步骤 1/5】解析报告目录: %s", reports_dir)
        reports = parse_all(reports_dir)
        if not reports:
            raise RuntimeError(f"目录中没有符合命名格式的报告: {reports_dir}")

        # 跳过前面 skip_images 张图片，并重新编号
        for report in reports:
            before = len(report.images)
            report.images = report.images[skip_images:]
            for new_idx, img in enumerate(report.images, 1):
                img.index = new_idx
            logger.info("  已解析 %s（图片 %d 张，跳过前 %d 张，剩余 %d 张）",
                        report.base_name, before, skip_images, len(report.images))
        # 始终保存解析产物（md 与图片）到与原文件相同路径
        self._save_parsed(reports)

        # 2. 读取评分要求（rule 表）
        logger.info("【步骤 2/5】读取评分模板 rule 表: %s", template)
        rule_items = read_rules(template)

        # 3. 程序化 rule 项逐项批处理
        logger.info("【步骤 3/5】程序化规则评分（逐 rule 批处理）")
        scores, warnings = run_program_rules(reports, rule_items)
        for w in warnings:
            logger.warning("  %s", w)

        # 4. vllm 批量识别图片，描述嵌入 MD + 另存 Pn.txt
        llm_rules = [r for r in rule_items if r.is_llm]
        if skip_vllm:
            logger.info("【步骤 4/5】跳过 vllm 图片识别（--skip-vllm）")
        elif llm_rules:
            logger.info("【步骤 4/5】vllm 批量识别图片")
            caption_images(reports, cfg.vllm)
            saved = save_captions_txt(reports, cfg.paths.captions_dir or None)
            logger.info("  图片描述已另存 %d 个 txt", saved)
        for report in reports:
            report.md_text = embed_captions(report.md_text, report.images,
                                            cfg.prompts.image_context)

        # 5. LLM 相关项：逐文件、逐项单独 Prompt
        if skip_llm or not llm_rules:
            logger.info("【步骤 5/5】跳过 LLM 评分项")
        else:
            logger.info("【步骤 5/5】LLM 评分项评估（逐文件 %d 项）", len(llm_rules))
            client = LLMClient(cfg.llm)
            total = len(reports)
            for idx, report in enumerate(reports, 1):
                for rule in llm_rules:
                    scores[report.student_id][rule.key] = self._grade_with_llm(client, report, rule)
                logger.info("【步骤 5/5】%d/%d, ID %s：", idx, total, report.student_id)

        # 6. 汇总写入 result 表（总分列使用 Excel 公式）
        out = write_results(template, output, reports, rule_items, scores)
        return out

    # ------------------------------------------------------------------ LLM 单项评估

    def _grade_with_llm(self, client: LLMClient, report: ParsedReport, rule: RuleItem) -> ItemScore:
        """对单个文件的单个 LLM 评分项单独输入 Prompt。"""
        lo, hi = rule.score_range()
        score_range = f"{lo:g}-{hi:g}" if lo is not None and hi is not None else "按评分标准"
        fallback = lo if lo is not None else 0.0

        prompt = self.config.prompts.llm_item.format(
            key=rule.key, evaluation=rule.evaluation, score_range=score_range,
            report=report.md_text,
        )
        try:
            data = client.chat_json([{"role": "user", "content": prompt}])
            score = _to_float(data.get("score"))
            if score is None:
                raise ValueError(f"score 字段缺失或非法: {data}")
            confidence = _to_float(data.get("confidence"))
            comment = str(data.get("comment") or rule.comment or "").strip()
            logger.info("  %s / %s -> score=%s confidence=%s",
                        report.student_id, rule.key, score, confidence)
            return ItemScore(key=rule.key, score=score, confidence=confidence, comment=comment)
        except Exception as exc:  # noqa: BLE001 重试已在客户端内完成，此处降级
            logger.error("  LLM 评分失败 %s / %s: %s（使用默认值 %s）",
                         report.student_id, rule.key, exc, fallback)
            return ItemScore(key=rule.key, score=fallback, confidence=0.0,
                             comment=f"LLM 评估失败，使用默认值：{exc}")

    # ------------------------------------------------------------------ 可选落盘

    @staticmethod
    def _save_parsed(reports: list[ParsedReport]) -> None:
        """把解析出的 md 与图片落盘到与原文件相同路径。"""
        for report in reports:
            target_dir = Path(report.source_path).parent
            (target_dir / f"{report.base_name}.md").write_text(report.md_text, encoding="utf-8")
            for image in report.images:
                (target_dir / f"{report.base_name}-P{image.index}.{image.ext}").write_bytes(image.data)
        logger.info("解析产物（md/图片）已落盘 %d 份报告", len(reports))


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
