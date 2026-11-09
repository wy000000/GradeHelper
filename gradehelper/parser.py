"""报告解析：文档 → Markdown + 图片，全部驻留内存。

- 解析器：MinerU 4.x（``mineru.parser.parse``），支持 PDF / DOC / DOCX 等格式，
  版面分析、表格、图片与阅读顺序还原更准确；
- 解析失败即记为失败文档（无本地兜底解析器），由批处理重试并按阶段容错；
- 可处理的文档后缀由 ``config.yaml`` 的 ``paths.document_suffixes`` 决定，
  默认 ``.docx`` / ``.doc`` / ``.pdf``；
- 文件名格式：课程名称-专业班级-学号-姓名.ext（课程名称可含 '-'，按右侧三段切分）
- md 中图片位置使用占位符 [[PIC:Pn]] 或 [[PIC:base_name-Pn.ext]]，后续由 vllm 描述结果替换
"""

from __future__ import annotations

import logging
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

from .batch import run_file_batch
from .config import DEFAULT_DOCUMENT_SUFFIXES
from .models import ParsedReport, ReportImage

logger = logging.getLogger(__name__)


@contextmanager
def _quiet_mineru_logs() -> Iterator[None]:
    """解析期间抑制 MinerU 的 loguru INFO/DEBUG 日志，避免刷屏。

    MinerU 每页输出一条 infer 完成信息，而解析进度已由 run_file_batch 的进度条显示。
    临时将 loguru 级别提高到 WARNING，解析完成后恢复默认配置。
    """
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        yield
        return
    loguru_logger.remove()
    loguru_logger.add(sys.stderr, level="WARNING")
    try:
        yield
    finally:
        loguru_logger.remove()
        loguru_logger.add(sys.stderr, level="INFO")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".jp2"}
PIC_PATTERN = re.compile(r"\[\[PIC:([^\]]+)\]\]")

# MinerU 4.x 的本地解析档位；办公文档只支持 flash（原生解析），PDF/图片可用其它档位
MINERU_TIERS = {"flash", "basic", "standard", "advanced"}
MINERU_MARKDOWN_NAME = "markdown.md"


def parse_filename(
    path: Path,
    name_format: str = "{course}-{klass}-{student_id}-{stdName}-{reportName}",
    separator: str = "-",
    suffixes: tuple[str, ...] = DEFAULT_DOCUMENT_SUFFIXES,
) -> tuple[str, str, str, str, str] | None:
    """按可配置的命名格式解析文件名；不符合格式返回 None。

    name_format 中的占位符（如 {course}、{klass}、{student_id}、{stdName}、{reportName}）决定
    从文件名右侧切分出多少段以及每段对应的字段名；separator 为分隔符；
    suffixes 限定允许的文件扩展名（默认 .docx / .doc / .pdf）。
    """
    if path.suffix.lower() not in suffixes or path.name.startswith("~$"):
        return None
    fields = re.findall(r"\{(\w+)\}", name_format)
    n_fields = len(fields)
    if n_fields == 0:
        return None
    parts = path.stem.rsplit(separator, n_fields - 1)
    if len(parts) != n_fields:
        return None
    values = dict(zip(fields, parts))
    if not all(values.get(f, "").strip() for f in fields):
        return None
    course = values.get("course", "")
    klass = values.get("klass", "")
    student_id = values.get("student_id", "")
    std_name = values.get("stdName", "")
    report_name = values.get("reportName", "")
    return course, klass, student_id, std_name, report_name


def scan_reports(
    reports_dir: str | Path,
    name_format: str = "{course}-{klass}-{student_id}-{name}",
    separator: str = "-",
    suffixes: tuple[str, ...] = DEFAULT_DOCUMENT_SUFFIXES,
) -> list[Path]:
    """扫描目录本身（不含子目录），仅保留后缀与文件名格式都符合的报告。

    suffixes 默认为 .docx / .doc / .pdf；只扫描该目录顶层，不搜索子目录。
    """
    root = Path(reports_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"报告目录不存在: {root}")
    allowed = {s.lower() for s in suffixes}
    allowed_tuple = tuple(allowed)
    return [
        p for p in sorted(root.iterdir())
        if p.is_file()
        and p.suffix.lower() in allowed
        and parse_filename(path=p, name_format=name_format, separator=separator,
                           suffixes=allowed_tuple) is not None
    ]


