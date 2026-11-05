"""结果文件路径：命名、推断与可写检查（不写模板文件）。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import AppConfig
from .models import ParsedReport
from .parser import parse_filename, scan_reports

logger = logging.getLogger(__name__)


def format_result_name(fmt: str, course: str, tag_dep: str, report_name: str) -> str:
    """按 result_name_format 生成结果文件名，并清理非法文件名字符。"""
    name = fmt.format(course=course, tagDep=tag_dep or "系部专业",
                      reportName=report_name)
    # 禁止路径分隔符等非法文件名字符，确保结果文件与模板同目录
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def resolve_result_path(config: AppConfig, template: str | Path,
                        reports: list[ParsedReport],
                        output: str | Path | None = None) -> Path:
    """确定评分结果 xlsx 路径（不写模板文件）。

    - 显式 output（--output / paths.output）优先；
    - 否则按 config.yaml 的 paths.result_name_format 生成文件名，落在与模板相同目录；
      占位符：{course} {tagDep} {reportName}，取自第一份报告（同批次应一致）。
    """
    if output:
        return Path(output)
    fmt = (config.paths.result_name_format or "").strip()
    if not fmt:
        raise ValueError("config.yaml 缺少 paths.result_name_format 配置，且未指定 --output")
    if not reports:
        raise ValueError("无报告可用于生成结果文件名（或请显式指定 --output）")
    first = reports[0]
    courses = {r.course for r in reports}
    report_names = {r.report_name for r in reports}
    if len(courses) > 1 or len(report_names) > 1:
        logger.warning("报告目录包含多个课程/报告类型，结果文件名取第一份: %s", first.base_name)
    name = format_result_name(fmt, first.course, first.tag_dep, first.report_name)
    return Path(template).parent / name


def resolve_result_path_hint(config: AppConfig, template: str | Path,
                             reports_dir: str | Path | None,
                             output: str | Path | None = None,
                             tag_dep: str = "系部专业") -> Path | None:
    """阶段开始前估算结果文件路径（不解析 docx/md 正文）。

    与 resolve_result_path 使用同一命名规则，仅依据文件名推断课程与报告名：
    - 显式 output（--output / paths.output）优先；
    - 否则扫描报告目录，取第一份符合命名格式的 docx（没有则退化为 .md）；
    - 无法推断（无报告、目录不存在或未配置 result_name_format）时返回 None。
    """
    output = output or (config.paths.output or "").strip() or None
    if output:
        return Path(output)
    fmt = (config.paths.result_name_format or "").strip()
    if not fmt or reports_dir is None:
        return None
    root = Path(reports_dir)
    name_format = config.paths.report_name_format
    separator = config.paths.report_name_separator
    candidates: list[Path] = []
    try:
        candidates = scan_reports(root, name_format=name_format, separator=separator)
    except OSError:
        candidates = []
    if not candidates:
        # 只跑了 pic/sen 时目录里可能只剩 md（docx 已被移走）
        try:
            candidates = sorted(root.glob("*.md"))
        except OSError:
            candidates = []
    for path in candidates:
        meta = parse_filename(path, name_format=name_format, separator=separator,
                              suffixes=(path.suffix.lower(),))
        if meta is not None:
            course, _klass, _sid, _name, report_name = meta
            return Path(template).parent / format_result_name(
                fmt, course, tag_dep, report_name)
    return None


def ensure_result_writable(result_path: str | Path | None) -> None:
    """doc / sen 阶段开始前检查结果文件是否被其它程序占用。

    结果文件不存在（首次运行）视为可写；存在时尝试以读写方式打开：
    被其它程序独占（Windows 下 Excel 打开 xlsx 会拒绝写入）时抛 RuntimeError，
    由 CLI 提示后退出（退出码 1），该阶段不做任何解析与评分。
    """
    if result_path is None:
        return
    path = Path(result_path)
    if not path.exists():
        return
    try:
        with path.open("r+b"):
            pass
    except OSError as exc:
        raise RuntimeError(
            f"结果文件无法写入，可能已被其它程序（如 Excel）打开，请关闭后重试: {path}"
        ) from exc
