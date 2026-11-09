"""报告读写：从已保存的 md 构造报告对象；把解析产物落盘。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .batch import FailedFile, run_file_batch
from .config import AppConfig
from .models import ParsedReport, ReportImage
from .parser import PIC_PATTERN, parse_filename, remove_skipped_placeholders

logger = logging.getLogger(__name__)


def load_reports_for_pic(config: AppConfig, reports_dir: Path, skip_images: int,
                         tag_dep: str = "系部专业",
                         on_error: Callable[[Path, Exception], None] | None = None,
                         workers: int = 1,
                         max_attempts: int = 2,
                         stage: str = "pic",
                         on_retry: Callable[[Path, Exception, int], None] | None = None,
                         on_warning: Callable[[str], None] | None = None,
                         progress_stage: str | None = None,
                         ) -> list[ParsedReport]:
    """多线程加载 doc 阶段已保存的 md 与图片，不重新解析 word。

    - 扫描目录下所有 *.md 文件，文件名不符合命名格式的直接忽略；
    - 单份 md 加载失败进入异常队列，全部加载完成后自动重试；
    - 每次失败且还会重试时通过 ``on_retry`` 记录中间错误；
    - 重试结束仍失败时通过 ``on_error`` 记录，其余报告照常返回；
    - 进度条默认显示为「{stage} 加载MD文档」，与同阶段随后的评分进度区分。
    """
    name_format = config.paths.report_name_format
    separator = config.paths.report_name_separator
    md_paths: list[Path] = []
    for md_path in sorted(reports_dir.glob("*.md")):
        if md_path.name.startswith("~$"):
            continue
        if parse_filename(md_path, name_format=name_format, separator=separator,
                          suffixes=(".md",)) is None:
            message = f"跳过不符合命名格式的 md 文件: {md_path.name}"
            logger.warning("%s", message)
            if on_warning is not None:
                try:
                    on_warning(message)
                except Exception:  # noqa: BLE001 记录失败不应影响加载
                    logger.debug("加载告警回调异常", exc_info=True)
            continue
        md_paths.append(md_path)

    def _on_final_failure(failed: FailedFile) -> None:
        logger.error("加载报告失败 %s: %s", failed.path.name, failed.reason)
        if on_error is not None:
            try:
                on_error(failed.path, failed.error)
            except Exception:  # noqa: BLE001 记录失败不应影响其余报告
                logger.debug("加载失败回调异常", exc_info=True)

    def _on_retry_failure(failed: FailedFile) -> None:
        if on_retry is not None:
            try:
                on_retry(failed.path, failed.error, failed.attempts)
            except Exception:  # noqa: BLE001 记录失败不应影响其余报告
                logger.debug("加载中间失败回调异常", exc_info=True)

    outcome = run_file_batch(
        stage=stage,
        items=md_paths,
        worker=lambda md_path: load_one_report_for_pic(
            config, md_path, skip_images, tag_dep),
        workers=max(1, int(workers)),
        path_of=lambda md_path: md_path,
        label_of=lambda md_path: md_path.name,
        unit="份",
        on_failure=_on_final_failure,
        on_retry=_on_retry_failure,
        max_attempts=max_attempts,
        progress_stage=progress_stage or f"{stage} 加载MD文档",
    )
    return outcome.results


def load_one_report_for_pic(config: AppConfig, md_path: Path, skip_images: int,
                            tag_dep: str) -> ParsedReport:
    """从单个 md 文件及其图片占位符构造报告对象。"""
    name_format = config.paths.report_name_format
    separator = config.paths.report_name_separator
    meta = parse_filename(md_path, name_format=name_format, separator=separator,
                          suffixes=(".md",))
    if meta is None:
        raise ValueError(f"md 文件名不符合命名格式: {md_path.name}")
    course, klass, student_id, std_name, report_name = meta

    md_text = md_path.read_text(encoding="utf-8")
    # 被跳过图片的占位符不进入后续阶段（兼容旧版 doc 阶段产出的 md）
    md_text = remove_skipped_placeholders(md_text, skip_images)

    # 从 md 中提取所有 [[PIC:文件名]] 占位符，直接获取图片文件名
    images: list[ReportImage] = []
    seen_indices: set[int] = set()
    warnings: list[str] = []
    seen_warnings: set[str] = set()

    def _warn(message: str) -> None:
        if message not in seen_warnings:
            seen_warnings.add(message)
            warnings.append(message)
            logger.warning("%s", message)

    for pic_file in PIC_PATTERN.findall(md_text):
        candidate = md_path.parent / pic_file
        if not candidate.is_file():
            _warn(f"未找到图片文件: {candidate.name}")
            continue
        # 从文件名解析索引：{base_name}-P{index}.{ext}
        try:
            pic_idx = int(candidate.stem.split("-P")[-1])
        except (ValueError, IndexError):
            _warn(f"无法从图片文件名解析索引: {pic_file}")
            continue
        if pic_idx in seen_indices:
            continue
        seen_indices.add(pic_idx)
        images.append(ReportImage(index=pic_idx, ext=candidate.suffix.lstrip("."),
                                  data=candidate.read_bytes()))

    # 应用跳图策略：只保留 index > skip_images 的图片进行处理，
    # 但不改变图片原始编号（始终反映在文档中的实际位置）
    images = [image for image in images if image.index > skip_images]
    return ParsedReport(
        course=course, klass=klass, student_id=student_id, std_name=std_name,
        report_name=report_name, tag_dep=tag_dep,
        source_path=str(md_path), md_text=md_text, images=images,
        warnings=warnings, name_separator=separator,
    )


def save_parsed_batch(config: AppConfig, reports: list[ParsedReport], *,
                      stage: str = "doc", max_attempts: int = 2,
                      on_failure: Callable[[FailedFile], None] | None = None,
                      on_retry: Callable[[FailedFile], None] | None = None,
                      ) -> list[ParsedReport]:
    """多线程保存解析产物；单份失败进入异常队列并在整批结束后重试。"""
    def _save_one(report: ParsedReport) -> ParsedReport:
        save_parsed([report])
        return report

    outcome = run_file_batch(
        stage=stage,
        items=reports,
        worker=_save_one,
        workers=max(1, int(config.paths.parse_workers)),
        path_of=lambda report: Path(report.source_path),
        label_of=lambda report: report.base_name,
        unit="份",
        on_failure=on_failure,
        on_retry=on_retry,
        max_attempts=max_attempts,
    )
    logger.info("解析产物（md/图片）已落盘 %d 份报告", len(outcome.results))
    return outcome.results


def save_parsed(reports: list[ParsedReport]) -> None:
    """把解析出的 md 与图片落盘到与原文件相同路径。

    先保存该文档的全部图片，图片全部写成功后再写 md：中途失败时不会留下
    “md 已存在、图片不全”的产物，pic 阶段也不会基于不完整的产物继续处理。
    """
    for report in reports:
        target_dir = Path(report.source_path).parent
        for image in report.images:
            (target_dir / f"{report.base_name}-P{image.index}.{image.ext}").write_bytes(image.data)
        (target_dir / f"{report.base_name}.md").write_text(report.md_text, encoding="utf-8")
    logger.debug("解析产物（md/图片）已落盘 %d 份报告", len(reports))