def _sniff_ext(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return None


# ---------------------------------------------------------------- MinerU 主解析

# Markdown 图片语法 ![alt](path) 与 HTML <img src="path">；两条正则用于按正文顺序定位图片
_MD_IMAGE_REF = re.compile(r"!\[[^\]]*]\(\s*<?([^)>\s]+)>?\s*\)")
_HTML_IMAGE_REF = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_IMAGE_COMMENT = re.compile(r"<!--\s*image\s*-->", re.IGNORECASE)


def _iter_image_refs(md: str) -> list[tuple[int, int, str]]:
    """按在 Markdown 中出现的顺序返回 (起始, 结束, 图片引用) 列表。"""
    refs: list[tuple[int, int, str]] = []
    refs.extend((m.start(), m.end(), m.group(1)) for m in _MD_IMAGE_REF.finditer(md))
    refs.extend((m.start(), m.end(), m.group(1)) for m in _HTML_IMAGE_REF.finditer(md))
    refs.sort(key=lambda item: item[0])
    # 去掉重叠匹配（例如 Markdown 图片语法恰好位于 HTML 标签内）
    merged: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, ref in refs:
        if start >= last_end:
            merged.append((start, end, ref))
            last_end = end
    return merged


def _resolve_image_file(root: Path, ref: str) -> Path | None:
    """把 Markdown 中的图片引用解析为本地文件；远程/内联图片返回 None。"""
    lowered = ref.lower()
    if lowered.startswith(("data:", "http://", "https://")):
        return None
    clean = unquote(ref.split("#", 1)[0].split("?", 1)[0]).strip().replace("\\", "/")
    if not clean:
        return None
    path = Path(clean)
    return path if path.is_absolute() else root / clean


def _mineru_asset_dir(out_dir: Path) -> Path:
    """MinerU 保存结果的图片目录（不同版本可能为 images/ 或输出目录本身）。"""
    images_dir = out_dir / "images"
    return images_dir if images_dir.is_dir() else out_dir


def _read_mineru_markdown(out_dir: Path) -> str:
    """读取 MinerU 落盘的 Markdown；找不到标准文件名时退化为任意 .md。"""
    md_path = out_dir / MINERU_MARKDOWN_NAME
    if not md_path.is_file():
        candidates = sorted(p for p in out_dir.rglob("*.md") if p.is_file())
        if not candidates:
            raise RuntimeError("MinerU 未生成 Markdown 文件")
        md_path = candidates[0]
    return md_path.read_text(encoding="utf-8")


def _materialize_mineru_images(md: str, out_dir: Path, base_name: str,
                               warnings: list[str]) -> tuple[str, list[ReportImage]]:
    """把 MinerU 的图片引用替换为 [[PIC:base_name-Pn.ext]]，并读取图片二进制。

    图片按 Markdown 中的出现顺序编号；未被引用的图片（含 ``<!-- image -->`` 占位）
    依次回填，最终仍未定位的追加到正文末尾，确保图片不丢失。
    """
    images: list[ReportImage] = []
    replacements: list[tuple[int, int, str]] = []
    referenced: set[Path] = set()

    def _add_image(file_path: Path, data: bytes) -> str:
        ext = (_sniff_ext(data)
               or file_path.suffix.lower().lstrip(".")
               or "png")
        images.append(ReportImage(index=len(images) + 1, ext=ext, data=data))
        return f"[[PIC:{base_name}-P{images[-1].index}.{ext}]]"

    for start, end, ref in _iter_image_refs(md):
        file_path = _resolve_image_file(out_dir, ref)
        data: bytes | None = None
        if file_path is not None:
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                warnings.append(f"读取 MinerU 图片失败({ref}): {exc}")
        if data is None:
            replacements.append((start, end, ""))
            continue
        referenced.add(file_path.resolve())
        replacements.append((start, end, _add_image(file_path, data)))

    # 从后往前替换，避免前面的替换影响后面的偏移
    for start, end, replacement in reversed(replacements):
        md = md[:start] + replacement + md[end:]

    # 未被 Markdown 引用的图片文件（按路径排序，保持稳定顺序）
    unused: list[Path] = []
    for file_path in sorted(_mineru_asset_dir(out_dir).rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTS:
            continue
        if file_path.resolve() in referenced:
            continue
        unused.append(file_path)

    def _placeholder_for(file_path: Path) -> str:
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            warnings.append(f"读取 MinerU 图片失败({file_path.name}): {exc}")
            return ""
        return _add_image(file_path, data)

    # 先回填 MinerU 可能留下的 `<!-- image -->` 注释占位
    if unused:
        def _sub_comment(_match: re.Match) -> str:
            return _placeholder_for(unused.pop(0)) if unused else ""

        md = _IMAGE_COMMENT.sub(_sub_comment, md)

    # 仍未定位的图片追加到正文末尾
    trailing = [_placeholder_for(file_path) for file_path in unused]
    trailing = [item for item in trailing if item]
    if trailing:
        md = md.rstrip() + "\n\n" + "\n".join(trailing) + "\n"

    return md, images


def _mineru_tier(path: Path, configured: str) -> str:
    """选择 MinerU 解析档位。

    PDF/图片支持 flash/basic/standard/advanced，使用配置值（默认 standard）；
    办公文档（doc/docx 等）由 MinerU 以本地 flash 原生解析，不能使用其它档位。
    """
    if path.suffix.lower() in {".pdf"} | IMAGE_EXTS:
        tier = (configured or "standard").strip().lower()
        return tier if tier in MINERU_TIERS else "standard"
    return "flash"


def _mineru_parse(path: Path, base_name: str, warnings: list[str],
                  tier: str = "standard") -> tuple[str, list[ReportImage]]:
    """用 MinerU 解析文档，返回 (md 文本, 图片列表)；md 中图片位置为占位符。

    支持 PDF / DOC / DOCX（以及 MinerU 自身支持的其它格式，只要
    ``paths.document_suffixes`` 允许）。MinerU 先保存到临时目录，读取其
    ``markdown.md`` 与 ``images/`` 后再全部载入内存，临时目录随即删除。

    占位符格式：[[PIC:{base_name}-P{index}.{ext}]]
    """
    try:
        from mineru.parser import parse as mineru_parse
        from mineru.parser.writer import FileBasedDataWriter
    except ImportError as exc:  # 延迟导入：未安装 MinerU 时不影响其它阶段
        raise RuntimeError(
            "未安装 MinerU（需要 mineru>=4.0）：请先安装"
            'pip install "mineru>=4.0,<5"'
        ) from exc

    with tempfile.TemporaryDirectory(prefix="gradehelper-mineru-") as tmp:
        out_dir = Path(tmp)
        result = mineru_parse(str(path), tier=_mineru_tier(path, tier))
        result.save(FileBasedDataWriter(str(out_dir)))
        md = _read_mineru_markdown(out_dir)
        return _materialize_mineru_images(md, out_dir, base_name, warnings)


# ------------------------------------------------------------------------ 对外接口

def parse_report(
    path: Path,
    name_format: str = "{course}-{klass}-{student_id}-{stdName}-{reportName}",
    separator: str = "-",
    tag_dep: str = "系部专业",
    suffixes: tuple[str, ...] = DEFAULT_DOCUMENT_SUFFIXES,
    mineru_tier: str = "standard",
) -> ParsedReport:
    """解析单份报告：MinerU 解析失败即抛出异常（无本地兜底）。"""
    meta = parse_filename(path, name_format=name_format, separator=separator,
                          suffixes=suffixes)
    if meta is None:
        raise ValueError(
            f"文件名不符合命名格式 '{name_format}'（分隔符: {separator!r}）: {path.name}"
        )
    course, klass, student_id, std_name, report_name = meta

    warnings: list[str] = []
    base_name = f"{course}{separator}{klass}{separator}{student_id}{separator}{std_name}{separator}{report_name}"
    try:
        md, images = _mineru_parse(path, base_name, warnings, tier=mineru_tier)
    except Exception as exc:  # MinerU 依赖模型/外部文件，任何异常都记为失败文档
        raise RuntimeError(f"MinerU 解析失败: {exc}") from exc

    return ParsedReport(course=course, klass=klass, student_id=student_id, std_name=std_name,
                        report_name=report_name, tag_dep=tag_dep,
                        source_path=str(path), md_text=md, images=images, warnings=warnings,
                        name_separator=separator)


def parse_all(
    reports_dir: str | Path,
    name_format: str = "{course}-{klass}-{student_id}-{stdName}-{reportName}",
    separator: str = "-",
    tag_dep: str = "系部专业",
    suffixes: tuple[str, ...] = DEFAULT_DOCUMENT_SUFFIXES,
    mineru_tier: str = "standard",
    on_error: Callable[[Path, Exception], None] | None = None,
    workers: int = 1,
    max_attempts: int = 2,
    on_retry: Callable[[Path, Exception, int], None] | None = None,
) -> list[ParsedReport]:
    """扫描并多线程解析目录下所有合规报告，解析结果全部驻留内存。

    suffixes 决定扫描哪些文档后缀（默认 .docx / .doc / .pdf）；
    mineru_tier 决定 PDF/图片的解析档位（办公文档固定使用 MinerU 的 flash 原生解析）。

    - 单份文档解析失败不会中断其它文档；
    - 失败文件进入异常队列，全部解析完成后自动重试；
    - 每次失败且还会重试时通过 ``on_retry`` 记录中间错误；
    - 重试结束仍失败的文档通过 ``on_error`` 回调记录，其余文档照常返回。

    ``on_error`` 只在重试结束后对最终仍失败的文档调用一次，
    因此不会因重试产生重复的失败记录。
    """
    paths = scan_reports(reports_dir, name_format=name_format, separator=separator,
                         suffixes=suffixes)

    def _on_final_failure(failed) -> None:
        logger.error("解析失败 %s: %s", failed.path.name, failed.reason)
        if on_error is not None:
            try:
                on_error(failed.path, failed.error)
            except Exception:  # noqa: BLE001 记录失败不应影响解析流程
                logger.debug("解析失败回调异常", exc_info=True)

    def _on_retry_failure(failed) -> None:
        if on_retry is not None:
            try:
                on_retry(failed.path, failed.error, failed.attempts)
            except Exception:  # noqa: BLE001 记录失败不应影响解析流程
                logger.debug("解析中间失败回调异常", exc_info=True)

    with _quiet_mineru_logs():
        outcome = run_file_batch(
            stage="doc",
            items=paths,
            worker=lambda path: parse_report(
                path, name_format=name_format, separator=separator, tag_dep=tag_dep,
                suffixes=suffixes, mineru_tier=mineru_tier),
            workers=max(1, int(workers)),
            path_of=lambda path: path,
            label_of=lambda path: path.name,
            unit="份",
            on_failure=_on_final_failure,
            on_retry=_on_retry_failure,
            max_attempts=max_attempts,
        )
    return outcome.results


def _caption_block_span(md: str, template: str, index: int) -> tuple[int, int] | None:
    """定位 md 中某张图片旧描述块的 (起始, 结束) 字节偏移。

    描述块由 image_context 模板生成，形如 "[图片描述 P{n}: {caption}]"。
    采用括号计数法定位与开头 '[' 配对的闭合 ']'，因此 caption 中即便
    含有 '[' 或 ']' 也能正确匹配整块。末尾的可选换行一并计入结束偏移，
    以便重复运行时用新描述块原位替换旧块。
    """
    prefix = template.split("{caption}", 1)[0].format(n=index)
    start = md.find(prefix)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(md)):
        ch = md[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(md) and md[end] == "\n":
                    end += 1
                return start, end
    return None


def embed_captions(md_text: str, images: list[ReportImage], template: str,
                   base_name: str = "") -> str:
    """把图片描述嵌入 md 中对应占位符位置。

    - 占位符 [[PIC:Pn]] 或 [[PIC:base_name-Pn.ext]] 始终保留（不消耗），描述块放在占位符之后；
    - 首次运行：在占位符后追加描述块；
    - 重复运行：若已存在旧描述块则原位替换为新描述；
    - 不再清除未替换的占位符，以便 pic 阶段可重复执行。
    """
    result = md_text
    for img in images:
        if not img.caption:
            continue
        block = template.format(n=img.index, caption=img.caption.strip())
        if base_name:
            placeholder = f"[[PIC:{base_name}-P{img.index}.{img.ext}]]"
        else:
            placeholder = f"[[PIC:P{img.index}]]"
        span = _caption_block_span(result, template, img.index)
        if span is not None:
            start, end = span
            result = result[:start] + block + result[end:]
        elif placeholder in result:
            result = result.replace(placeholder, placeholder + block, 1)
    return re.sub(r"\n{3,}", "\n\n", result).strip() + "\n"


# ------------------------------------------------------------------------ md 清理（doc 阶段）

def _placeholder_index(token: str) -> int | None:
    """从占位符内容解析图片编号 n；兼容 "Pn" 与 "base_name-Pn.ext" 两种格式。"""
    match = re.search(r"P(\d+)(?:\..*)?$", token)
    return int(match.group(1)) if match else None


def remove_skipped_placeholders(md: str, skip_images: int) -> str:
    """移除被跳过图片（编号 1..skip_images）的占位符，其余占位符保持原样。

    md 中的占位符可能是短格式 [[PIC:Pn]]（测试或旧版产物），也可能是解析时写入的
    完整格式 [[PIC:base_name-Pn.ext]]；无法解析出编号的占位符不删除。
    """
    if skip_images <= 0 or "[[PIC:" not in md:
        return md

    def _sub(match: re.Match) -> str:
        index = _placeholder_index(match.group(1))
        return "" if index is not None and index <= skip_images else match.group(0)

    return PIC_PATTERN.sub(_sub, md)


def strip_blank_lines(md: str) -> str:
    """去除空行（含仅含空白字符的行），非空行内容原样保留。

    结果非空时以单个换行结尾；仅删除空行，不合并相邻的非空行。
    """
    lines = [line for line in md.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"