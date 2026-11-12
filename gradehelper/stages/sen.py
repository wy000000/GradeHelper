"""阶段 sen：LLM 语义评分并写回结果表。"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from ..batch import FailedFile, run_file_batch
from ..excel_io import (
    RESULT_SHEET, build_expected_headers, read_result_items, read_rules,
    write_results_atomic,
)
from ..file_claim import ResultFileClaim
from ..llm_client import LLMClient
from ..models import ItemScore, ParsedReport, RuleItem
from ..run_log import format_duration
from .common import (
    _files_label, _report_needs_llm_grading, _run_mode_label, _select_reports,
    logger,
)


class SenStageMixin:
    """阶段 sen（LLM 语义评分 + 写结果表）的实现。

    宿主类需提供 ``config`` / ``incremental`` / ``files`` / ``cancel_token``，
    以及结果路径、报告加载、LLM 评分委托和运行日志（RunLogMixin）能力。
    """

    def stage_sen(self, reports_dir: str | Path, template: str | Path,
                  output: str | Path | None = None, skip_images: int = 2,
                  tag_dep: str = "系部专业") -> Path:
        """阶段 sen：llm 语义评分（单文件批量），写入独立结果文件的 result 表。

        增量模式（self.incremental=True）只评分结果表中任一 LLM 项未评分的报告；
        运行日志按时序记录：开始的配置信息 → 处理过程中的出错/报警信息（保留文档名
        与出错内容）→ 结束时的阶段总结（最终未成功文档只在总结里列出）。
        """
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
        # 学号不在 result 表而被跳过的报告数（记入失败数，但不中断其余报告评分）
        missing_skipped = 0
        # 最终失败文档：md 全名（课程-班级-学号-姓名-报告名.md），按全名去重，重试多轮只计一次
        failed_documents: list[str] = []
        failed_seen: set[str] = set()
        # 重试期间仍未成功的文档：md 全名 -> 最近一次失败原因（只用于判断是否重试成功）
        retrying: dict[str, str] = {}
        # 重试后成功的文档：不计入失败数，仅作为说明写入日志
        recovered: list[str] = []
        warnings_log: list[str] = []
        failed_report: ParsedReport | None = None
        claim: ResultFileClaim | None = None
        parse_workers = max(1, int(cfg.paths.parse_workers))
        max_attempts = 1 + max(0, int(cfg.paths.retry_rounds))
        # 每完成一份报告就把已完成评分回写结果文件（有结果就回写，不分批）
        checkpoint_every = max(1, cfg.llm.llm_workers)
        # 实时回写失败后的“冷却”位置：再完成约一批（checkpoint_every 份）才重试，
        # 避免结果文件被占用时每完成一份都重复失败、刷警告
        checkpoint_retry_at = 0
        checkpoint_failures = 0
        # 无 LLM 评分项时分数不会变化，跳过结尾的冗余写表
        write_needed = True
        # 增量模式：因 LLM 项均已评分而跳过的报告数
        incremental_skipped = 0

        def _doc_name(report: ParsedReport) -> str:
            """失败文档的全名：本阶段实际处理的 md 文件名（课程-班级-学号-姓名-报告名.md）。"""
            return Path(report.source_path).name

        def _record_failed(label: str) -> bool:
            """登记最终失败文档（md 全名）；已登记的不重复计数。"""
            if label in failed_seen:
                return False
            failed_seen.add(label)
            failed_documents.append(label)
            return True

        def _record_event(level: str, message: str, *, count: bool = True) -> None:
            """把出错/报警信息按时序写入运行日志（保留文档名与出错内容）。

            ``count=True`` 的条目同时计入阶段总结的「中间错误」条数；
            最终失败用 ``count=False``（已计入失败数，不重复算中间错误）。
            """
            if count:
                warnings_log.append(message)
            self._append_stage_event(stage="sen", level=level, message=message)

        def _mark_retrying(label: str, reason: str) -> None:
            """登记重试中的失败项：失败数不重复累计，中间错误按时序写入日志、不省略。"""
            retrying[label] = reason
            _record_event("警告", f"{label}: {reason}")

        def _mark_recovered(label: str) -> None:
            """重试成功：从失败计数中移除，并记为「重试成功」。"""
            if retrying.pop(label, None) is not None and label not in recovered:
                recovered.append(label)

        def _on_load_error(path: Path, exc: Exception) -> None:
            """单份 md 最终加载失败：按时序写入错误信息，不再单独写失败记录。"""
            nonlocal load_failed
            label = path.name
            retrying.pop(label, None)  # 转为最终失败，不再算重试中
            if _record_failed(label):
                load_failed += 1
            _record_event(
                "错误",
                f"{label}: 加载失败（重试后仍失败，已跳过）: "
                f"{str(exc) or exc.__class__.__name__}",
                count=False)

        def _on_load_retry(path: Path, exc: Exception, attempt: int) -> None:
            """单份 md 本次加载失败但还会重试：记入中间错误（每轮都保留）。"""
            _mark_retrying(
                path.name,
                f"第 {attempt} 次加载失败: {str(exc) or exc.__class__.__name__}")

        def _detail_message(failed: FailedFile) -> str:
            """附属日志的完整内容：失败原因 + LLM 完整原始输出（非截断）。

            主 log/控制台的 reason 只保留前后 200 字符（如 JsonParseError、
            LLMContentError）；这里额外附上异常携带的完整原文（raw_text）。
            """
            message = (f"第 {failed.attempts} 次评分失败: {failed.reason}")
            raw = getattr(failed.error, "raw_text", None)
            if raw:
                message += f"\n完整 LLM 原始输出:\n{raw}"
            return message

        def _on_grade_failure(failed: FailedFile) -> None:
            """单份报告评分失败（重试后仍失败）：按时序写入错误信息，继续其它报告。"""
            nonlocal grade_failed, failed_report
            report = failed.item
            label = _doc_name(report)
            retrying.pop(label, None)  # 转为最终失败，不再算重试中
            if _record_failed(label):
                grade_failed += 1
            if failed_report is None:
                failed_report = report
            _record_event(
                "错误",
                f"{label}: 评分失败（重试后仍失败，已跳过）: {failed.reason}",
                count=False)
            # 附属日志保留完整内容（失败原因 + LLM 完整原始输出）；每次失败都写
            self._append_failure_detail("sen", report, _detail_message(failed))

        def _on_grade_retry(failed: FailedFile) -> None:
            """单份报告本次评分失败但还会重试：记入中间错误（每轮都保留）。"""
            report = failed.item
            _mark_retrying(
                _doc_name(report),
                f"第 {failed.attempts} 次评分失败: {failed.reason}")
            # 每次失败都写入附属日志：完整原因 + LLM 完整原始输出（不去重）
            self._append_failure_detail("sen", report, _detail_message(failed))

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
            # 日志第一步：先记录配置信息（控制台同步输出），再做占用检查与评分
            start_course, start_report_name = self._first_report_identity(
                reports_dir, ".md", self.files)
            self._append_stage_start_entry(
                stage="sen",
                course=start_course,
                tag_dep=tag_dep,
                report_name=start_report_name,
                config={
                    "模式": _run_mode_label(self.incremental, self.files),
                    **({"指定文件": _files_label(self.files)} if self.files else {}),
                    "报告目录": str(reports_dir),
                    "评分模板": str(template),
                    "结果文件": str(hint) if hint else "",
                    "base_url": cfg.llm.base_url or "（未配置）",
                    "model": str(cfg.llm.get_model() or "（未配置）"),
                    "并发": f"{max(1, cfg.llm.llm_workers)} 线程",
                    "解析并发": f"{parse_workers} 线程",
                    "重试轮数": f"{max(0, int(cfg.paths.retry_rounds))} 轮",
                    "跳过图片": f"前 {skip_images} 张",
                },
            )
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
                on_warning=lambda message: _record_event("警告", message))

            # 指定文件（--files）：只保留名单内的报告，未命中的名字逐条告警；
            # 名单全部未命中时报错退出，避免“指定了文件却什么都没做”仍返回成功
            reports, unmatched_files = _select_reports(reports, self.files)
            for name in unmatched_files:
                _record_event("警告", f"指定的文件没有对应的 md 文件: {name}")
            if self.files and not reports:
                raise RuntimeError(
                    "指定的文件没有对应的 md 文件（请先执行 doc 阶段）: "
                    f"{reports_dir}\n指定文件：{'、'.join(self.files)}")

            for report in reports:
                # 之前加载失败的文档重试成功后，从失败计数中移除
                _mark_recovered(_doc_name(report))
                for warning in report.warnings:
                    _record_event("警告",
                                  f"{Path(report.source_path).name}: {warning}")

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

                # 增量模式：只保留任一 LLM 项未评分的报告；全已评分的报告跳过
                if self.incremental:
                    pending_reports = [
                        report for report in reports
                        if _report_needs_llm_grading(
                            report.student_id, existing_scores, llm_rules)
                    ]
                    incremental_skipped = len(reports) - len(pending_reports)
                    if incremental_skipped:
                        logger.info(
                            "【阶段 sen】增量模式：跳过 %d 份 LLM 项已评分的报告，待评分 %d 份",
                            incremental_skipped, len(pending_reports))
                    reports = pending_reports
                    if not reports:
                        logger.info("【阶段 sen】增量模式：所有报告的 LLM 项均已评分，无需评分")
                        return result_path

                # 学号覆盖检查：sen 只更新 result 表中已有的行（strict 不新增行），
                # 缺学号的报告无法写回：记录错误并跳过这些报告，继续评分其余报告
                # （不为写不进结果表的报告白跑 LLM，也不因个别缺行整体退出）。
                missing_reports = [report for report in reports
                                   if str(report.student_id).strip() not in existing_scores]
                if missing_reports:
                    for report in missing_reports:
                        label = _doc_name(report)
                        if _record_failed(label):
                            missing_skipped += 1
                        if failed_report is None:
                            failed_report = report
                        _record_event(
                            "错误",
                            f"{label}: 学号不在结果文件 result 表中，已跳过"
                            "（请先执行 doc 阶段生成结果文件）",
                            count=False)
                    missing_sids = {str(report.student_id).strip()
                                    for report in missing_reports}
                    reports = [report for report in reports
                               if str(report.student_id).strip() not in missing_sids]
                    if not reports:
                        logger.error(
                            "【阶段 sen】%d 份报告的学号都不在结果文件 result 表中，"
                            "未进行 LLM 提交（请先执行 doc 阶段生成结果文件）",
                            missing_skipped)
                        return result_path

                logger.info("【阶段 sen】LLM 评分项批量评估（每份报告一次提交 %d 项），并发 %d",
                            len(llm_rules), max(1, cfg.llm.llm_workers))
                client = LLMClient(cfg.llm, cancel=self.cancel_token)

                def _on_graded(result: tuple[ParsedReport, dict[str, ItemScore]]) -> None:
                    """单份报告评分成功：立即记入结果并实时回写（不分批）。"""
                    nonlocal checkpoint_failures, checkpoint_retry_at
                    report, row = result
                    final_scores[report.student_id] = row
                    completed_reports.append(report)
                    # 之前评分失败、重试成功的文档不再计入失败数
                    _mark_recovered(_doc_name(report))
                    logger.debug("【阶段 sen】已完成 ID %s", report.student_id)
                    if len(completed_reports) >= checkpoint_retry_at:
                        # 实时回写：每完成一份就写一次，中断/崩溃时不丢已完成的任何结果；
                        # 成功不打 INFO（进度由进度条单行原地显示），失败告警保留
                        try:
                            write_results_atomic(
                                result_path, completed_reports, rule_items,
                                final_scores, mode="strict", claim=claim,
                                announce=False)
                            logger.debug("【阶段 sen】实时回写 %d/%d 份评分 → %s",
                                         len(completed_reports), len(reports),
                                         result_path)
                        except Exception as save_exc:  # noqa: BLE001 回写失败不终止评分
                            checkpoint_failures += 1
                            # 冷却约一批的量再试：一次瞬时占用不至于退化为只在结束时写，
                            # 也不至于每完成一份都重复失败
                            checkpoint_retry_at = len(completed_reports) + checkpoint_every
                            _record_event(
                                "警告",
                                f"实时回写失败（冷却 {checkpoint_every} 份后重试，"
                                f"结束时会再写一次）: {save_exc}")

                run_file_batch(
                    stage="sen",
                    items=reports,
                    worker=lambda report: _grade_one(report, client, llm_rules),
                    workers=max(1, cfg.llm.llm_workers),
                    # 进入重试轮：llm 并发（llm_workers）按配置值减半——只减一次，
                    # 后续重试轮沿用同一值，不逐轮递归减半（下限 1 线程）
                    retry_workers=max(1, cfg.llm.llm_workers // 2),
                    path_of=lambda report: Path(report.source_path),
                    label_of=_doc_name,
                    unit="份",
                    on_success=_on_graded,
                    on_failure=_on_grade_failure,
                    on_retry=_on_grade_retry,
                    max_attempts=max_attempts,
                    progress_stall=cfg.llm.read_timeout or None,
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
                    logger.info("【阶段 sen】完成，最终评分已写入: %s，耗时 %s", out,
                                format_duration(time.perf_counter() - t0))
                else:
                    out = result_path
                    logger.error("【阶段 sen】没有任何报告成功评分，结果文件未更新: %s", out)
            else:
                out = result_path
                logger.info("【阶段 sen】无 LLM 评分项，结果文件无需更新: %s，耗时 %s",
                            out, format_duration(time.perf_counter() - t0))
            return out
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt
            error = str(exc) or exc.__class__.__name__
            # 中断/失败：先停掉在飞生成，再保存已经完整拿到结果的报告
            self.cancel()
            _record_event("错误", f"阶段中断: {error}", count=False)
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
                    _record_event("错误", f"保存已完成评分失败: {save_exc}",
                                  count=False)
            raise
        finally:
            if claim is not None:
                claim.release()  # 阶段结束（成功或失败）都必须释放独占句柄
            end_dt = datetime.now()
            elapsed = time.perf_counter() - t0
            # 中断分类（全部文档 = 成功 + 失败 + 未处理，三类互斥）：
            # - 成功：评分已完成（含中间出过错但重试已恢复的文档，此类已从 retrying 移除）；
            # - 失败：出错且尚未成功（中断时仍在重试队列），并入失败文档名单；
            # - 未处理：无错误记录、但中断时尚未完成评分，单独计数、不并入失败。
            # 最终失败的文档已在「失败文档」名单里（并计入失败数），不重复登记。
            interrupt_error_count = 0
            sen_unprocessed_docs: list[str] = []
            if error:
                for label in retrying:  # 出错且尚未成功；最终失败已从中移除
                    if _record_failed(label):
                        interrupt_error_count += 1
                completed_labels = {_doc_name(report) for report in completed_reports}
                for report in reports:
                    name = _doc_name(report)
                    if (name in completed_labels or name in failed_seen
                            or name in sen_unprocessed_docs):
                        continue  # 已成功 / 已计失败 / 已归类，不重复登记
                    sen_unprocessed_docs.append(name)
            sen_failed = (load_failed + grade_failed + missing_skipped
                          + interrupt_error_count)
            sen_success = len(completed_reports)
            sen_unprocessed = len(sen_unprocessed_docs)
            sen_total = sen_success + sen_failed + sen_unprocessed
            failed_docs_text = "、".join(failed_documents)
            extra: dict[str, str] = {
                "报告": (f"处理 {sen_total} 份 / 成功 {sen_success} 份 / "
                         f"失败 {sen_failed} 份"
                         + (f" / 未处理 {sen_unprocessed} 份" if sen_unprocessed else "")),
                "LLM 评分项": f"{llm_rule_count} 项",
                "base_url": cfg.llm.base_url or "（未配置）",
                "model": str(cfg.llm.get_model() or "（未配置）"),
                "并发": f"{max(1, cfg.llm.llm_workers)} 线程",
            }
            if recovered:
                extra["重试成功"] = f"{len(recovered)} 份（{'、'.join(recovered)}）"
            if rule_items:
                extra["规则项"] = str(len(rule_items))
            if load_failed:
                extra["加载失败"] = f"{load_failed} 个文档"
            if grade_failed:
                extra["评分失败"] = f"{grade_failed} 份"
                extra["异常队列"] = f"{grade_failed} 份重试后仍失败"
            if missing_skipped:
                extra["学号缺失"] = f"{missing_skipped} 份（不在 result 表，已跳过）"
            if failed_documents:
                extra["失败文档"] = failed_docs_text
            if sen_unprocessed_docs:
                extra["未处理文档"] = "、".join(sen_unprocessed_docs)
            if result_path:
                extra["结果文件"] = str(result_path)
            extra["跳过图片"] = f"前 {skip_images} 张"
            if self.incremental:
                extra["增量跳过"] = f"{incremental_skipped} 份 LLM 项已评分报告"
            if checkpoint_failures:
                extra["实时回写"] = f"失败 {checkpoint_failures} 次"
            entry = self._build_stage_entry(
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
                document=(_doc_name(failed_report)
                          if failed_report is not None else ""),
                processed=sen_total,
                succeeded=sen_success,
                failed=sen_failed,
                unprocessed=sen_unprocessed,
                unit="份",
                # 中间错误已按时序写入日志，总结里只保留条数
                list_warnings=False,
            )

            # 控制台汇总：成功/失败/未处理份数与失败/未处理文档全名（阶段中断时也输出）；
            # 先输出一行汇总，随后追加各项名单，让控制台以总结收尾
            if sen_total:
                summary = (f"【阶段 sen】处理 {sen_total} 份文档：成功 {sen_success} 份，"
                           f"失败 {sen_failed} 份")
                if sen_unprocessed:
                    summary += f"，未处理 {sen_unprocessed} 份"
                if failed_docs_text:
                    docs = [d.strip() for d in failed_docs_text.split("、") if d.strip()]
                    summary += "；失败文档：\n" + "\n".join(f"  - {d}" for d in docs)
                if sen_unprocessed_docs:
                    summary += ("；未处理文档：\n"
                                + "\n".join(f"  - {d}" for d in sen_unprocessed_docs))
                if sen_failed or error is not None:
                    logger.error("%s", summary)
                else:
                    logger.info("%s", summary)
            self._append_stage_log(entry)
