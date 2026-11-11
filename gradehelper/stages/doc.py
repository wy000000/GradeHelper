"""阶段 doc：解析文档 + 程序化规则打分。"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

from ..batch import FailedFile
from ..excel_io import read_result_items, read_rules, write_results_atomic
from ..file_claim import ResultFileClaim
from ..models import ParsedReport, RuleItem
from ..parser import (
    parse_all, parse_filename, remove_skipped_placeholders, scan_reports,
    select_paths, strip_blank_lines,
)
from ..rules_engine import run_program_rules
from ..run_log import format_duration
from .common import _files_label, _run_mode_label, logger


class DocStageMixin:
    """阶段 doc（解析 + 程序规则打分）的实现。

    宿主类需提供 ``config`` / ``incremental`` / ``files``，以及结果路径、
    报告读写和运行日志（RunLogMixin）能力。
    """

    def stage_doc(self, reports_dir: str | Path, template: str | Path,
                  skip_images: int = 2, tag_dep: str = "系部专业",
                  output: str | Path | None = None) -> Path | None:
        """阶段 doc：解析文档 + 程序化规则打分，写入独立结果文件的 result 表。

        doc 只更新 result 表中的 code 项，LLM 项的分数/置信度/评语保持原值（新行留空），
        由 sen 阶段填写。执行顺序：解析文档 → 保存图片 → 保存 md → 写结果文件。
        增量模式（self.incremental=True）只解析结果文件 result 表中没有对应学号记录的文档；
        结果文件不存在时解析全部文档。
        运行日志按时序记录：开始的配置信息 → 处理过程中的出错/报警信息（保留文档名
        与出错内容）→ 结束时的阶段总结（最终未成功文档只在总结里列出）。
        """
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
        save_failed = 0
        scoring_failed = 0
        image_total = 0
        image_kept = 0
        # 增量模式下因结果文件已有记录而跳过的文档数
        skipped_existing_records = 0
        warnings_log: list[str] = []
        claim: ResultFileClaim | None = None
        parse_workers = max(1, int(cfg.paths.parse_workers))
        max_attempts = 1 + max(0, int(cfg.paths.retry_rounds))
        # 最终未成功的文档（文件全名，按全名去重）：只在阶段总结的「失败文档」里登记
        failed_documents: list[str] = []

        def _record_failed_doc(name: str) -> None:
            if name and name not in failed_documents:
                failed_documents.append(name)

        def _record_event(level: str, message: str, *, count: bool = True) -> None:
            """把出错/报警信息按时序写入运行日志（保留文档名与出错内容）。

            ``count=True`` 的条目同时计入阶段总结的「中间错误」条数；
            最终失败用 ``count=False``（已计入失败数，不重复算中间错误）。
            """
            if count:
                warnings_log.append(message)
            self._append_stage_event(stage="doc", level=level, message=message)

        def _on_parse_error(path: Path, exc: Exception) -> None:
            """单份文档解析失败（重试后仍失败）：按时序写入错误信息，不再单独写记录。"""
            nonlocal doc_failed
            doc_failed += 1
            _record_failed_doc(path.name)
            _record_event(
                "错误",
                f"{path.name}: 解析失败（重试后仍失败，已跳过）: "
                f"{str(exc) or exc.__class__.__name__}",
                count=False)

        def _on_parse_retry(path: Path, exc: Exception, attempt: int) -> None:
            """单份文档本次解析失败但还会重试：按时序记入运行日志的中间错误。"""
            _record_event(
                "警告",
                f"{path.name}: 第 {attempt} 次解析失败，将重试: "
                f"{str(exc) or exc.__class__.__name__}")

        def _on_save_error(failed: FailedFile) -> None:
            """单份文档解析产物落盘失败（重试后仍失败）：按时序写入错误信息。"""
            nonlocal save_failed
            save_failed += 1
            report = failed.item
            name = Path(report.source_path).name
            _record_failed_doc(name)
            _record_event(
                "错误",
                f"{name}: 解析产物保存失败（重试后仍失败，已跳过）: {failed.reason}",
                count=False)

        def _on_save_retry(failed: FailedFile) -> None:
            """单份文档解析产物本次落盘失败但还会重试：按时序记入中间错误。"""
            report = failed.item
            _record_event(
                "警告",
                f"{Path(report.source_path).name}: 第 {failed.attempts} 次保存失败，"
                f"将重试: {failed.reason}")

        try:
            # 结果文件被 Excel 等程序占用时立即提示退出，不做解析与评分；
            # 随后独占占用该文件：运行期间其它程序（如 Excel）只能只读打开，
            # 避免跑到最后写表时才发现文件被锁
            hint = self._resolve_result_path_hint(template, reports_dir, output, tag_dep)
            # 日志第一步：先记录配置信息（控制台同步输出），再做占用检查与解析
            start_course, start_report_name = self._first_report_identity(
                reports_dir, tuple(cfg.paths.document_suffixes), self.files)
            self._append_stage_start_entry(
                stage="doc",
                course=start_course,
                tag_dep=tag_dep,
                report_name=start_report_name,
                config={
                    # 模式紧跟「运行开始」，便于先看到本次是全部/指定、全量/增量
                    "模式": _run_mode_label(self.incremental, self.files),
                    **({"指定文件": _files_label(self.files)} if self.files else {}),
                    "报告目录": str(reports_dir),
                    "评分模板": str(template),
                    "结果文件": str(hint) if hint else "",
                    "文档后缀": "、".join(cfg.paths.document_suffixes),
                    "MinerU档位": cfg.paths.mineru_tier,
                    "并发": f"{parse_workers} 线程",
                    "重试轮数": f"{max(0, int(cfg.paths.retry_rounds))} 轮",
                    "跳过图片": f"前 {skip_images} 张",
                },
            )
            self._ensure_result_writable(hint)
            claim = ResultFileClaim(hint)
            claim.acquire()
            # 指定文件（--files）：先扫描目录并按名单筛选，未命中的名字逐条记为错误；
            # 部分未命中时继续解析命中的文件；全部未命中时报错退出，避免“指定了文件
            # 却什么都没做”仍返回成功。
            # 增量 doc：以结果文件 result 表中的学号记录为准，只解析没有记录的文档；
            # 结果文件不存在（首次运行）时解析全部文档。
            pending_paths: list[Path] | None = None
            all_docs: list[Path] = []

            def _student_id_of(path: Path) -> str:
                meta = parse_filename(
                    path, name_format=cfg.paths.report_name_format,
                    separator=cfg.paths.report_name_separator,
                    suffixes=(path.suffix.lower(),))
                return meta[2] if meta is not None else ""

            if self.files or self.incremental:
                try:
                    all_docs = scan_reports(
                        reports_dir,
                        name_format=cfg.paths.report_name_format,
                        separator=cfg.paths.report_name_separator,
                        suffixes=tuple(cfg.paths.document_suffixes),
                    )
                except OSError:
                    all_docs = []
                if self.files:
                    all_docs, unmatched = select_paths(all_docs, self.files)
                    for name in unmatched:
                        _record_event(
                            "错误",
                            f"指定的文件未找到（或不符合命名格式）: {name}")
                    if not all_docs:
                        raise RuntimeError(
                            "指定的文件在报告目录中没有找到: "
                            f"{reports_dir}\n指定文件：{'、'.join(self.files)}")
                if self.incremental:
                    recorded_ids: set[str] | None = None
                    if hint is not None and hint.is_file():
                        recorded_ids = set(read_result_items(hint))
                    if recorded_ids is None:
                        logger.info("【阶段 doc】增量模式：结果文件不存在，解析全部 %d 个文档",
                                    len(all_docs))
                        # 指定增量：只解析名单内文件（全部重新解析）
                        if self.files:
                            pending_paths = all_docs
                    else:
                        pending_paths = [
                            path for path in all_docs
                            if _student_id_of(path) not in recorded_ids
                        ]
                        skipped_existing_records = len(all_docs) - len(pending_paths)
                        if skipped_existing_records:
                            logger.info(
                                "【阶段 doc】增量模式：跳过 %d 个结果文件中已有记录的文档，"
                                "待解析 %d 个", skipped_existing_records, len(pending_paths))
                else:
                    # 指定全量：只解析名单内的文档，且全部重新解析
                    pending_paths = all_docs
            logger.info("【阶段 doc】解析报告目录: %s（并发 %d）", reports_dir, parse_workers)
            reports = parse_all(
                reports_dir,
                name_format=cfg.paths.report_name_format,
                separator=cfg.paths.report_name_separator,
                tag_dep=tag_dep,
                suffixes=tuple(cfg.paths.document_suffixes),
                mineru_tier=cfg.paths.mineru_tier,
                on_error=_on_parse_error,
                workers=parse_workers,
                max_attempts=max_attempts,
                on_retry=_on_parse_retry,
                paths=pending_paths,
            )
            if not reports:
                # 仅当确实有文档、且都因结果文件已有记录被跳过时才视为“增量无待办”；
                # 目录为空或路径写错仍按原逻辑报错，避免静默成功
                if (self.incremental and pending_paths is not None
                        and all_docs and not pending_paths):
                    logger.info("【阶段 doc】增量模式：所有文档均已记录，跳过解析与写表")
                    result_path = hint
                    return result_path
                raise RuntimeError(f"目录中没有符合命名格式的报告: {reports_dir}")

            # 移除被 skip 的图片（不保存图片文件）及其占位符，保持剩余图片原始编号；
            # 解析出的 md 同步去除空行（落盘与后续阶段使用同一份内容）
            total_reports = len(reports)
            for idx, report in enumerate(reports, 1):
                for warning in report.warnings:
                    _record_event("警告",
                                  f"{Path(report.source_path).name}: {warning}")
                before = len(report.images)
                image_total += before
                report.images = [img for img in report.images if img.index > skip_images]
                image_kept += len(report.images)
                report.md_text = strip_blank_lines(
                    remove_skipped_placeholders(report.md_text, skip_images))
                line = (f"  已解析 {idx}/{total_reports}: {report.base_name}"
                        f"（图片 {before} 张，跳过前 {skip_images} 张，剩余 {len(report.images)} 张）")
                sys.stderr.write("\r" + line)
                sys.stderr.flush()
            sys.stderr.write("\n")
            sys.stderr.flush()

            logger.info("【阶段 doc】共解析图片 %d 张（跳过前 %d 张，保留 %d 张）",
                        image_total, skip_images, image_kept)

            # 始终保存解析产物（md 与图片）到与原文件相同路径；
            # 单份保存失败进入异常队列，整批完成后自动重试，不中断其余文档
            reports = self._save_parsed_batch(
                reports, stage="doc", max_attempts=max_attempts,
                on_failure=_on_save_error, on_retry=_on_save_retry)
            if not reports:
                raise RuntimeError("所有报告的解析产物都保存失败")

            # 读取评分要求（rule 表）
            logger.info("【阶段 doc】读取评分模板 rule 表: %s", template)
            rule_items = read_rules(template)

            # 程序化 rule 项逐项批处理
            logger.info("【阶段 doc】程序化规则评分（逐 rule 批处理）")
            scores, warnings = run_program_rules(reports, rule_items)
            warning_count = len(warnings)
            for w in warnings:
                _record_event("警告", w)

            # 只写程序化（code）评分项；LLM 项由 sen 阶段填写，doc 不覆盖已有值
            result_path = self._resolve_result_path(template, reports, output)
            claim.retarget(result_path)  # 推断出的结果文件与预设路径不同时补占用
            write_results_atomic(result_path, reports, rule_items, scores,
                                 mode="upsert", claim=claim, code_only=True)
            logger.info("【阶段 doc】完成，程序化规则分数已写入 %s（仅更新 code 项，"
                        "LLM 项保持不变），耗时 %s",
                        result_path, format_duration(time.perf_counter() - t0))
            return result_path
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt，统一记录后原样抛出
            # 评分或写表阶段中断：已解析保存但未完成整个阶段的文档计入失败
            scoring_failed = len(reports)
            error = str(exc) or exc.__class__.__name__
            _record_event("错误", f"阶段中断: {error}", count=False)
            raise
        finally:
            if claim is not None:
                claim.release()  # 阶段结束（成功或失败）都必须释放独占句柄
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            extra: dict[str, str] = {}
            doc_succeeded = len(reports) - scoring_failed
            doc_failed_total = doc_failed + save_failed + scoring_failed
            doc_total = doc_succeeded + doc_failed_total
            extra["文档"] = (f"处理 {doc_total} 份 / 成功 {doc_succeeded} 份 / "
                             f"失败 {doc_failed_total} 份")
            if rule_items:
                llm_count = sum(1 for item in rule_items if item.is_llm)
                extra["规则项"] = f"{len(rule_items)}（LLM {llm_count}）"
            if warning_count:
                extra["规则警告"] = f"{warning_count} 条"
            if doc_failed:
                extra["解析失败"] = f"{doc_failed} 个"
            if save_failed:
                extra["保存失败"] = f"{save_failed} 个"
            unanswered = doc_failed + save_failed
            if unanswered:
                extra["异常队列"] = f"{unanswered} 个文件重试后仍失败"
            if failed_documents:
                extra["失败文档"] = "、".join(failed_documents)
            extra["并发"] = f"{parse_workers} 线程"
            extra["图片"] = f"解析 {image_total} 张 / 保留 {image_kept} 张"
            extra["跳过图片"] = f"前 {skip_images} 张"
            if self.incremental:
                extra["增量跳过"] = f"{skipped_existing_records} 个结果文件中已有记录的文档"
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
                warnings=warnings_log,
                reports_dir=reports_dir,
                template=template,
                output=result_path,
                processed=doc_total,
                succeeded=doc_succeeded,
                failed=doc_failed_total,
                unit="份",
                # 中间错误已按时序写入日志，总结里只保留条数
                list_warnings=False,
            ))
