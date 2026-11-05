"""总编排：doc(解析+程序规则) → pic(vllm识图) → sen(llm语义评分)，可独立执行。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from .batch import FailedFile, run_file_batch
from .cancel import CancelToken
from .config import AppConfig
from .excel_io import (
    read_result_items, read_rules, write_results_atomic, build_expected_headers, RESULT_SHEET
)
from .file_claim import ResultFileClaim
from .image_caption import caption_images, save_caption_txt
from .llm_client import LLMClient
from .llm_grading import (
    build_llm_items_section, grade_with_llm_batch, render_llm_batch_prompt,
)
from .models import ItemScore, ParsedReport, ReportImage, RuleItem
from .parser import (
    embed_captions, parse_all, parse_filename, remove_skipped_placeholders,
    strip_blank_lines,
)
from .report_io import (
    load_one_report_for_pic, load_reports_for_pic, save_parsed, save_parsed_batch,
)
from .result_paths import (
    ensure_result_writable, format_result_name, resolve_result_path,
    resolve_result_path_hint,
)
from .rules_engine import run_program_rules
from .run_log import RunLogMixin, StageLogEntry

logger = logging.getLogger(__name__)

__all__ = [
    "Grader", "StageLogEntry", "build_llm_items_section", "render_llm_batch_prompt",
]


class Grader(RunLogMixin):
    def __init__(self, config: AppConfig):
        self.config = config
        self._init_run_log()
        # 全局取消令牌：Ctrl+C / 异常退出时关闭在飞 LLM 连接，让服务端停止生成
        self.cancel_token = CancelToken()

    def cancel(self) -> None:
        """中断所有进行中的 LLM 请求（可重复调用）。"""
        self.cancel_token.cancel()

    # ------------------------------------------------------------ 结果文件路径（实现见 result_paths.py）

    @staticmethod
    def _format_result_name(fmt: str, course: str, tag_dep: str, report_name: str) -> str:
        return format_result_name(fmt, course, tag_dep, report_name)

    def _resolve_result_path(self, template: str | Path,
                             reports: list[ParsedReport],
                             output: str | Path | None = None) -> Path:
        return resolve_result_path(self.config, template, reports, output)

    def _resolve_result_path_hint(self, template: str | Path,
                                  reports_dir: str | Path | None,
                                  output: str | Path | None = None,
                                  tag_dep: str = "系部专业") -> Path | None:
        return resolve_result_path_hint(
            self.config, template, reports_dir, output, tag_dep)

    @staticmethod
    def _ensure_result_writable(result_path: str | Path | None) -> None:
        ensure_result_writable(result_path)

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
        save_failed = 0
        warnings_log: list[str] = []
        claim: ResultFileClaim | None = None
        parse_workers = max(1, int(cfg.paths.parse_workers))
        max_attempts = 1 + max(0, int(cfg.paths.retry_rounds))

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

        def _on_parse_retry(path: Path, exc: Exception, attempt: int) -> None:
            """单份文档本次解析失败但还会重试：记入运行日志的中间错误。"""
            warnings_log.append(
                f"{path.name}: 第 {attempt} 次解析失败，将重试: "
                f"{str(exc) or exc.__class__.__name__}")

        def _on_save_error(failed: FailedFile) -> None:
            """单份文档解析产物落盘失败（重试后仍失败）：按原报告文件记录。"""
            nonlocal save_failed
            save_failed += 1
            report = failed.item
            self._append_failure_entry(
                stage="doc",
                error=f"解析产物保存失败: {failed.reason}",
                report=report,
                document=Path(report.source_path).name,
                tag_dep=tag_dep,
                extra={"环节": "写盘"},
            )

        def _on_save_retry(failed: FailedFile) -> None:
            """单份文档解析产物本次落盘失败但还会重试：记入中间错误。"""
            report = failed.item
            warnings_log.append(
                f"{Path(report.source_path).name}: 第 {failed.attempts} 次保存失败，"
                f"将重试: {failed.reason}")

        try:
            # 结果文件被 Excel 等程序占用时立即提示退出，不做解析与评分；
            # 随后独占占用该文件：运行期间其它程序（如 Excel）只能只读打开，
            # 避免跑到最后写表时才发现文件被锁
            hint = self._resolve_result_path_hint(template, reports_dir, output, tag_dep)
            self._ensure_result_writable(hint)
            claim = ResultFileClaim(hint)
            claim.acquire()
            logger.info("【阶段 doc】解析报告目录: %s（并发 %d）", reports_dir, parse_workers)
            reports = parse_all(
                reports_dir,
                name_format=cfg.paths.report_name_format,
                separator=cfg.paths.report_name_separator,
                tag_dep=tag_dep,
                on_error=_on_parse_error,
                workers=parse_workers,
                max_attempts=max_attempts,
                on_retry=_on_parse_retry,
            )
            if not reports:
                raise RuntimeError(f"目录中没有符合命名格式的报告: {reports_dir}")

            # 移除被 skip 的图片（不保存图片文件）及其占位符，保持剩余图片原始编号；
            # 解析出的 md 同步去除空行（落盘与后续阶段使用同一份内容）
            for report in reports:
                for warning in report.warnings:
                    warnings_log.append(f"{Path(report.source_path).name}: {warning}")
                before = len(report.images)
                report.images = [img for img in report.images if img.index > skip_images]
                report.md_text = strip_blank_lines(
                    remove_skipped_placeholders(report.md_text, skip_images))
                logger.info("  已解析 %s（图片 %d 张，跳过前 %d 张，剩余 %d 张）",
                            report.base_name, before, skip_images, len(report.images))

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
            warnings_log.extend(warnings)
            for w in warnings:
                logger.warning("  %s", w)

            # LLM 项分数先写 0，sen 阶段再更新
            llm_rule_keys = [r.key for r in rule_items if r.is_llm]
            for sid_items in scores.values():
                for key in llm_rule_keys:
                    sid_items[key] = ItemScore(key=key, score=0.0, confidence=None, comment="")

            # 写入独立结果文件的 result 表（按学号匹配：存在则更新，不存在则添加；不修改模板）
            result_path = self._resolve_result_path(template, reports, output)
            claim.retarget(result_path)  # 推断出的结果文件与预设路径不同时补占用
            write_results_atomic(result_path, reports, rule_items, scores,
                                 mode="upsert", claim=claim)
            logger.info("【阶段 doc】完成，程序化规则分数已写入 %s（LLM 项已预置 0），耗时 %.2f 秒",
                        result_path, time.perf_counter() - t0)
            return result_path
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt，统一记录后原样抛出
            error = str(exc) or exc.__class__.__name__
            raise
        finally:
            if claim is not None:
                claim.release()  # 阶段结束（成功或失败）都必须释放独占句柄
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            extra: dict[str, str] = {}
            doc_failed_total = doc_failed + save_failed
            doc_total = len(reports) + doc_failed_total
            extra["文档"] = (f"处理 {doc_total} 份 / 成功 {len(reports)} 份 / "
                             f"失败 {doc_failed_total} 份")
            if rule_items:
                llm_count = sum(1 for item in rule_items if item.is_llm)
                extra["规则项"] = f"{len(rule_items)}（LLM {llm_count}）"
            if warning_count:
                extra["规则警告"] = f"{warning_count} 条"
            if doc_failed:
                extra["失败文档"] = f"{doc_failed} 个"
            if save_failed:
                extra["保存失败"] = f"{save_failed} 个"
            unanswered = doc_failed + save_failed
            if unanswered:
                extra["异常队列"] = f"{unanswered} 个文件重试后仍失败"
            extra["并发"] = f"{parse_workers} 线程"
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
                warnings=warnings_log,
                reports_dir=reports_dir,
                template=template,
                output=result_path,
                processed=doc_total,
                succeeded=len(reports),
                failed=doc_failed_total,
                unit="份",
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
        warnings_log: list[str] = []
        parse_workers = max(1, int(cfg.paths.parse_workers))
        max_attempts = 1 + max(0, int(cfg.paths.retry_rounds))

        # 最终异常（重试后仍失败）：按 md 文件句柄归属
        caption_errors: dict[tuple[str, int], str] = {}
        txt_errors: dict[tuple[str, int], str] = {}
        md_errors: dict[str, str] = {}
        saved_txt_keys: set[tuple[str, int]] = set()
        saved_md_keys: set[str] = set()
        report_by_key: dict[str, ParsedReport] = {}
        # 原始 md：实时回写时始终从它重新嵌入描述，避免重复嵌入
        base_md: dict[str, str] = {}
        # 最终写盘失败（阶段汇总用；异常中断时保持为空）
        caption_write_failed: list[str] = []
        md_write_failed: dict[str, str] = {}

        def _report_key(report: ParsedReport) -> str:
            return str(Path(report.source_path))

        def _on_load_error(path: Path, exc: Exception) -> None:
            """单份 md 加载失败（重试后仍失败）：追加具体文档的失败记录。"""
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

        def _on_load_retry(path: Path, exc: Exception, attempt: int) -> None:
            """单份 md 本次加载失败但还会重试：记入中间错误。"""
            warnings_log.append(
                f"{path.name}: 第 {attempt} 次加载失败，将重试: "
                f"{str(exc) or exc.__class__.__name__}")

        def _on_caption_error(report: ParsedReport, image: ReportImage,
                              exc: Exception) -> None:
            """单张图片识别失败：先记入异常队列，重试结束仍失败才写最终记录。"""
            reason = str(exc) or exc.__class__.__name__
            caption_errors[(_report_key(report), image.index)] = reason
            warnings_log.append(
                f"{report.base_name}-P{image.index}: 图片识别失败: {reason}")
            logger.warning("【阶段 pic】图片识别失败 %s-P%d: %s",
                           report.base_name, image.index, reason)

        def _embed_and_save_md(report: ParsedReport) -> None:
            """把当前已识别的描述嵌入 md 并落盘（可重复调用，不会重复嵌入）。"""
            key = _report_key(report)
            report.md_text = embed_captions(
                base_md.get(key, report.md_text), report.images,
                cfg.prompts.image_context, base_name=report.base_name)
            (Path(report.source_path).parent / f"{report.base_name}.md").write_text(
                report.md_text, encoding="utf-8")

        def _save_caption(report: ParsedReport, image: ReportImage) -> None:
            key = _report_key(report)
            item_key = (key, image.index)
            try:
                if save_caption_txt(report, image, cfg.paths.captions_dir or None) is not None:
                    saved_txt_keys.add(item_key)
                else:
                    saved_txt_keys.discard(item_key)
                txt_errors.pop(item_key, None)
            except Exception as exc:  # noqa: BLE001 写盘失败只记录，不中断识图
                txt_errors[item_key] = str(exc) or exc.__class__.__name__
                saved_txt_keys.discard(item_key)

        def _save_report(report: ParsedReport) -> None:
            """保存一份报告的全部图片描述与 md；失败只记录，不中断其它报告。"""
            key = _report_key(report)
            for image in report.images:
                _save_caption(report, image)
            try:
                _embed_and_save_md(report)
            except Exception as exc:  # noqa: BLE001 写盘失败只记录，不中断识图
                md_errors[key] = str(exc) or exc.__class__.__name__
                saved_md_keys.discard(key)
            else:
                md_errors.pop(key, None)
                saved_md_keys.add(key)

        def _on_caption_batch(items: list[tuple[ParsedReport, ReportImage]]) -> None:
            """按 batch_size 实时回写：图片描述 txt + 涉及的 md，无需等整阶段结束。"""
            touched: dict[str, ParsedReport] = {}
            for report, image in items:
                _save_caption(report, image)
                touched[_report_key(report)] = report
            for report in touched.values():
                try:
                    _embed_and_save_md(report)
                except Exception as exc:  # noqa: BLE001 写盘失败只记录，不中断识图
                    md_errors[_report_key(report)] = str(exc) or exc.__class__.__name__
                    saved_md_keys.discard(_report_key(report))
                else:
                    md_errors.pop(_report_key(report), None)
                    saved_md_keys.add(_report_key(report))
            logger.debug("【阶段 pic】实时回写 %d 张图片描述（累计 %d 个 txt）",
                         len(items), len(saved_txt_keys))

        def _pending_keys() -> set[str]:
            keys = {key for key, _index in caption_errors}
            keys.update(key for key, _index in txt_errors)
            keys.update(md_errors)
            return keys

        def _clear_resolved_caption_errors(items: list[ParsedReport]) -> None:
            for report in items:
                key = _report_key(report)
                for image in report.images:
                    if image.caption:
                        caption_errors.pop((key, image.index), None)

        try:
            logger.info("【阶段 pic】从已保存的 md 与图片加载报告: %s（并发 %d）",
                        reports_dir, parse_workers)
            reports = self._load_reports_for_pic(
                reports_dir, skip_images, tag_dep=tag_dep,
                on_error=_on_load_error, workers=parse_workers,
                max_attempts=max_attempts, on_retry=_on_load_retry,
                on_warning=warnings_log.append)
            if not reports:
                raise RuntimeError(f"目录中没有找到已保存的 md 文件: {reports_dir}")

            for report in reports:
                key = _report_key(report)
                report_by_key[key] = report
                base_md[key] = report.md_text
                for warning in report.warnings:
                    warnings_log.append(f"{Path(report.source_path).name}: {warning}")

            llm_rules_flag = any(r.is_llm for r in read_rules(template))
            if llm_rules_flag:
                caption_enabled = True
                logger.info("【阶段 pic】vllm 批量识别图片（每 %d 张实时回写）",
                            max(1, cfg.vllm.batch_size))
                try:
                    caption_images(
                        reports, cfg.vllm, cfg.prompts.image_prompt,
                        cancel=self.cancel_token, on_error=_on_caption_error,
                        on_batch=_on_caption_batch,
                    )
                except BaseException:
                    # 先停掉在飞的生成，再保留已识别成功的描述
                    self.cancel()
                    for report in reports:
                        _save_report(report)
                    logger.warning("【阶段 pic】已中断，已保存 %d 张已识别图片描述",
                                   len(saved_txt_keys))
                    raise
                _clear_resolved_caption_errors(reports)

                # 整批完成后重试异常队列（只重试失败的 md 文件）
                for retry in range(1, max_attempts):
                    pending_keys = _pending_keys()
                    pending_reports = [report_by_key[key] for key in pending_keys
                                       if key in report_by_key]
                    if not pending_reports:
                        break
                    logger.warning(
                        "【阶段 pic】第 %d/%d 轮重试 %d 个异常文件（缺失图片重新识别后覆盖保存）",
                        retry + 1, max_attempts, len(pending_reports))
                    # 只把仍缺描述的图片交给 vllm，已成功的图片不重复识别
                    missing_reports = [
                        replace(report,
                                images=[image for image in report.images if not image.caption])
                        for report in pending_reports
                        if any(not image.caption for image in report.images)
                    ]
                    if missing_reports:
                        try:
                            caption_images(
                                missing_reports, cfg.vllm, cfg.prompts.image_prompt,
                                cancel=self.cancel_token, on_error=_on_caption_error,
                            )
                        except BaseException:
                            self.cancel()
                            for report in pending_reports:
                                _save_report(report)
                            raise
                        _clear_resolved_caption_errors(pending_reports)
                    # 保存失败（而非识别失败）的文件也在本轮重试写盘
                    for report in pending_reports:
                        _save_report(report)

            caption_done = sum(1 for report in reports
                               for image in report.images if image.caption)
            saved_txt = len(saved_txt_keys)
            caption_failed = len(caption_errors)

            # 重试结束：把最终仍失败的异常按文件/图片写入运行日志
            for (key, index), reason in caption_errors.items():
                report = report_by_key.get(key)
                if report is None:
                    continue
                self._append_failure_entry(
                    stage="pic", error=f"图片识别失败: {reason}", report=report,
                    extra={"图片": f"P{index}"})
            caption_write_failed: list[str] = []
            for (key, index), reason in txt_errors.items():
                report = report_by_key.get(key)
                if report is None:
                    continue
                name = f"{report.base_name}-P{index}.txt"
                caption_write_failed.append(f"{name}: {reason}")
                logger.error("【阶段 pic】描述写盘失败 %s: %s", name, reason)
                self._append_failure_entry(
                    stage="pic", error=f"描述写盘失败: {reason}", report=report,
                    extra={"图片": f"P{index}", "环节": "写盘"})
            md_write_failed: dict[str, str] = {}
            for key, reason in md_errors.items():
                report = report_by_key.get(key)
                if report is None:
                    continue
                md_write_failed[report.base_name] = reason
                logger.error("【阶段 pic】md 回写失败 %s: %s", report.base_name, reason)
                self._append_failure_entry(
                    stage="pic", error=f"md 回写失败: {reason}", report=report,
                    extra={"环节": "写盘"})

            if caption_failed:
                logger.error("【阶段 pic】%d 张图片重试后仍识别失败，可重跑 --stage pic 补齐",
                             caption_failed)
            if caption_write_failed or md_write_failed:
                logger.error(
                    "【阶段 pic】%d 张描述 / %d 份 md 写盘失败（其余已按批回写），"
                    "可重跑 --stage pic 补齐",
                    len(caption_write_failed), len(md_write_failed))
                for line in caption_write_failed:
                    logger.error("  %s", line)
                for name, reason in md_write_failed.items():
                    logger.error("  %s.md: %s", name, reason)
            logger.info("【阶段 pic】完成，图片描述已嵌入 md 并落盘，耗时 %.2f 秒",
                        time.perf_counter() - t0)
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt，统一记录后原样抛出
            error = str(exc) or exc.__class__.__name__
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            doc_failed_total = load_failed + len(md_write_failed)
            doc_success = max(0, len(reports) - len(md_write_failed))
            doc_total = doc_success + doc_failed_total
            extra: dict[str, str] = {
                "文档": (f"处理 {doc_total} 份 / 成功 {doc_success} 份 / "
                         f"失败 {doc_failed_total} 份"),
            }
            extra["base_url"] = cfg.vllm.base_url or "（未配置）"
            extra["model"] = str(cfg.vllm.get_model() or "（未配置）")
            extra["并发"] = f"{max(1, cfg.vllm.batch_size)} 线程"
            if caption_enabled:
                img_failed = len(caption_errors) + len(txt_errors)
                img_success = max(0, caption_done - len(txt_errors))
                img_total = img_success + img_failed
                extra["图片"] = (f"处理 {img_total} 张 / 成功 {img_success} 张 / "
                                 f"失败 {img_failed} 张")
                total = doc_total + img_total
                success = doc_success + img_success
                failed = doc_failed_total + img_failed
                unit = "条"
            else:
                extra["识图"] = "跳过（无 LLM 评分项）"
                total = doc_total
                success = doc_success
                failed = doc_failed_total
                unit = "份"
            if load_failed:
                extra["加载失败"] = f"{load_failed} 个文档"
            if saved_txt:
                extra["描述 txt"] = f"{saved_txt} 个"
            if caption_write_failed or md_write_failed:
                extra["写盘失败"] = (f"{len(caption_write_failed)} 张 txt / "
                                    f"{len(md_write_failed)} 份 md")
            pending_count = len(caption_errors) + len(txt_errors) + len(md_errors)
            if pending_count:
                extra["异常队列"] = f"{pending_count} 项重试后仍失败"
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
                warnings=warnings_log,
                reports_dir=reports_dir,
                template=template,
                processed=total,
                succeeded=success,
                failed=failed,
                unit=unit,
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
        grade_failed = 0
        warnings_log: list[str] = []
        failed_report: ParsedReport | None = None
        claim: ResultFileClaim | None = None
        parse_workers = max(1, int(cfg.paths.parse_workers))
        max_attempts = 1 + max(0, int(cfg.paths.retry_rounds))
        # 每完成 batch_size 份报告就把已完成评分回写结果文件（中断也不丢已完成部分）
        checkpoint_every = max(1, cfg.llm.batch_size)
        # 实时回写失败后的“冷却”位置：再多完成一批才重试，避免每批都卡在重试上
        checkpoint_retry_at = 0
        checkpoint_failures = 0
        # 无 LLM 评分项时分数不会变化，跳过结尾的冗余写表
        write_needed = True

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

        def _on_load_retry(path: Path, exc: Exception, attempt: int) -> None:
            """单份 md 本次加载失败但还会重试：记入中间错误。"""
            warnings_log.append(
                f"{path.name}: 第 {attempt} 次加载失败，将重试: "
                f"{str(exc) or exc.__class__.__name__}")

        def _on_grade_failure(failed: FailedFile) -> None:
            """单份报告评分失败（重试后仍失败）：记录后继续处理其它报告。"""
            nonlocal grade_failed, failed_report
            grade_failed += 1
            report = failed.item
            if failed_report is None:
                failed_report = report
            logger.error("【阶段 sen】评分失败 %s: %s", report.base_name, failed.reason)
            self._append_failure_entry(
                stage="sen",
                error=failed.reason,
                report=report,
            )

        def _on_grade_retry(failed: FailedFile) -> None:
            """单份报告本次评分失败但还会重试：记入中间错误。"""
            report = failed.item
            warnings_log.append(
                f"{report.base_name}: 第 {failed.attempts} 次评分失败，将重试: {failed.reason}")

        def _grade_one(
            report: ParsedReport,
            client: LLMClient,
            llm_rules: list[RuleItem],
        ) -> tuple[ParsedReport, dict[str, ItemScore]]:
            """单份报告：程序化规则分数 + LLM 批量评分（在工作线程中执行）。"""
            sid = report.student_id
            row: dict[str, ItemScore] = {}
            for rule in rule_items:
                if not rule.is_llm:
                    prev_item = existing_scores.get(sid, {}).get(rule.key)
                    prev_score = (prev_item.score if isinstance(prev_item, ItemScore)
                                  else (prev_item or 0.0))
                    prev_comment = prev_item.comment if isinstance(prev_item, ItemScore) else ""
                    row[rule.key] = ItemScore(key=rule.key, score=prev_score,
                                              comment=prev_comment)
            batch_scores = self._grade_with_llm_batch(client, report, llm_rules)
            for rule in llm_rules:
                row[rule.key] = batch_scores[rule.key]
            return report, row

        try:
            # 结果文件被 Excel 等程序占用时立即提示退出，不做评分与写表；
            # 随后独占占用该文件：运行期间其它程序（如 Excel）只能只读打开
            hint = self._resolve_result_path_hint(template, reports_dir, output, tag_dep)
            self._ensure_result_writable(hint)
            claim = ResultFileClaim(hint)
            claim.acquire()
            # 读取评分要求（rule 表仍来自模板）
            rule_items = read_rules(template)
            llm_rules = [r for r in rule_items if r.is_llm]
            llm_rule_count = len(llm_rules)

            # 从磁盘读取已更新的 md（pic 阶段保存的）；多线程加载，失败文件自动重试
            reports = self._load_reports_for_pic(
                reports_dir, skip_images, tag_dep=tag_dep,
                on_error=_on_load_error, workers=parse_workers,
                max_attempts=max_attempts, stage="sen", on_retry=_on_load_retry,
                on_warning=warnings_log.append)

            for report in reports:
                for warning in report.warnings:
                    warnings_log.append(f"{Path(report.source_path).name}: {warning}")

            # 独立结果文件路径（与 doc 阶段同一解析逻辑，读写同一文件）
            result_path = self._resolve_result_path(template, reports, output)
            claim.retarget(result_path)  # 推断出的结果文件与预设路径不同时补占用

            # 读取结果文件中已存在的所有评分（不限 LLM 项，避免覆盖程序化规则分数）
            existing_scores = read_result_items(result_path)  # 读取全部评分列

            if not llm_rules:
                logger.info("【阶段 sen】无 LLM 评分项，沿用已有分数")
                for report in reports:
                    sid = report.student_id
                    row: dict[str, ItemScore] = {}
                    for rule in rule_items:
                        prev_item = existing_scores.get(sid, {}).get(rule.key)
                        prev_score = prev_item.score if isinstance(prev_item, ItemScore) else (prev_item or 0.0)
                        prev_comment = prev_item.comment if isinstance(prev_item, ItemScore) else ""
                        row[rule.key] = ItemScore(key=rule.key, score=prev_score,
                                                  confidence=None, comment=prev_comment)
                    final_scores[sid] = row
                completed_reports = list(reports)
                write_needed = False  # 分数未变化，结果文件不需要重新写
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

                # 学号覆盖检查：sen 只更新 result 表中已有的行（strict 不新增行），
                # 缺学号的报告必然在写表时失败，提前在这里报错退出，避免白跑一遍 LLM。
                missing_ids = [report.student_id for report in reports
                               if str(report.student_id).strip() not in existing_scores]
                if missing_ids:
                    shown = "、".join(missing_ids[:10])
                    if len(missing_ids) > 10:
                        shown += f" 等 {len(missing_ids)} 个"
                    raise RuntimeError(
                        f"sen 阶段：{len(missing_ids)} 份报告的学号不在结果文件 result 表中，"
                        "程序退出，未进行 LLM 提交（请先执行 doc 阶段生成结果文件）。\n"
                        f"结果文件：{result_path}\n"
                        f"缺少学号：{shown}"
                    )

                logger.info("【阶段 sen】LLM 评分项批量评估（每份报告一次提交 %d 项），并发 %d",
                            len(llm_rules), max(1, cfg.llm.batch_size))
                client = LLMClient(cfg.llm, cancel=self.cancel_token)

                def _on_graded(result: tuple[ParsedReport, dict[str, ItemScore]]) -> None:
                    """单份报告评分成功：立即记入结果，并按批实时回写。"""
                    nonlocal checkpoint_failures, checkpoint_retry_at
                    report, row = result
                    final_scores[report.student_id] = row
                    completed_reports.append(report)
                    logger.debug("【阶段 sen】已完成 ID %s", report.student_id)
                    if (len(completed_reports) % checkpoint_every == 0
                            and len(completed_reports) >= checkpoint_retry_at):
                        # 实时回写：中断/崩溃时最多只丢最后不足一批的结果
                        try:
                            write_results_atomic(
                                result_path, completed_reports, rule_items,
                                final_scores, mode="strict", claim=claim)
                            logger.info("【阶段 sen】实时回写 %d/%d 份评分 → %s",
                                        len(completed_reports), len(reports),
                                        result_path)
                        except Exception as save_exc:  # noqa: BLE001 回写失败不终止评分
                            checkpoint_failures += 1
                            # 冷却一批再试：一次瞬时占用不至于让整个阶段退化为只在结束时写
                            checkpoint_retry_at = len(completed_reports) + checkpoint_every
                            warnings_log.append(
                                f"实时回写失败（将在下一批后重试）: {save_exc}")
                            logger.warning(
                                "【阶段 sen】实时回写失败（将在下一批后重试，"
                                "结束时会再写一次）: %s", save_exc)

                run_file_batch(
                    stage="sen",
                    items=reports,
                    worker=lambda report: _grade_one(report, client, llm_rules),
                    workers=max(1, cfg.llm.batch_size),
                    path_of=lambda report: Path(report.source_path),
                    label_of=lambda report: report.student_id,
                    unit="份",
                    on_success=_on_graded,
                    on_failure=_on_grade_failure,
                    on_retry=_on_grade_retry,
                    max_attempts=max_attempts,
                )
                if grade_failed:
                    logger.error("【阶段 sen】%d 份报告重试后仍评分失败，已跳过",
                                 grade_failed)

            # 汇总写入独立结果文件的 result 表（按学号匹配：存在则更新，不存在则报错退出）
            if write_needed:
                successful_reports = [report for report in reports
                                      if report.student_id in final_scores]
                if successful_reports:
                    out = write_results_atomic(
                        result_path, successful_reports, rule_items, final_scores,
                        mode="strict", claim=claim)
                    logger.info("【阶段 sen】完成，最终评分已写入: %s，耗时 %.2f 秒", out,
                                time.perf_counter() - t0)
                else:
                    out = result_path
                    logger.error("【阶段 sen】没有任何报告成功评分，结果文件未更新: %s", out)
            else:
                out = result_path
                logger.info("【阶段 sen】无 LLM 评分项，结果文件无需更新: %s，耗时 %.2f 秒",
                            out, time.perf_counter() - t0)
            return out
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt
            error = str(exc) or exc.__class__.__name__
            # 中断/失败：先停掉在飞生成，再保存已经完整拿到结果的报告
            self.cancel()
            if (write_needed and completed_reports and result_path is not None
                    and rule_items):
                try:
                    out = write_results_atomic(result_path, completed_reports,
                                               rule_items, final_scores, mode="strict",
                                               claim=claim)
                    logger.warning(
                        "【阶段 sen】已中断（%s）：已保存 %d/%d 份完整评分 → %s",
                        error, len(completed_reports), len(reports), out,
                    )
                except Exception as save_exc:  # noqa: BLE001 保存失败不掩盖原始异常
                    logger.error("【阶段 sen】保存已完成评分失败: %s", save_exc)
            raise
        finally:
            if claim is not None:
                claim.release()  # 阶段结束（成功或失败）都必须释放独占句柄
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            sen_failed = load_failed + grade_failed
            sen_success = len(completed_reports)
            sen_total = len(reports) + load_failed
            sen_pending = max(0, sen_total - sen_success - sen_failed)
            extra: dict[str, str] = {
                "报告": (f"处理 {sen_total} 份 / 成功 {sen_success} 份 / "
                         f"失败 {sen_failed} 份"),
                "LLM 评分项": f"{llm_rule_count} 项",
                "base_url": cfg.llm.base_url or "（未配置）",
                "model": str(cfg.llm.get_model() or "（未配置）"),
                "并发": f"{max(1, cfg.llm.batch_size)} 线程",
            }
            if sen_pending:
                extra["未完成"] = f"{sen_pending} 份"
            if rule_items:
                extra["规则项"] = str(len(rule_items))
            if load_failed:
                extra["加载失败"] = f"{load_failed} 个文档"
            if grade_failed:
                extra["评分失败"] = f"{grade_failed} 份"
                extra["异常队列"] = f"{grade_failed} 份重试后仍失败"
            if result_path:
                extra["结果文件"] = str(result_path)
            extra["跳过图片"] = f"前 {skip_images} 张"
            if checkpoint_failures:
                extra["实时回写"] = f"失败 {checkpoint_failures} 次"
            self._append_stage_log(self._build_stage_entry(
                stage="sen",
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
                title_report=failed_report,
                document=(failed_report.base_name if failed_report is not None else ""),
                processed=sen_total,
                succeeded=sen_success,
                failed=sen_failed,
                unit="份",
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

        # 确定运行日志文件路径（按 course-tagDep-reportName.log 命名）
        self._log_path = self._resolve_log_path(template, reports_dir, tag_dep)
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
            # 全部阶段结束（成功、部分文件失败或中断）后统一写运行尾日志
            self._append_run_footer(
                start_dt=run_start_dt,
                elapsed=time.perf_counter() - run_t0,
                error=run_error,
            )

    # -------------------------------------------------------- 报告读写（实现见 report_io.py）

    def _load_reports_for_pic(self, reports_dir: Path, skip_images: int,
                              tag_dep: str = "系部专业",
                              on_error: Callable[[Path, Exception], None] | None = None,
                              workers: int = 1,
                              max_attempts: int = 2,
                              stage: str = "pic",
                              on_retry: Callable[[Path, Exception, int], None] | None = None,
                              on_warning: Callable[[str], None] | None = None,
                              progress_stage: str | None = None,
                              ) -> list[ParsedReport]:
        return load_reports_for_pic(
            self.config, reports_dir, skip_images, tag_dep=tag_dep,
            on_error=on_error, workers=workers, max_attempts=max_attempts,
            stage=stage, on_retry=on_retry, on_warning=on_warning,
            progress_stage=progress_stage)

    def _load_one_report_for_pic(self, md_path: Path, skip_images: int,
                                 tag_dep: str) -> ParsedReport:
        return load_one_report_for_pic(self.config, md_path, skip_images, tag_dep)

    def _save_parsed_batch(self, reports: list[ParsedReport], *,
                           stage: str = "doc", max_attempts: int = 2,
                           on_failure: Callable[[FailedFile], None] | None = None,
                           on_retry: Callable[[FailedFile], None] | None = None,
                           ) -> list[ParsedReport]:
        return save_parsed_batch(
            self.config, reports, stage=stage, max_attempts=max_attempts,
            on_failure=on_failure, on_retry=on_retry)

    @staticmethod
    def _save_parsed(reports: list[ParsedReport]) -> None:
        save_parsed(reports)

    # -------------------------------------------------------- LLM 评分（实现见 llm_grading.py）

    def _grade_with_llm_batch(self, client: LLMClient, report: ParsedReport,
                              llm_rules: list[RuleItem]) -> dict[str, ItemScore]:
        return grade_with_llm_batch(
            client, report, llm_rules, self.config.prompts.llm_batch)
