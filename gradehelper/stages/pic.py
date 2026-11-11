"""阶段 pic：vllm 图片识别并回写 md。"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..excel_io import read_rules
from ..image_caption import caption_images, save_caption_txt
from ..models import ParsedReport, ReportImage
from ..parser import embed_captions, has_image_caption
from ..run_log import append_failure_detail, format_duration
from .common import _files_label, _run_mode_label, _select_reports, logger


class PicStageMixin:
    """阶段 pic（vllm 识图 + 回写 md）的实现。

    宿主类需提供 ``config`` / ``incremental`` / ``files`` / ``cancel_token``，
    以及报告加载和运行日志（RunLogMixin）能力。
    """

    def stage_pic(self, reports_dir: str | Path, template: str | Path,
                  skip_images: int = 2, tag_dep: str = "系部专业") -> None:
        """阶段 pic：vllm 识别所有图片，将描述嵌入 md 文档并落盘。

        运行日志按时序记录：开始的配置信息 → 处理过程中的出错/报警信息（保留文档名
        与出错内容）→ 结束时的阶段总结（最终未成功文档只在总结里列出）。
        """
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
        # 增量模式：因 md 中已有描述而跳过的图片/报告数
        skipped_caption_images = 0
        skipped_caption_reports = 0
        warnings_log: list[str] = []
        parse_workers = max(1, int(cfg.paths.parse_workers))
        max_attempts = 1 + max(0, int(cfg.paths.retry_rounds))
        # 最终未成功的文档（md 文件全名，按全名去重）：只在阶段总结的「失败文档」里登记
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
            self._append_stage_event(stage="pic", level=level, message=message)

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
            """单份 md 加载失败（重试后仍失败）：按时序写入错误信息，不再单独写记录。"""
            nonlocal load_failed
            load_failed += 1
            _record_failed_doc(path.name)
            _record_event(
                "错误",
                f"{path.name}: 加载失败（重试后仍失败，已跳过）: "
                f"{str(exc) or exc.__class__.__name__}",
                count=False)

        def _on_load_retry(path: Path, exc: Exception, attempt: int) -> None:
            """单份 md 本次加载失败但还会重试：按时序记入中间错误。"""
            _record_event(
                "警告",
                f"{path.name}: 第 {attempt} 次加载失败，将重试: "
                f"{str(exc) or exc.__class__.__name__}")

        def _on_caption_error(report: ParsedReport, image: ReportImage,
                              exc: Exception) -> None:
            """单张图片识别失败：记入异常队列，同时按时序写入警告信息。"""
            reason = str(exc) or exc.__class__.__name__
            caption_errors[(_report_key(report), image.index)] = reason
            _record_event("警告",
                          f"{report.base_name}-P{image.index}: 图片识别失败: {reason}")
            # pic 阶段的 JSON 解析错误（JsonParseError 携带完整原文）同样写入附 log
            raw = getattr(exc, "raw_text", None)
            if raw:
                append_failure_detail(
                    "pic", report,
                    f"{report.base_name}-P{image.index}: JSON 解析错误:"
                    f"{reason}\n完整原文:\n{raw}")

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
            """按 vllm_workers 实时回写：图片描述 txt + 涉及的 md，无需等整阶段结束。"""
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
            # 日志第一步：先记录配置信息（控制台同步输出），再加载与识图
            start_course, start_report_name = self._first_report_identity(
                reports_dir, ".md", self.files)
            self._append_stage_start_entry(
                stage="pic",
                course=start_course,
                tag_dep=tag_dep,
                report_name=start_report_name,
                config={
                    "模式": _run_mode_label(self.incremental, self.files),
                    **({"指定文件": _files_label(self.files)} if self.files else {}),
                    "报告目录": str(reports_dir),
                    "评分模板": str(template),
                    "base_url": cfg.vllm.base_url or "（未配置）",
                    "model": str(cfg.vllm.get_model() or "（未配置）"),
                    "并发": f"{max(1, cfg.vllm.vllm_workers)} 线程",
                    "解析并发": f"{parse_workers} 线程",
                    "重试轮数": f"{max(0, int(cfg.paths.retry_rounds))} 轮",
                    "跳过图片": f"前 {skip_images} 张",
                },
            )
            caption_template = cfg.prompts.image_context
            incremental_caption_ok = bool(
                self.incremental and caption_template and "{caption}" in caption_template)
            if self.incremental and not incremental_caption_ok:
                _record_event(
                    "警告",
                    "增量模式无法识别已有图片描述（prompts.image_context 缺少 {caption}），"
                    "将按全量识别")
            logger.info("【阶段 pic】从已保存的 md 与图片加载报告: %s（并发 %d）",
                        reports_dir, parse_workers)
            reports = self._load_reports_for_pic(
                reports_dir, skip_images, tag_dep=tag_dep,
                on_error=_on_load_error, workers=parse_workers,
                max_attempts=max_attempts, on_retry=_on_load_retry,
                on_warning=lambda message: _record_event("警告", message))
            # 指定文件（--files）：只保留名单内的报告，未命中的名字逐条告警
            reports, unmatched_files = _select_reports(reports, self.files)
            for name in unmatched_files:
                _record_event("警告", f"指定的文件没有对应的 md 文件: {name}")
            if not reports:
                if self.files:
                    raise RuntimeError(
                        "指定的文件没有对应的 md 文件（请先执行 doc 阶段）: "
                        f"{reports_dir}\n指定文件：{'、'.join(self.files)}")
                raise RuntimeError(f"目录中没有找到已保存的 md 文件: {reports_dir}")

            for report in reports:
                key = _report_key(report)
                report_by_key[key] = report
                base_md[key] = report.md_text
                if incremental_caption_ok:
                    before = len(report.images)
                    report.images = [
                        image for image in report.images
                        if not has_image_caption(report.md_text, caption_template,
                                                 image.index)
                    ]
                    skipped_caption_images += before - len(report.images)
                    if before and not report.images:
                        skipped_caption_reports += 1
                for warning in report.warnings:
                    _record_event("警告",
                                  f"{Path(report.source_path).name}: {warning}")
            if skipped_caption_images:
                logger.info("【阶段 pic】增量模式：跳过 %d 张已有描述图片（%d 份报告），待识别 %d 张",
                            skipped_caption_images, skipped_caption_reports,
                            sum(len(report.images) for report in reports))

            llm_rules_flag = any(r.is_llm for r in read_rules(template))
            pending_image_count = sum(len(report.images) for report in reports)
            if llm_rules_flag:
                caption_enabled = True
                if not pending_image_count:
                    logger.info("【阶段 pic】%s",
                                "增量模式：所有图片均已有描述，无需识别"
                                if self.incremental else "没有需要识别的图片")
                else:
                    logger.info("【阶段 pic】vllm 批量识别图片（每 %d 张实时回写）",
                                max(1, cfg.vllm.vllm_workers))
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

                    # 整批完成后重试异常队列（只重试失败的 md 文件）。
                    # 进入重试轮：vllm 并发（vllm_workers）按配置值减半——只减一次，
                    # 后续重试轮沿用同一值，不逐轮递归减半（下限 1 线程）
                    retry_vllm = replace(cfg.vllm,
                                         vllm_workers=max(1, cfg.vllm.vllm_workers // 2))
                    for retry in range(1, max_attempts):
                        pending_keys = _pending_keys()
                        pending_reports = [report_by_key[key] for key in pending_keys
                                           if key in report_by_key]
                        if not pending_reports:
                            break
                        logger.warning(
                            "【阶段 pic】第 %d/%d 轮重试 %d 个异常文件"
                            "（并发降为 %d，缺失图片重新识别后覆盖保存）",
                            retry + 1, max_attempts, len(pending_reports),
                            retry_vllm.vllm_workers)
                        # 只把仍缺描述的图片交给 vllm，已成功的图片不重复识别
                        missing_reports = [
                            replace(report,
                                    images=[image for image in report.images
                                            if not image.caption])
                            for report in pending_reports
                            if any(not image.caption for image in report.images)
                        ]
                        if missing_reports:
                            try:
                                caption_images(
                                    missing_reports, retry_vllm,
                                    cfg.prompts.image_prompt,
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
                _record_failed_doc(Path(report.source_path).name)
                _record_event(
                    "错误",
                    f"{report.base_name}-P{index}: 图片识别失败（重试后仍失败）: {reason}",
                    count=False)
            caption_write_failed: list[str] = []
            for (key, index), reason in txt_errors.items():
                report = report_by_key.get(key)
                if report is None:
                    continue
                name = f"{report.base_name}-P{index}.txt"
                caption_write_failed.append(f"{name}: {reason}")
                _record_failed_doc(Path(report.source_path).name)
                _record_event(
                    "错误",
                    f"{name}: 描述写盘失败（重试后仍失败）: {reason}",
                    count=False)
            md_write_failed: dict[str, str] = {}
            for key, reason in md_errors.items():
                report = report_by_key.get(key)
                if report is None:
                    continue
                md_write_failed[report.base_name] = reason
                _record_failed_doc(Path(report.source_path).name)
                _record_event(
                    "错误",
                    f"{Path(report.source_path).name}: md 回写失败（重试后仍失败）: {reason}",
                    count=False)

            if caption_failed:
                logger.error("【阶段 pic】%d 张图片重试后仍识别失败，可重跑 --stage pic 补齐",
                             caption_failed)
            if caption_write_failed or md_write_failed:
                logger.error(
                    "【阶段 pic】%d 张描述 / %d 份 md 写盘失败（其余已按批回写），"
                    "可重跑 --stage pic 补齐",
                    len(caption_write_failed), len(md_write_failed))
            logger.info("【阶段 pic】完成，图片描述已嵌入 md 并落盘，耗时 %s",
                        format_duration(time.perf_counter() - t0))
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt，统一记录后原样抛出
            error = str(exc) or exc.__class__.__name__
            _record_event("错误", f"阶段中断: {error}", count=False)
            raise
        finally:
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            if error:
                # 阶段中断：已加载但未完成整个阶段的文档都算失败
                failed_in_reports = len(reports)
            else:
                # 正常完成：md 回写失败的文档算失败（图片识别/txt 失败只影响图片计数）
                failed_in_reports = len(md_write_failed)
            doc_success = len(reports) - failed_in_reports
            doc_failed_total = load_failed + failed_in_reports
            doc_total = doc_success + doc_failed_total
            extra: dict[str, str] = {
                "文档": (f"处理 {doc_total} 份 / 成功 {doc_success} 份 / "
                         f"失败 {doc_failed_total} 份"),
            }
            extra["base_url"] = cfg.vllm.base_url or "（未配置）"
            extra["model"] = str(cfg.vllm.get_model() or "（未配置）")
            extra["并发"] = f"{max(1, cfg.vllm.vllm_workers)} 线程"
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
                # 合计 = 文档 + 图片，共处理行写明构成，避免与分项数字对不上
                processed_note = f"文档 {doc_total} 份 + 图片 {img_total} 张"
            else:
                extra["识图"] = "跳过（无 LLM 评分项）"
                total = doc_total
                success = doc_success
                failed = doc_failed_total
                unit = "份"
                processed_note = ""  # 只计文档，单位已是“份”，无需额外说明
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
            if self.incremental:
                extra["增量跳过"] = (f"{skipped_caption_images} 张已有描述图片"
                                     f"（{skipped_caption_reports} 份报告）")
            if failed_documents:
                extra["失败文档"] = "、".join(failed_documents)
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
                processed_note=processed_note,
                # 中间错误已按时序写入日志，总结里只保留条数
                list_warnings=False,
            ))
