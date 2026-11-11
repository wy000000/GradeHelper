"""总编排：doc(解析+程序规则) → pic/pic-e(图片描述回写) → sen(llm语义评分)，可独立执行。

阶段内：单文件出错只记录并继续完成本阶段（由 batch 与各阶段自行容错）；
阶段间（full / full-e 模式）：当前阶段结束后若仍有错误，不再执行下一阶段。

三个阶段的具体实现位于 ``gradehelper.stages``，以 mixin 形式组合进 :class:`Grader`。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .batch import FailedFile
from .cancel import CancelToken
from .config import AppConfig
from .llm_client import LLMClient
from .llm_grading import (
    build_llm_items_section, grade_with_llm_batch, render_llm_batch_prompt,
)
from .models import ItemScore, ParsedReport, RuleItem
from .parser import normalize_file_names, parse_filename, select_paths
from .report_io import (
    load_one_report_for_pic, load_reports_for_pic, save_parsed, save_parsed_batch,
)
from .result_paths import (
    ensure_result_writable, format_result_name, resolve_result_path,
    resolve_result_path_hint,
)
from .run_log import RunLogMixin, StageLogEntry
from .stages import DocStageMixin, PicStageMixin, SenStageMixin
from .stages.common import (
    _files_label, _llm_item_scored, _report_needs_llm_grading, _run_mode_label,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Grader", "StageLogEntry", "build_llm_items_section", "render_llm_batch_prompt",
]


class Grader(DocStageMixin, PicStageMixin, SenStageMixin, RunLogMixin):
    def __init__(self, config: AppConfig):
        self.config = config
        # 增量模式：doc 只解析结果文件中无记录的文档；pic 只识别缺描述的图片；
        # sen 只评分 LLM 项未评分的报告。由启动参数 --incremental true/false 控制。
        self.incremental = False
        # 指定文件（--files）：非空时增量/全量只对名单内的文件有效。
        self.files: list[str] = []
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

    def _first_report_identity(self, reports_dir: str | Path,
                               suffixes: str | tuple[str, ...],
                               names: list[str] | None = None) -> tuple[str, str]:
        """取报告目录中排序第一份合规文件的（课程名, 报告名），用于阶段开始记录的标题。

        suffixes 可为单个后缀或后缀元组（doc 阶段传入 paths.document_suffixes，
        pic/sen 阶段传入 .md）。names 非空时只看 --files 名单内的文件；
        找不到合规文件时返回空串对，由标题逻辑回退到占位名。
        """
        cfg = self.config
        allowed = (suffixes,) if isinstance(suffixes, str) else tuple(suffixes)
        allowed_set = {s.lower() for s in allowed}
        try:
            candidates = sorted(
                p for p in Path(reports_dir).iterdir()
                if p.is_file() and p.suffix.lower() in allowed_set)
        except OSError:
            return "", ""
        for path in candidates:
            if path.name.startswith("~$"):
                continue
            if names:
                matched, _unmatched = select_paths([path], names)
                if not matched:
                    continue
            meta = parse_filename(path, name_format=cfg.paths.report_name_format,
                                  separator=cfg.paths.report_name_separator,
                                  suffixes=(path.suffix.lower(),))
            if meta is not None:
                return meta[0], meta[4]
        return "", ""

    # ------------------------------------------------------------------ 总流程调度

    def _last_stage_has_errors(self) -> bool:
        """最近结束的阶段是否存在错误（口径与阶段日志「状态」一致）。

        阶段中断（error）或重试后仍失败（failed_count > 0）都算错误；仅中间
        警告（warnings）不算，不阻断下一阶段。各阶段都在 ``finally`` 里收尾
        登记阶段记录，因此阶段正常返回后 ``stage_logs[-1]`` 即该阶段的记录。
        """
        if not self.stage_logs:
            return False
        entry = self.stage_logs[-1]
        return bool(entry.error) or entry.failed_count > 0

    def _abort_followup_stages(self, finished: str, skipped: tuple[str, ...]) -> None:
        """当前阶段已按“出错继续完成本阶段”收尾、但仍存在错误时中止后续阶段。

        跳过原因写入控制台（ERROR）与运行日志，并抛出 ``RuntimeError``——
        后续阶段不会执行，命令行入口统一转为退出码 1。
        """
        entry = self.stage_logs[-1]
        detail = entry.error or f"最终失败 {entry.failed_count} 项"
        message = (f"阶段 {finished} 存在错误（{detail}），已完成本阶段，"
                   f"不再执行后续阶段（{'、'.join(skipped)} 未运行）")
        self._append_stage_event(stage=finished, level="错误", message=message)
        raise RuntimeError(message)

    def run(self, reports_dir: str | None = None, template: str | None = None,
            output: str | None = None, stage: str = "full-e",
            skip_images: int = 2, tag_dep: str = "系部专业",
            incremental: bool = False,
            files: list[str] | str | None = None) -> Path | None:
        cfg = self.config
        # 增量模式：显式传入 true/false（CLI 已解析为 bool）
        self.incremental = bool(incremental)
        # 指定文件：非空时增量/全量只对名单内的文件有效；none/空值表示不过滤
        self.files = normalize_file_names(files)
        # reports_dir 仅由启动参数提供，默认当前工作目录
        reports_dir = reports_dir or str(Path.cwd())
        # template 仅由启动参数提供，不能为空
        template = (template or "").strip()
        if not template:
            logger.error("评分模板未指定：--template 不能为空，请通过启动参数传入")
            raise SystemExit(1)
        # 结果输出路径：显式指定优先；留空则按 paths.result_name_format 在模板同目录生成
        output = output or cfg.paths.output or None

        # 确定运行日志文件路径（按 course-tagDep-reportName.log 命名）
        self._log_path = self._resolve_log_path(template, reports_dir, tag_dep)
        self._log_failed = False
        self.stage_logs.clear()
        # 上次异常退出可能留下未闭合的阶段记录块，新一次运行重新开始
        self._stage_block_open = False
        self._stage_config_values = {}
        self._append_run_header(stage=stage, reports_dir=reports_dir,
                                template=template, output=output)

        try:
            if stage in ("full", "full-e"):
                # 阶段内出错继续完成本阶段（各阶段自行容错）；
                # 阶段之间：当前阶段结束后仍有错误时不再执行下一阶段
                # full-e 与 full 的唯一区别：图片阶段使用 pic-e（优先复用描述 txt）
                use_pic_e = stage == "full-e"
                pic_stage = "pic-e" if use_pic_e else "pic"
                self.stage_doc(reports_dir, template, skip_images, tag_dep=tag_dep, output=output)
                if self._last_stage_has_errors():
                    self._abort_followup_stages("doc", (pic_stage, "sen"))
                if use_pic_e:
                    self.stage_pic_e(reports_dir, template, skip_images, tag_dep=tag_dep)
                else:
                    self.stage_pic(reports_dir, template, skip_images, tag_dep=tag_dep)
                if self._last_stage_has_errors():
                    self._abort_followup_stages(pic_stage, ("sen",))
                return self.stage_sen(reports_dir, template, output, skip_images, tag_dep=tag_dep)

            if stage == "doc":
                return self.stage_doc(reports_dir, template, skip_images, tag_dep=tag_dep,
                                      output=output)

            if stage == "pic":
                self.stage_pic(reports_dir, template, skip_images, tag_dep=tag_dep)
                return None

            if stage == "pic-e":
                self.stage_pic_e(reports_dir, template, skip_images, tag_dep=tag_dep)
                return None

            if stage == "sen":
                return self.stage_sen(reports_dir, template, output, skip_images, tag_dep=tag_dep)

            raise ValueError(f"未知的 stage 参数: {stage}")
        finally:
            # 运行开始/结束时间与状态已按阶段记录在各自的记录块里（含失败状态）；
            # 这里只提示日志落盘位置，成功与中断都会输出
            if self._log_path and not self._log_failed:
                logger.info("运行日志已追加: %s", self._log_path)

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