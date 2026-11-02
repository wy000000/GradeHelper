"""总编排：doc(解析+程序规则) → pic(vllm识图) → sen(llm语义评分)，可独立执行。"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .excel_io import (
    read_result_scores, read_rules, write_results, build_expected_headers, RESULT_SHEET
)
from openpyxl import load_workbook
from .image_caption import caption_images, save_captions_txt
from .llm_client import LLMClient
from .models import ItemScore, ParsedReport, ReportImage, RuleItem
from .parser import embed_captions, parse_all
from .rules_engine import run_program_rules

logger = logging.getLogger(__name__)


@dataclass
class StageLogEntry:
    """单个阶段的运行日志记录。"""

    stage: str                      # 阶段名：doc / pic / sen
    start_time: str                 # 开始时间 "YYYY-MM-DD HH:MM:SS"
    end_time: str                   # 结束时间
    duration_seconds: float         # 用时（秒）
    processed_count: int            # 共处理多少份
    start_student_id: str | None    # 开始学号（第一份报告的学号）
    end_student_id: str | None      # 结束学号（最后一份报告的学号）
    error: str | None = None        # 出错原因（若无则为 None）


class Grader:
    def __init__(self, config: AppConfig):
        self.config = config
        self.stage_logs: list[StageLogEntry] = []
        self._log_path: Path | None = None

    def _resolve_log_path(self, template: str | Path) -> Path:
        """确定运行日志文件路径。

        - 若 config.paths.log_path 非空，直接使用；
        - 否则与模板文件同目录，文件名为 <模板名>.log。
        """
        cfg = self.config
        log_path = (cfg.paths.log_path or "").strip()
        if log_path:
            return Path(log_path)
        return Path(str(template) + ".log")

    def _write_run_log(self) -> None:
        """把所有阶段的运行日志写入日志文件。"""
        if not self._log_path:
            return
        log_path = Path(self._log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("GradeHelper 运行日志")
        lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 60)
        lines.append("")

        for entry in self.stage_logs:
            lines.append(f"阶段: {entry.stage}")
            lines.append(f"开始时间: {entry.start_time}")
            lines.append(f"结束时间: {entry.end_time}")
            lines.append(f"用时: {entry.duration_seconds:.2f} 秒")
            lines.append(f"共处理: {entry.processed_count} 份")
            if entry.start_student_id:
                lines.append(f"开始学号: {entry.start_student_id}")
            if entry.end_student_id:
                lines.append(f"结束学号: {entry.end_student_id}")
            if entry.error:
                lines.append(f"状态: 失败 - {entry.error}")
            else:
                lines.append("状态: 成功")
            lines.append("-" * 60)
            lines.append("")

        log_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("运行日志已写入: %s", log_path)

    # ------------------------------------------------------------------ 结果文件路径

    def _resolve_result_path(self, template: str | Path,
                             reports: list[ParsedReport],
                             output: str | Path | None = None) -> Path:
        """确定评分结果 xlsx 路径（不写模板文件）。

        - 显式 output（--output / paths.output）优先；
        - 否则按 config.yaml 的 paths.result_name_format 生成文件名，落在与模板相同目录；
          占位符：{course} {tagDep} {reportName}，取自第一份报告（同批次应一致）。
        """
        if output:
            return Path(output)
        fmt = (self.config.paths.result_name_format or "").strip()
        if not fmt:
            raise ValueError("config.yaml 缺少 paths.result_name_format 配置，且未指定 --output")
        if not reports:
            raise ValueError("无报告可用于生成结果文件名（或请显式指定 --output）")
        first = reports[0]
        courses = {r.course for r in reports}
        report_names = {r.report_name for r in reports}
        if len(courses) > 1 or len(report_names) > 1:
            logger.warning("报告目录包含多个课程/报告类型，结果文件名取第一份: %s", first.base_name)
        name = fmt.format(course=first.course, tagDep=first.tag_dep,
                          reportName=first.report_name)
        # 禁止路径分隔符等非法文件名字符，确保结果文件与模板同目录
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        return Path(template).parent / name

    # ------------------------------------------------------------------ 阶段 1：doc（解析 + 程序规则打分）

    def stage_doc(self, reports_dir: str | Path, template: str | Path,
                  skip_images: int = 2, tag_dep: str = "系部专业",
                  output: str | Path | None = None) -> Path:
        """阶段 doc：解析所有文档 + 程序化规则打分，写入独立结果文件的 result 表（LLM 项先写 0）。"""
        cfg = self.config
        reports_dir = Path(reports_dir)
        t0 = time.perf_counter()
        start_dt = datetime.now()
        reports: list[ParsedReport] = []
        error: str | None = None
        try:
            logger.info("【阶段 doc】解析报告目录: %s", reports_dir)
            reports = parse_all(
            reports_dir,
            name_format=cfg.paths.report_name_format,
            separator=cfg.paths.report_name_separator,
            tag_dep=tag_dep,
        )
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

            # 写入独立结果文件的 result 表（按学号匹配：存在则更新，不存在则添加；不修改模板）
            result_path = self._resolve_result_path(template, reports, output)
            write_results(result_path, reports, rule_items, scores, mode="upsert")
            logger.info("【阶段 doc】完成，程序化规则分数已写入 %s（LLM 项已预置 0），耗时 %.2f 秒",
                        result_path, time.perf_counter() - t0)
            return result_path
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            start_sid = reports[0].student_id if reports else None
            end_sid = reports[-1].student_id if reports else None
            self.stage_logs.append(StageLogEntry(
                stage="doc",
                start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                duration_seconds=round(elapsed, 2),
                processed_count=len(reports),
                start_student_id=start_sid,
                end_student_id=end_sid,
                error=error,
            ))

    # ------------------------------------------------------------------ 阶段 2：pic（vllm 图片识别 + 更新 md）

    def stage_pic(self, reports_dir: str | Path, template: str | Path,
                  skip_images: int = 2, tag_dep: str = "系部专业") -> None:
        """阶段 pic：vllm 识别所有图片，将描述嵌入 md 文档并落盘。"""
        cfg = self.config
        reports_dir = Path(reports_dir)
        t0 = time.perf_counter()
        start_dt = datetime.now()
        reports: list[ParsedReport] = []
        error: str | None = None
        try:
            logger.info("【阶段 pic】从已保存的 md 与图片加载报告: %s", reports_dir)
            reports = self._load_reports_for_pic(reports_dir, skip_images, tag_dep=tag_dep)
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
                                                cfg.prompts.image_context,
                                                base_name=report.base_name)

            # 保存更新后的 md（带图片描述）
            self._save_parsed(reports)
            logger.info("【阶段 pic】完成，图片描述已嵌入 md 并落盘，耗时 %.2f 秒",
                        time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            start_sid = reports[0].student_id if reports else None
            end_sid = reports[-1].student_id if reports else None
            self.stage_logs.append(StageLogEntry(
                stage="pic",
                start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                duration_seconds=round(elapsed, 2),
                processed_count=len(reports),
                start_student_id=start_sid,
                end_student_id=end_sid,
                error=error,
            ))

    # ------------------------------------------------------------------ 阶段 3：sen（llm 语义评分 + 更新 result 表）

    def stage_sen(self, reports_dir: str | Path, template: str | Path,
                  output: str | Path | None = None, skip_images: int = 2,
                  tag_dep: str = "系部专业") -> Path:
        """阶段 sen：llm 语义评分（单文件批量），写入独立结果文件的 result 表。"""
        cfg = self.config
        reports_dir = Path(reports_dir)
        t0 = time.perf_counter()
        start_dt = datetime.now()
        reports: list[ParsedReport] = []
        error: str | None = None
        try:
            # 读取评分要求（rule 表仍来自模板）
            rule_items = read_rules(template)
            llm_rules = [r for r in rule_items if r.is_llm]

            # 从磁盘读取已更新的 md（pic 阶段保存的）
            reports = self._load_reports_for_pic(reports_dir, skip_images, tag_dep=tag_dep)

            # 独立结果文件路径（与 doc 阶段同一解析逻辑，读写同一文件）
            result_path = self._resolve_result_path(template, reports, output)

            # 读取结果文件中已存在的所有评分（不限 LLM 项，避免覆盖程序化规则分数）
            existing_scores = read_result_scores(result_path)  # 读取全部评分列

            if not llm_rules:
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
                # sen 阶段前置检查：result 表表头必须与当前规则完全匹配，
                # 不匹配则报错退出，不进行任何 LLM 提交。
                expected_header = build_expected_headers(rule_items)
                if result_path.is_file():
                    wb_check = load_workbook(result_path, data_only=False)
                    if RESULT_SHEET in wb_check.sheetnames:
                        ws_check = wb_check[RESULT_SHEET]
                        existing_header = (
                            [cell.value for cell in ws_check[1]] if ws_check.max_row >= 1 else []
                        )
                    else:
                        existing_header = []
                else:
                    existing_header = []
                if existing_header != expected_header:
                    raise RuntimeError(
                        "sen 阶段：结果文件缺失或 result 表表头与当前评分规则不匹配，"
                        "程序退出，未进行 LLM 提交（请先执行 doc 阶段生成结果文件）。\n"
                        f"结果文件：{result_path}\n"
                        f"期望表头：{expected_header}\n"
                        f"现有表头：{existing_header}"
                    )

                logger.info("【阶段 sen】LLM 评分项批量评估（每份报告一次提交 %d 项）", len(llm_rules))
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
                    # 批量 LLM 评分（一次提交所有 LLM 项）；失败抛出异常，由 main 捕获后退出程序
                    batch_scores = self._grade_with_llm_batch(client, report, llm_rules)
                    for rule in llm_rules:
                        row[rule.key] = batch_scores[rule.key]
                    final_scores[sid] = row
                    logger.info("【阶段 sen】%d/%d, ID %s：", idx, total, sid)

            # 汇总写入独立结果文件的 result 表（按学号匹配：存在则更新，不存在则报错退出）
            out = write_results(result_path, reports, rule_items, final_scores, mode="strict")
            logger.info("【阶段 sen】完成，最终评分已写入: %s，耗时 %.2f 秒", out,
                        time.perf_counter() - t0)
            return out
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            start_sid = reports[0].student_id if reports else None
            end_sid = reports[-1].student_id if reports else None
            self.stage_logs.append(StageLogEntry(
                stage="sen",
                start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
                duration_seconds=round(elapsed, 2),
                processed_count=len(reports),
                start_student_id=start_sid,
                end_student_id=end_sid,
                error=error,
            ))

    # ------------------------------------------------------------------ 总流程调度

    def run(self, reports_dir: str | None = None, template: str | None = None,
            output: str | None = None, stage: str = "full",
            skip_images: int = 2, tag_dep: str = "系部专业") -> Path | None:
        cfg = self.config
        reports_dir = reports_dir or cfg.paths.reports_dir
        template = template or cfg.paths.template
        # 结果输出路径：显式指定优先；留空则按 paths.result_name_format 在模板同目录生成
        output = output or cfg.paths.output or None

        # 确定运行日志文件路径
        self._log_path = self._resolve_log_path(template)

        try:
            if stage == "full":
                self.stage_doc(reports_dir, template, skip_images, tag_dep=tag_dep, output=output)
                self.stage_pic(reports_dir, template, skip_images, tag_dep=tag_dep)
                return self.stage_sen(reports_dir, template, output, skip_images, tag_dep=tag_dep)

            if stage == "doc":
                return self.stage_doc(reports_dir, template, skip_images, tag_dep=tag_dep,
                                      output=output)

            if stage == "pic":
                self.stage_pic(reports_dir, template, skip_images, tag_dep=tag_dep)
                return None

            if stage == "sen":
                return self.stage_sen(reports_dir, template, output, skip_images, tag_dep=tag_dep)

            raise ValueError(f"未知的 stage 参数: {stage}")
        finally:
            # 无论成功或出错，都写入运行日志
            self._write_run_log()

    # ------------------------------------------------------------------ 从磁盘加载报告（供 pic 阶段使用，不重新解析 word）

    def _load_reports_for_pic(self, reports_dir: Path, skip_images: int,
                              tag_dep: str = "系部专业") -> list[ParsedReport]:
        """从 doc 阶段已保存的 md 与图片文件加载报告，不重新解析 word。

        扫描目录下所有 *.md 文件，从文件名解析元数据，读取 md 内容，
        再从 md 中提取 [[PIC:文件名]] 占位符中的完整图片文件名，
        直接读取对应图片文件。
        """
        from .parser import parse_filename, PIC_PATTERN
        reports: list[ParsedReport] = []
        name_format = self.config.paths.report_name_format
        separator = self.config.paths.report_name_separator
        for md_path in sorted(reports_dir.glob("*.md")):
            if md_path.name.startswith("~$"):
                continue
            try:
                meta = parse_filename(md_path, name_format=name_format, separator=separator, suffixes=(".md",))
                if meta is None:
                    logger.warning("跳过不符合命名格式的 md 文件: %s", md_path.name)
                    continue
                course, klass, student_id, std_name, report_name = meta

                md_text = md_path.read_text(encoding="utf-8")

                # 从 md 中提取所有 [[PIC:文件名]] 占位符，直接获取图片文件名
                pic_files = PIC_PATTERN.findall(md_text)

                images: list[ReportImage] = []
                seen_indices: set[int] = set()
                for pic_file in pic_files:
                    candidate = md_path.parent / pic_file
                    if not candidate.is_file():
                        logger.warning("未找到图片文件: %s", candidate)
                        continue
                    # 从文件名解析索引：{base_name}-P{index}.{ext}
                    stem = candidate.stem  # {base_name}-P{index}
                    try:
                        pic_idx = int(stem.split("-P")[-1])
                    except (ValueError, IndexError):
                        logger.warning("无法从图片文件名解析索引: %s", pic_file)
                        continue
                    if pic_idx in seen_indices:
                        continue
                    seen_indices.add(pic_idx)
                    img_data = candidate.read_bytes()
                    img_ext = candidate.suffix.lstrip(".")
                    images.append(ReportImage(index=pic_idx, ext=img_ext, data=img_data))

                # 应用跳图策略：只保留 index > skip_images 的图片进行处理，
                # 但不改变图片原始编号（始终反映在文档中的实际位置）
                images = [img for img in images if img.index > skip_images]

                reports.append(ParsedReport(
                    course=course, klass=klass, student_id=student_id, std_name=std_name,
                    report_name=report_name, tag_dep=tag_dep,
                    source_path=str(md_path), md_text=md_text, images=images,
                    name_separator=separator,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.error("加载报告失败 %s: %s", md_path.name, exc)
        return reports

    # ------------------------------------------------------------------ LLM 批量评估（单文件一次性提交所有 LLM 项）

    def _grade_with_llm_batch(self, client: LLMClient, report: ParsedReport,
                              llm_rules: list[RuleItem]) -> dict[str, ItemScore]:
        """对单个文件的所有 LLM 评分项一次性提交，返回 {key: ItemScore}。

        失败不自行捕获，异常向上抛出。client.chat_json 内部已按 cfg.max_retries
        重试，耗尽后会抛出 RuntimeError，由 stage_sen / main 决定退出程序。
        """
        items_section: list[str] = []
        for rule in llm_rules:
            lo, hi = rule.score_range()
            score_range = f"{lo:g}-{hi:g}" if lo is not None and hi is not None else "按评分标准"
            items_section.append(f"- {rule.key}：{rule.evaluation}，分数范围 {score_range}。")
        items_text = "\n".join(items_section)

        prompt = self.config.prompts.llm_batch.format(
            items_section=items_text, report=report.md_text,
        )
        data = client.chat_json([{"role": "user", "content": prompt}])

        result: dict[str, ItemScore] = {}
        for rule in llm_rules:
            entry = data.get(rule.key) or {}
            score = _to_float(entry.get("score"))
            if score is None:
                lo, _ = rule.score_range()
                score = lo if lo is not None else 0.0
            confidence = _to_float(entry.get("confidence"))
            comment = str(entry.get("comment") or rule.comment or "").strip()
            result[rule.key] = ItemScore(key=rule.key, score=score,
                                         confidence=confidence, comment=comment)
            logger.info("  %s / %s -> score=%s confidence=%s (batch)",
                        report.student_id, rule.key, score, confidence)
        return result

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