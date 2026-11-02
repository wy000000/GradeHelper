"""总编排：doc(解析+程序规则) → pic(vllm识图) → sen(llm语义评分)，可独立执行。"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .cancel import CancelToken, LLMAborted
from .config import AppConfig
from .excel_io import (
    read_result_scores, read_rules, write_results, build_expected_headers, RESULT_SHEET
)
from openpyxl import load_workbook
from .image_caption import caption_images, save_captions_txt
from .llm_client import LLMClient
from .models import ItemScore, ParsedReport, ReportImage, RuleItem
from .parser import embed_captions, parse_all, parse_filename
from .progress import ParallelProgress
from .rules_engine import run_program_rules

logger = logging.getLogger(__name__)

# 日志记录的时间标记；清理时按该时间判断新旧（当前格式，不兼容旧日志）
_RECORD_TIME_RE = re.compile(
    r"(?m)^记录时间:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)
_SEP_EQ = "=" * 60
_SEP_DASH = "-" * 60


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
    course: str = ""                # 课程名称（取第一份报告）
    tag_dep: str = ""               # 系部专业（取第一份报告；无报告时用 CLI 参数）
    report_name: str = ""           # 报告名称（取第一份报告）
    document: str = ""              # 具体文档名（单项失败时写入标题）
    extra: dict[str, str] = field(default_factory=dict)  # 阶段附加信息
    record_time: str = ""           # 日志记录写入时间（清理依据）

    @property
    def title(self) -> str:
        """阶段标题：课程名称-系部专业-报告名称-阶段；单项失败时用文档名替代报告名。"""
        course = (self.course or "").strip() or "未知课程"
        tag_dep = (self.tag_dep or "").strip() or "系部专业"
        detail = ((self.document or "").strip() or (self.report_name or "").strip()
                  or "未知报告")
        return f"{course}-{tag_dep}-{detail}-{self.stage}"


class Grader:
    def __init__(self, config: AppConfig):
        self.config = config
        self.stage_logs: list[StageLogEntry] = []
        self._log_path: Path | None = None
        self._log_lock = threading.Lock()
        self._log_failed = False
        # 全局取消令牌：Ctrl+C / 异常退出时关闭在飞 LLM 连接，让服务端停止生成
        self.cancel_token = CancelToken()

    def cancel(self) -> None:
        """中断所有进行中的 LLM 请求（可重复调用）。"""
        self.cancel_token.cancel()

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

    @staticmethod
    def _split_log_records(text: str) -> list[str]:
        """按当前日志格式切分记录。

        记录以 60 个 ``=`` 或 ``-`` 结尾；每条记录都带自己的
        ``记录时间``。无法识别时间的记录会原样保留。
        """
        lines = text.splitlines(keepends=True)
        records: list[str] = []
        buf: list[str] = []
        idx = 0
        while idx < len(lines):
            line = lines[idx]
            buf.append(line)
            idx += 1
            marker = line.rstrip("\r\n")
            if marker in (_SEP_EQ, _SEP_DASH):
                content = "".join(buf).strip()
                # 记录开头的分隔线（内容只有分隔线）不结束记录
                if content and content != marker:
                    # 记录末尾的空行归属当前记录，删除时一并清理
                    while idx < len(lines) and not lines[idx].strip():
                        buf.append(lines[idx])
                        idx += 1
                    records.append("".join(buf))
                    buf = []
        if buf:
            records.append("".join(buf))
        return records

    def _trim_log(self) -> None:
        """日志超过配置大小时，按每条记录的“记录时间”清理过期记录。

        只在启动阶段调用。大小与保留天数来自 config.yaml：
        ``paths.log_max_size_kb`` / ``paths.log_retention_days``；
        任一为 0 时关闭清理。当前格式的每条记录都必须带“记录时间”，
        没有该字段的旧格式记录会在清理时一并移除。
        清理采用同目录临时文件 + 原子替换。
        """
        if not self._log_path:
            return
        try:
            max_size_kb = int(self.config.paths.log_max_size_kb)
            retention_days = float(self.config.paths.log_retention_days)
        except (TypeError, ValueError):
            logger.warning("日志清理配置无效，跳过清理")
            return
        if max_size_kb <= 0 or retention_days <= 0:
            return

        log_path = Path(self._log_path)
        try:
            if not log_path.is_file():
                return
            size = log_path.stat().st_size
            if size <= max_size_kb * 1024:
                return
            text = log_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取运行日志失败，跳过旧记录清理: %s", exc)
            return

        records = self._split_log_records(text)
        if not records:
            return

        cutoff = datetime.now() - timedelta(days=retention_days)
        kept_records: list[str] = []
        removed = 0
        for record in records:
            match = _RECORD_TIME_RE.search(record)
            if not match:
                # 当前格式的每条记录都必须带“记录时间”；无时间视为旧格式并清理
                removed += 1
                continue
            try:
                record_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                removed += 1
                continue
            if record_time < cutoff:
                removed += 1
            else:
                kept_records.append(record)

        if not removed:
            return

        tmp_path = log_path.with_name(log_path.name + ".trim.tmp")
        try:
            tmp_path.write_text("".join(kept_records), encoding="utf-8")
            tmp_path.replace(log_path)
        except OSError as exc:
            logger.warning("清理运行日志失败，保留原文件: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        logger.info(
            "运行日志 %.1f KB 超过 %d KB，已清理 %d 条超过 %.1f 天的记录，保留 %d 条",
            size / 1024, max_size_kb, removed, retention_days, len(kept_records),
        )

    def _append_log(self, text: str) -> None:
        """把文本追加到运行日志；写失败只警告一次，不影响主流程。"""
        if not self._log_path or self._log_failed:
            return
        log_path = Path(self._log_path)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(text)
                    if text and not text.endswith("\n"):
                        fh.write("\n")
        except OSError as exc:
            self._log_failed = True
            logger.warning("运行日志写入失败，后续不再尝试: %s", exc)

    def _append_run_header(self, *, stage: str, reports_dir: str | Path,
                           template: str | Path, output: str | Path | None) -> None:
        """追加本次运行头。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            _SEP_EQ,
            "GradeHelper 运行日志",
            f"记录时间: {now}",
            f"运行开始: {now}",
            f"执行阶段: {stage}",
            f"报告目录: {reports_dir}",
            f"评分模板: {template}",
        ]
        if output:
            lines.append(f"结果文件: {output}")
        lines.extend([_SEP_EQ, ""])
        self._append_log("\n".join(lines) + "\n")

    def _append_run_footer(self, *, start_dt: datetime, elapsed: float,
                           error: str | None) -> None:
        """追加本次运行尾。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            _SEP_DASH,
            f"记录时间: {now}",
            f"运行结束: {now}",
            f"总用时: {elapsed:.2f} 秒",
            f"状态: {'成功' if not error else '失败/中断 - ' + error}",
            _SEP_EQ,
            "",
        ]
        self._append_log("\n".join(lines) + "\n")
        if self._log_path and not self._log_failed:
            logger.info("运行日志已追加: %s", self._log_path)

    def _build_stage_entry(self, *, stage: str, reports: list[ParsedReport],
                           tag_dep: str, start_dt: datetime, end_dt: datetime,
                           elapsed: float, error: str | None,
                           extra: dict[str, str] | None = None,
                           title_report: ParsedReport | None = None,
                           document: str = "") -> StageLogEntry:
        """构造阶段日志记录；标题取第一份报告的课程/系部专业/报告名。"""
        first = title_report or (reports[0] if reports else None)
        start_report = reports[0] if reports else first
        end_report = reports[-1] if reports else first
        extras = dict(extra or {})
        courses = {r.course.strip() for r in reports if r.course and r.course.strip()}
        report_names = {r.report_name.strip() for r in reports
                        if r.report_name and r.report_name.strip()}
        if len(courses) > 1 or len(report_names) > 1:
            extras["批次"] = f"{len(courses)} 个课程 / {len(report_names)} 种报告"
        return StageLogEntry(
            stage=stage,
            start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            duration_seconds=round(elapsed, 2),
            processed_count=len(reports),
            start_student_id=start_report.student_id if start_report else None,
            end_student_id=end_report.student_id if end_report else None,
            error=error,
            course=first.course if first else "",
            tag_dep=(first.tag_dep if first else "") or tag_dep,
            report_name=first.report_name if first else "",
            document=document,
            extra=extras,
        )

    def _append_failure_entry(self, *, stage: str, error: str,
                              report: ParsedReport | None = None,
                              document: str = "", course: str = "",
                              tag_dep: str = "", report_name: str = "",
                              student_id: str | None = None,
                              extra: dict[str, str] | None = None) -> None:
        """追加单个文档/图片的失败记录，标题精确到具体文档。"""
        now = datetime.now()
        if not document and report is not None:
            document = report.base_name
        sid = student_id or (report.student_id if report is not None else None)
        self._append_stage_log(StageLogEntry(
            stage=stage,
            start_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            duration_seconds=0.0,
            processed_count=0,
            start_student_id=sid,
            end_student_id=sid,
            error=error,
            course=course or (report.course if report is not None else ""),
            tag_dep=tag_dep or (report.tag_dep if report is not None else ""),
            report_name=report_name or (report.report_name if report is not None else ""),
            document=document,
            extra=dict(extra or {}),
        ))

    def _append_stage_log(self, entry: StageLogEntry) -> None:
        """追加单个阶段日志块（阶段结束时调用，保证中途退出也已落盘）。"""
        if not entry.record_time:
            entry.record_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.stage_logs.append(entry)
        lines = [
            entry.title,
            f"记录时间: {entry.record_time}",
            f"开始时间: {entry.start_time}",
            f"结束时间: {entry.end_time}",
            f"用时: {entry.duration_seconds:.2f} 秒",
        ]
        if entry.processed_count:
            lines.append(f"共处理: {entry.processed_count} 份")
        if entry.start_student_id:
            lines.append(f"开始学号: {entry.start_student_id}")
        if entry.end_student_id:
            lines.append(f"结束学号: {entry.end_student_id}")
        for key, value in entry.extra.items():
            lines.append(f"{key}: {value}")
        if entry.error:
            lines.append(f"状态: 失败 - {entry.error}")
        else:
            lines.append("状态: 成功")
        lines.extend([_SEP_DASH, ""])
        self._append_log("\n".join(lines) + "\n")

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
        rule_items: list[RuleItem] = []
        result_path: Path | None = None
        warning_count = 0
        doc_failed = 0

        def _on_parse_error(path: Path, exc: Exception) -> None:
            """单份文档解析失败：按具体文件名追加失败记录。"""
            nonlocal doc_failed
            doc_failed += 1
            meta = parse_filename(path, name_format=cfg.paths.report_name_format,
                                  separator=cfg.paths.report_name_separator)
            self._append_failure_entry(
                stage="doc",
                error=str(exc) or exc.__class__.__name__,
                document=path.name,
                course=meta[0] if meta else "",
                tag_dep=tag_dep,
                report_name=meta[4] if meta else "",
                student_id=meta[2] if meta else None,
            )

        try:
            logger.info("【阶段 doc】解析报告目录: %s", reports_dir)
            reports = parse_all(
                reports_dir,
                name_format=cfg.paths.report_name_format,
                separator=cfg.paths.report_name_separator,
                tag_dep=tag_dep,
                on_error=_on_parse_error,
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
            warning_count = len(warnings)
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
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt，统一记录后原样抛出
            error = str(exc) or exc.__class__.__name__
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            extra: dict[str, str] = {}
            if rule_items:
                llm_count = sum(1 for item in rule_items if item.is_llm)
                extra["规则项"] = f"{len(rule_items)}（LLM {llm_count}）"
            if warning_count:
                extra["规则警告"] = f"{warning_count} 条"
            if doc_failed:
                extra["失败文档"] = f"{doc_failed} 个"
            extra["跳过图片"] = f"前 {skip_images} 张"
            if result_path:
                extra["结果文件"] = str(result_path)
            self._append_stage_log(self._build_stage_entry(
                stage="doc",
                reports=reports,
                tag_dep=tag_dep,
                start_dt=start_dt,
                end_dt=end_dt,
                elapsed=elapsed,
                error=error,
                extra=extra,
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
        caption_done = 0
        saved_txt = 0
        caption_enabled = False
        caption_failed = 0
        load_failed = 0

        def _on_load_error(path: Path, exc: Exception) -> None:
            """单份 md 加载失败：追加具体文档的失败记录。"""
            nonlocal load_failed
            load_failed += 1
            meta = parse_filename(path, name_format=cfg.paths.report_name_format,
                                  separator=cfg.paths.report_name_separator,
                                  suffixes=(".md",))
            self._append_failure_entry(
                stage="pic",
                error=str(exc) or exc.__class__.__name__,
                document=path.name,
                course=meta[0] if meta else "",
                tag_dep=tag_dep,
                report_name=meta[4] if meta else "",
                student_id=meta[2] if meta else None,
            )

        def _on_caption_error(report: ParsedReport, image: ReportImage,
                              exc: Exception) -> None:
            """单张图片识别失败：追加具体文档 + 图片编号的失败记录。"""
            nonlocal caption_failed
            caption_failed += 1
            self._append_failure_entry(
                stage="pic",
                error=str(exc) or exc.__class__.__name__,
                report=report,
                extra={"图片": f"P{image.index}"},
            )

        try:
            logger.info("【阶段 pic】从已保存的 md 与图片加载报告: %s", reports_dir)
            reports = self._load_reports_for_pic(reports_dir, skip_images, tag_dep=tag_dep,
                                                 on_error=_on_load_error)
            if not reports:
                raise RuntimeError(f"目录中没有找到已保存的 md 文件: {reports_dir}")

            llm_rules_flag = any(r.is_llm for r in read_rules(template))
            if llm_rules_flag:
                caption_enabled = True
                logger.info("【阶段 pic】vllm 批量识别图片")
                try:
                    caption_done = caption_images(
                        reports, cfg.vllm, cfg.prompts.image_prompt,
                        cancel=self.cancel_token, on_error=_on_caption_error,
                    )
                except BaseException:
                    # 先停掉在飞的生成，再保留已识别成功的描述
                    self.cancel()
                    caption_done = sum(1 for report in reports
                                       for image in report.images if image.caption)
                    saved_txt = save_captions_txt(reports, cfg.paths.captions_dir or None)
                    logger.warning("【阶段 pic】已中断，已保存 %d 张已识别图片描述", saved_txt)
                    raise
                saved_txt = save_captions_txt(reports, cfg.paths.captions_dir or None)
                logger.info("  图片描述已另存 %d 个 txt", saved_txt)
            for report in reports:
                report.md_text = embed_captions(report.md_text, report.images,
                                                cfg.prompts.image_context,
                                                base_name=report.base_name)

            # 保存更新后的 md（带图片描述）
            self._save_parsed(reports)
            logger.info("【阶段 pic】完成，图片描述已嵌入 md 并落盘，耗时 %.2f 秒",
                        time.perf_counter() - t0)
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt，统一记录后原样抛出
            error = str(exc) or exc.__class__.__name__
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            caption_total = sum(len(report.images) for report in reports)
            extra: dict[str, str] = {"图片总数": f"{caption_total} 张"}
            if caption_enabled:
                extra["识别成功"] = f"{caption_done} 张"
                if caption_failed:
                    extra["识别失败"] = f"{caption_failed} 张"
                if caption_total > caption_done:
                    extra["未识别"] = f"{caption_total - caption_done} 张"
            else:
                extra["识图"] = "跳过（无 LLM 评分项）"
            if load_failed:
                extra["加载失败"] = f"{load_failed} 个文档"
            if saved_txt:
                extra["描述 txt"] = f"{saved_txt} 个"
            extra["跳过图片"] = f"前 {skip_images} 张"
            self._append_stage_log(self._build_stage_entry(
                stage="pic",
                reports=reports,
                tag_dep=tag_dep,
                start_dt=start_dt,
                end_dt=end_dt,
                elapsed=elapsed,
                error=error,
                extra=extra,
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
        result_path: Path | None = None
        rule_items: list[RuleItem] = []
        llm_rule_count = 0
        final_scores: dict[str, dict[str, ItemScore]] = {}
        # 已经完整拿到 LLM 结果并写入 final_scores 的报告（中断时只保存这些）
        completed_reports: list[ParsedReport] = []
        load_failed = 0
        failed_report: ParsedReport | None = None

        def _on_load_error(path: Path, exc: Exception) -> None:
            """单份 md 加载失败：追加具体文档的失败记录。"""
            nonlocal load_failed
            load_failed += 1
            meta = parse_filename(path, name_format=cfg.paths.report_name_format,
                                  separator=cfg.paths.report_name_separator,
                                  suffixes=(".md",))
            self._append_failure_entry(
                stage="sen",
                error=str(exc) or exc.__class__.__name__,
                document=path.name,
                course=meta[0] if meta else "",
                tag_dep=tag_dep,
                report_name=meta[4] if meta else "",
                student_id=meta[2] if meta else None,
            )

        try:
            # 读取评分要求（rule 表仍来自模板）
            rule_items = read_rules(template)
            llm_rules = [r for r in rule_items if r.is_llm]
            llm_rule_count = len(llm_rules)

            # 从磁盘读取已更新的 md（pic 阶段保存的）
            reports = self._load_reports_for_pic(reports_dir, skip_images, tag_dep=tag_dep,
                                                 on_error=_on_load_error)

            # 独立结果文件路径（与 doc 阶段同一解析逻辑，读写同一文件）
            result_path = self._resolve_result_path(template, reports, output)

            # 读取结果文件中已存在的所有评分（不限 LLM 项，避免覆盖程序化规则分数）
            existing_scores = read_result_scores(result_path)  # 读取全部评分列

            if not llm_rules:
                logger.info("【阶段 sen】无 LLM 评分项，沿用已有分数")
                for report in reports:
                    sid = report.student_id
                    row: dict[str, ItemScore] = {}
                    for rule in rule_items:
                        prev = existing_scores.get(sid, {}).get(rule.key, 0.0)
                        row[rule.key] = ItemScore(key=rule.key, score=prev,
                                                  confidence=None, comment="")
                    final_scores[sid] = row
                completed_reports = list(reports)
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

                logger.info("【阶段 sen】LLM 评分项批量评估（每份报告一次提交 %d 项），并发 %d",
                            len(llm_rules), max(1, cfg.llm.batch_size))
                client = LLMClient(cfg.llm, cancel=self.cancel_token)

                progress = ParallelProgress("sen", len(reports), unit="份",
                                            workers=max(1, cfg.llm.batch_size))
                with progress:
                    def _grade_one(report: ParsedReport) -> tuple[ParsedReport, dict[str, ItemScore]]:
                        """单份报告：程序化规则分数 + LLM 批量评分（在工作线程中执行）。"""
                        with progress.task(report.student_id):
                            sid = report.student_id
                            row: dict[str, ItemScore] = {}
                            for rule in rule_items:
                                if not rule.is_llm:
                                    prev = existing_scores.get(sid, {}).get(rule.key, 0.0)
                                    row[rule.key] = ItemScore(key=rule.key, score=prev, comment="")
                            batch_scores = self._grade_with_llm_batch(client, report, llm_rules)
                            for rule in llm_rules:
                                row[rule.key] = batch_scores[rule.key]
                            return report, row

                    pool = ThreadPoolExecutor(max_workers=max(1, cfg.llm.batch_size))
                    try:
                        futures = {pool.submit(_grade_one, report): report for report in reports}
                        for future in as_completed(futures):
                            try:
                                report, row = future.result()
                            except BaseException as exc:
                                current = futures[future]
                                if not isinstance(exc, (KeyboardInterrupt, LLMAborted)):
                                    failed_report = current
                                    logger.error("【阶段 sen】评分失败 %s: %s",
                                                 current.base_name, exc)
                                    self._append_failure_entry(
                                        stage="sen",
                                        error=str(exc) or exc.__class__.__name__,
                                        report=current,
                                    )
                                raise
                            final_scores[report.student_id] = row
                            completed_reports.append(report)
                            logger.debug("【阶段 sen】已完成 ID %s", report.student_id)
                    except BaseException:
                        # 任一报告失败/用户中断：立刻停止其余在飞请求，由外层统一保存已完成结果
                        self.cancel()
                        pool.shutdown(wait=False, cancel_futures=True)
                        raise
                    else:
                        pool.shutdown(wait=True)

            # 汇总写入独立结果文件的 result 表（按学号匹配：存在则更新，不存在则报错退出）
            out = write_results(result_path, reports, rule_items, final_scores, mode="strict")
            logger.info("【阶段 sen】完成，最终评分已写入: %s，耗时 %.2f 秒", out,
                        time.perf_counter() - t0)
            return out
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt
            error = str(exc) or exc.__class__.__name__
            # 中断/失败：先停掉在飞生成，再保存已经完整拿到结果的报告
            self.cancel()
            if completed_reports and result_path is not None and rule_items:
                try:
                    out = write_results(result_path, completed_reports,
                                        rule_items, final_scores, mode="strict")
                    logger.warning(
                        "【阶段 sen】已中断（%s）：已保存 %d/%d 份完整评分 → %s",
                        error, len(completed_reports), len(reports), out,
                    )
                except Exception as save_exc:  # noqa: BLE001 保存失败不掩盖原始异常
                    logger.error("【阶段 sen】保存已完成评分失败: %s", save_exc)
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            extra: dict[str, str] = {
                "LLM 评分项": f"{llm_rule_count} 项",
                "并发": f"{max(1, cfg.llm.batch_size)} 线程",
                "已完成": f"{len(completed_reports)}/{len(reports)} 份",
            }
            if rule_items:
                extra["规则项"] = str(len(rule_items))
            if load_failed:
                extra["加载失败"] = f"{load_failed} 个文档"
            if failed_report is not None:
                extra["评分失败"] = "1 份"
            if result_path:
                extra["结果文件"] = str(result_path)
            extra["跳过图片"] = f"前 {skip_images} 张"
            self._append_stage_log(self._build_stage_entry(
                stage="sen",
                reports=reports,
                tag_dep=tag_dep,
                start_dt=start_dt,
                end_dt=end_dt,
                elapsed=elapsed,
                error=error,
                extra=extra,
                title_report=failed_report,
                document=(failed_report.base_name if failed_report is not None else ""),
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

        # 确定运行日志文件路径；超过 32 KB 时先清理一天前的记录
        self._log_path = self._resolve_log_path(template)
        self._trim_log()
        self._log_failed = False
        self.stage_logs.clear()
        run_start_dt = datetime.now()
        run_t0 = time.perf_counter()
        run_error: str | None = None
        self._append_run_header(stage=stage, reports_dir=reports_dir,
                                template=template, output=output)

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
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt
            run_error = str(exc) or exc.__class__.__name__
            raise
        finally:
            # 阶段日志已在各阶段结束时增量追加；这里只补运行尾
            self._append_run_footer(start_dt=run_start_dt,
                                    elapsed=time.perf_counter() - run_t0,
                                    error=run_error)

    # ------------------------------------------------------------------ 从磁盘加载报告（供 pic 阶段使用，不重新解析 word）

    def _load_reports_for_pic(self, reports_dir: Path, skip_images: int,
                              tag_dep: str = "系部专业",
                              on_error: Callable[[Path, Exception], None] | None = None,
                              ) -> list[ParsedReport]:
        """从 doc 阶段已保存的 md 与图片文件加载报告，不重新解析 word。

        扫描目录下所有 *.md 文件，从文件名解析元数据，读取 md 内容，
        再从 md 中提取 [[PIC:文件名]] 占位符中的完整图片文件名，
        直接读取对应图片文件。on_error 用于记录单份 md 加载失败。
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
                if on_error is not None:
                    try:
                        on_error(md_path, exc)
                    except Exception:  # noqa: BLE001 记录失败不应影响其余报告
                        logger.debug("加载失败回调异常", exc_info=True)
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