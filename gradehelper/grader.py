"""总编排：doc(解析+程序规则) → pic(vllm识图) → sen(llm语义评分)，可独立执行。"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .config import AppConfig
from .excel_io import read_result_scores, read_rules, write_results
from .image_caption import caption_images, save_captions_txt
from .llm_client import LLMClient
from .models import ItemScore, ParsedReport, ReportImage, RuleItem
from .parser import embed_captions, parse_all
from .rules_engine import run_program_rules

logger = logging.getLogger(__name__)


class Grader:
    def __init__(self, config: AppConfig):
        self.config = config

    # ------------------------------------------------------------------ 阶段 1：doc（解析 + 程序规则打分）

    def stage_doc(self, reports_dir: str | Path, template: str | Path,
                  skip_images: int = 2) -> None:
        """阶段 doc：解析所有文档 + 程序化规则打分，写入 result 表（LLM 项先写 0）。"""
        cfg = self.config
        reports_dir = Path(reports_dir)
        t0 = time.perf_counter()

        logger.info("【阶段 doc】解析报告目录: %s", reports_dir)
        reports = parse_all(reports_dir)
        if not reports:
            raise RuntimeError(f"目录中没有符合命名格式的报告: {reports_dir}")

        # 移除被 skip 的图片（不保存图片文件）及其占位符，保持剩余图片原始编号
        for report in reports:
            before = len(report.images)
            report.images = [img for img in report.images if img.index > skip_images]
            for skip_idx in range(1, skip_images + 1):
                report.md_text = report.md_text.replace(f"[[PIC:P{skip_idx}]]", "")
            logger.info("  已解析 %s（图片 %d 张，跳过前 %d 张，剩余 %d 张）",
                        report.base_name, before, skip_images, len(report.images))

        # 始终保存解析产物（md 与图片）到与原文件相同路径
        self._save_parsed(reports)

        # 读取评分要求（rule 表）
        logger.info("【阶段 doc】读取评分模板 rule 表: %s", template)
        rule_items = read_rules(template)

        # 程序化 rule 项逐项批处理
        logger.info("【阶段 doc】程序化规则评分（逐 rule 批处理）")
        scores, warnings = run_program_rules(reports, rule_items)
        for w in warnings:
            logger.warning("  %s", w)

        # LLM 项分数先写 0，sen 阶段再更新
        llm_rule_keys = [r.key for r in rule_items if r.is_llm]
        for sid_items in scores.values():
            for key in llm_rule_keys:
                sid_items[key] = ItemScore(key=key, score=0.0, confidence=None, comment="")

        # 写入 result 表
        write_results(template, template, reports, rule_items, scores)
        elapsed = time.perf_counter() - t0
        logger.info("【阶段 doc】完成，程序化规则分数已写入 result 表（LLM 项已预置 0），耗时 %.2f 秒", elapsed)

    # ------------------------------------------------------------------ 阶段 2：pic（vllm 图片识别 + 更新 md）

    def stage_pic(self, reports_dir: str | Path, template: str | Path,
                  skip_images: int = 2) -> None:
        """阶段 pic：vllm 识别所有图片，将描述嵌入 md 文档并落盘。"""
        cfg = self.config
        reports_dir = Path(reports_dir)
        t0 = time.perf_counter()

        logger.info("【阶段 pic】从已保存的 md 与图片加载报告: %s", reports_dir)
        reports = self._load_reports_for_pic(reports_dir, skip_images)
        if not reports:
            raise RuntimeError(f"目录中没有找到已保存的 md 文件: {reports_dir}")

        llm_rules_flag = any(r.is_llm for r in read_rules(template))
        if llm_rules_flag:
            logger.info("【阶段 pic】vllm 批量识别图片")
            caption_images(reports, cfg.vllm, cfg.prompts.image_prompt)
            saved = save_captions_txt(reports, cfg.paths.captions_dir or None)
            logger.info("  图片描述已另存 %d 个 txt", saved)
        for report in reports:
            report.md_text = embed_captions(report.md_text, report.images,
                                            cfg.prompts.image_context)

        # 保存更新后的 md（带图片描述）
        self._save_parsed(reports)
        elapsed = time.perf_counter() - t0
        logger.info("【阶段 pic】完成，图片描述已嵌入 md 并落盘，耗时 %.2f 秒", elapsed)

    # ------------------------------------------------------------------ 阶段 3：sen（llm 语义评分 + 更新 result 表）

    def stage_sen(self, reports_dir: str | Path, template: str | Path,
                  output: str | Path, skip_images: int = 2) -> Path:
        """阶段 sen：llm 语义评分，读取 doc 阶段已写入的程序化分数，写入最终 result 表。"""
        cfg = self.config
        reports_dir = Path(reports_dir)
        t0 = time.perf_counter()

        # 读取 result 表中已存在的所有评分（不限 LLM 项，避免覆盖程序化规则分数）
        rule_items = read_rules(template)
        llm_rule_keys = [r.key for r in rule_items if r.is_llm]
        existing_scores = read_result_scores(template)  # 读取全部评分列

        # 从磁盘读取已更新的 md（pic 阶段保存的）
        reports = self._load_reports_for_pic(reports_dir, skip_images)

        if not llm_rule_keys:
            logger.info("【阶段 sen】无 LLM 评分项，沿用已有分数")
            final_scores: dict[str, dict[str, ItemScore]] = {}
            for report in reports:
                sid = report.student_id
                row: dict[str, ItemScore] = {}
                for rule in rule_items:
                    prev = existing_scores.get(sid, {}).get(rule.key, 0.0)
                    row[rule.key] = ItemScore(key=rule.key, score=prev,
                                              confidence=None, comment="")
                final_scores[sid] = row
        else:
            logger.info("【阶段 sen】LLM 评分项评估（逐文件 %d 项）", len(llm_rule_keys))
            client = LLMClient(cfg.llm)
            final_scores = {}
            total = len(reports)
            for idx, report in enumerate(reports, 1):
                sid = report.student_id
                row: dict[str, ItemScore] = {}
                # 先把程序化规则分数填进来
                for rule in rule_items:
                    if not rule.is_llm:
                        prev = existing_scores.get(sid, {}).get(rule.key, 0.0)
                        row[rule.key] = ItemScore(key=rule.key, score=prev, comment="")
                # 再逐项 LLM 评分
                for rule in rule_items:
                    if rule.is_llm:
                        row[rule.key] = self._grade_with_llm(client, report, rule)
                final_scores[sid] = row
                logger.info("【阶段 sen】%d/%d, ID %s：", idx, total, sid)

        # 汇总写入 result 表
        out = write_results(template, output, reports, rule_items, final_scores)
        elapsed = time.perf_counter() - t0
        logger.info("【阶段 sen】完成，最终评分已写入: %s，耗时 %.2f 秒", out, elapsed)
        return out

    # ------------------------------------------------------------------ 总流程调度

    def run(self, reports_dir: str | None = None, template: str | None = None,
            output: str | None = None, stage: str = "full",
            skip_images: int = 2) -> Path | None:
        cfg = self.config
        reports_dir = reports_dir or cfg.paths.reports_dir
        template = template or cfg.paths.template
        output = output or cfg.paths.output or template

        if stage == "full":
            self.stage_doc(reports_dir, template, skip_images)
            self.stage_pic(reports_dir, template, skip_images)
            return self.stage_sen(reports_dir, template, output, skip_images)

        if stage == "doc":
            self.stage_doc(reports_dir, template, skip_images)
            return None

        if stage == "pic":
            self.stage_pic(reports_dir, template, skip_images)
            return None

        if stage == "sen":
            return self.stage_sen(reports_dir, template, output, skip_images)

        raise ValueError(f"未知的 stage 参数: {stage}")

    # ------------------------------------------------------------------ 从磁盘加载报告（供 pic 阶段使用，不重新解析 word）

    PIC_PATTERN = re.compile(r"\[\[PIC:P(\d+)\]\]")

    def _load_reports_for_pic(self, reports_dir: Path, skip_images: int) -> list[ParsedReport]:
        """从 doc 阶段已保存的 md 与图片文件加载报告，不重新解析 word。

        扫描目录下所有 *.md 文件，从文件名解析元数据，读取 md 内容，
        再从 md 中提取 [[PIC:Pn]] 占位符，按 {base_name}-P{n}.{ext} 模式
        查找并读取对应图片文件。
        """
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
        reports: list[ParsedReport] = []
        for md_path in sorted(reports_dir.glob("*.md")):
            if md_path.name.startswith("~$"):
                continue
            try:
                # 从 md 文件名解析：课程名称-专业班级-学号-姓名.md
                parts = md_path.stem.rsplit("-", 3)
                if len(parts) != 4:
                    logger.warning("跳过不符合命名格式的 md 文件: %s", md_path.name)
                    continue
                course, klass, student_id, name = parts
                if not all(p.strip() for p in (course, klass, student_id, name)):
                    continue

                md_text = md_path.read_text(encoding="utf-8")
                base_name = md_path.stem

                # 从 md 中提取所有 [[PIC:Pn]] 占位符，确定图片索引
                pic_indices = sorted({int(m) for m in self.PIC_PATTERN.findall(md_text)})

                images: list[ReportImage] = []
                for pic_idx in pic_indices:
                    img_data = None
                    img_ext = None
                    for ext in IMAGE_EXTS:
                        candidate = md_path.parent / f"{base_name}-P{pic_idx}{ext}"
                        if candidate.is_file():
                            img_data = candidate.read_bytes()
                            img_ext = ext.lstrip(".")
                            break
                    if img_data is None:
                        logger.warning("未找到图片文件: %s-P%d.*", base_name, pic_idx)
                        continue
                    images.append(ReportImage(index=pic_idx, ext=img_ext, data=img_data))

                # 应用跳图策略：只保留 index > skip_images 的图片进行处理，
                # 但不改变图片原始编号（始终反映在文档中的实际位置）
                images = [img for img in images if img.index > skip_images]

                reports.append(ParsedReport(
                    course=course, klass=klass, student_id=student_id, name=name,
                    source_path=str(md_path), md_text=md_text, images=images,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.error("加载报告失败 %s: %s", md_path.name, exc)
        return reports

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