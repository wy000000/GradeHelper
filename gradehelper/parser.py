"""报告解析：docx → Markdown + 图片，全部驻留内存。

- 主解析器：docling（版面分析，表格/图片/阅读顺序还原更准确）
- 兜底解析器：python-docx（docling 失败时使用）
- 文件名格式：课程名称-专业班级-学号-姓名.docx（课程名称可含 '-'，按右侧三段切分）
- md 中图片位置使用占位符 [[PIC:Pn]] 或 [[PIC:base_name-Pn.ext]]，后续由 vllm 描述结果替换
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from .batch import run_file_batch
from .models import ParsedReport, ReportImage

logger = logging.getLogger(__name__)

DOCX_SUFFIX = ".docx"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
PIC_PATTERN = re.compile(r"\[\[PIC:([^\]]+)\]\]")


def parse_filename(
    path: Path,
    name_format: str = "{course}-{klass}-{student_id}-{stdName}-{reportName}",
    separator: str = "-",
    suffixes: tuple[str, ...] = (DOCX_SUFFIX,),
) -> tuple[str, str, str, str, str] | None:
    """按可配置的命名格式解析文件名；不符合格式返回 None。

    name_format 中的占位符（如 {course}、{klass}、{student_id}、{stdName}、{reportName}）决定
    从文件名右侧切分出多少段以及每段对应的字段名；separator 为分隔符；
    suffixes 限定允许的文件扩展名（默认仅 .docx）。
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
) -> list[Path]:
    """扫描目录（含子目录），仅保留文件名格式正确的 docx。"""
    root = Path(reports_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"报告目录不存在: {root}")
    return [
        p for p in sorted(root.rglob("*"))
        if p.is_file()
        and p.suffix.lower() == DOCX_SUFFIX
        and parse_filename(path=p, name_format=name_format, separator=separator) is not None
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


def _content_key(data: bytes) -> str:
    import hashlib
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------- docling 主解析

def _extract_picture_bytes(doc, item, warnings: list[str]) -> tuple[bytes | None, str | None]:
    """从 docling 文档工件中取图片二进制与文件名。"""
    art_no = getattr(item, "image_item_no", None)
    if art_no is None:
        return None, None
    try:
        artifact = doc.artifacts[art_no]
    except (IndexError, KeyError, TypeError) as exc:
        warnings.append(f"读取 docling 图片工件失败(#{art_no}): {exc}")
        return None, None
    binary = getattr(artifact, "binary", None)
    if binary:
        return bytes(binary), _artifact_name(artifact)
    uri = getattr(artifact, "uri", None)
    if uri:
        try:
            return Path(uri).read_bytes(), Path(uri).name
        except OSError as exc:
            warnings.append(f"读取 docling 图片文件失败({uri}): {exc}")
    return None, None


def _artifact_name(artifact) -> str:
    uri = getattr(artifact, "uri", None)
    return Path(uri).name if uri else ""


def _replace_image_ref(md: str, art_name: str, placeholder: str) -> str:
    """把 md 中引用 art_name 的图片语法替换为占位符。"""
    pattern = re.compile(
        r"!\[[^\]]*]\(\s*<?[^)\s]*" + re.escape(art_name) + r"[^)\s]*>?\s*\)",
        re.IGNORECASE,
    )
    new_md, n = pattern.subn(placeholder, md, count=1)
    if n == 0:  # 退化：仅替换文件名
        new_md = md.replace(art_name, placeholder, 1)
    return new_md


def _docling_parse(path: Path, base_name: str,
                   warnings: list[str]) -> tuple[str, list[ReportImage]]:
    """用 docling 解析 docx，返回 (md 文本, 图片列表)。md 中图片位置为占位符。

    docling 对 DOCX 默认不导出图片工件（Markdown 中为 `<!-- image -->` 注释），
    因此 Markdown 由 docling 生成，图片二进制在工件不可用时用 python-docx
    按正文顺序提取后依次回填占位位置。

    占位符格式：[[PIC:{base_name}-P{index}.{ext}]]
    """
    from docling.document_converter import DocumentConverter

    doc = DocumentConverter().convert(str(path)).document
    md = doc.export_to_markdown() or ""

    # 1) 优先尝试 docling 自带图片工件（不同版本支持程度不同）
    pictures = sorted(
        doc.pictures,
        key=lambda it: ((getattr(it, "page_no", 0) or 0),
                        (getattr(getattr(it, "bbox", None), "t", 0) or 0)),
    )
    images: list[ReportImage] = []
    artifact_names: list[str] = []
    for item in pictures:
        data, art_name = _extract_picture_bytes(doc, item, warnings)
        if data is None:
            continue
        ext = _sniff_ext(data) or Path(art_name or "").suffix.lstrip(".") or "png"
        images.append(ReportImage(index=len(images) + 1, ext=ext, data=data))
        if art_name:
            artifact_names.append(art_name)

    # 2) 工件不可用时，用 python-docx 按正文出现顺序提取
    if not images:
        images = _extract_ordered_images(path, warnings)

    # 3) 把 md 中的图片位置替换为占位符 [[PIC:base_name-Pn.ext]]
    for idx, art_name in enumerate(artifact_names, start=1):
        if idx <= len(images) and art_name and art_name in md:
            img = images[idx - 1]
            md = _replace_image_ref(md, art_name, f"[[PIC:{base_name}-P{img.index}.{img.ext}]]")
    md = _place_placeholders(md, images, base_name)

    return md, images


def _extract_ordered_images(path: Path, warnings: list[str]) -> list[ReportImage]:
    """python-docx 按正文出现顺序提取图片（内容去重，保持首现顺序）。"""
    import docx
    from docx.oxml.ns import qn

    document = docx.Document(str(path))
    images: list[ReportImage] = []
    seen: set[str] = set()
    for blip in document.element.body.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid or rid not in document.part.rels:
            if rid:
                warnings.append(f"图片关系缺失，无法读取: {rid}")
            continue
        try:
            blob = document.part.rels[rid].target_part.blob
        except (KeyError, OSError) as exc:
            warnings.append(f"读取文档图片失败({rid}): {exc}")
            continue
        key = _content_key(blob)
        if key in seen:
            continue
        seen.add(key)
        images.append(ReportImage(index=len(images) + 1,
                                  ext=_sniff_ext(blob) or "png", data=blob))
    if not images:  # 最终兜底：word/media 全量
        images = [ReportImage(index=i, ext=ext, data=data)
                  for i, (data, ext) in enumerate(_extract_media(path, warnings), start=1)]
    return images


def _place_placeholders(md: str, images: list[ReportImage], base_name: str) -> str:
    """按出现顺序把 md 中的图片位置替换为 [[PIC:base_name-Pn.ext]]；未定位的占位符追加到文末。"""
    if not images:
        return md
    n = 0

    def _sub_comment(_match: re.Match) -> str:
        nonlocal n
        n += 1
        if n <= len(images):
            img = images[n - 1]
            return f"[[PIC:{base_name}-P{img.index}.{img.ext}]]"
        return ""

    # docling 的 `<!-- image -->` 注释占位
    md = re.sub(r"<!--\s*image\s*-->", _sub_comment, md, flags=re.IGNORECASE)
    if n < len(images):  # 剩余图片按 markdown 图片语法位置回填

        def _sub_mdimg(_match: re.Match) -> str:
            nonlocal n
            n += 1
            if n <= len(images):
                img = images[n - 1]
                return f"[[PIC:{base_name}-P{img.index}.{img.ext}]]"
            return ""

        md = re.sub(r"!\[[^\]]*]\([^)]*\)", _sub_mdimg, md)

    total = len(PIC_PATTERN.findall(md))
    if total < len(images):  # 仍未定位的图片，占位符追加到文末
        existing_indices = {
            int(m.split("-P")[-1].split(".")[0])
            for m in PIC_PATTERN.findall(md)
            if "-P" in m
        }
        missing = [img for img in images if img.index not in existing_indices]
        md += "\n\n" + "\n".join(
            f"[[PIC:{base_name}-P{img.index}.{img.ext}]]" for img in missing
        )
    elif total > len(images):  # 多余占位符清理
        for img in images:
            placeholder = f"[[PIC:{base_name}-P{img.index}.{img.ext}]]"
            count = md.count(placeholder)
            if count > 1:
                md = md.replace(placeholder, "", count - 1)
    return md


# ------------------------------------------------------------ python-docx 兜底解析

def _looks_like_list(text: str) -> bool:
    stripped = text.lstrip()
    return bool(re.match(r"^([-*·•]|\d+[.、)]|[（(]\d+[)）])\s+\S", stripped))


def _table_to_md(table) -> list[str]:
    rows = []
    for r in table.rows:
        cells = [c.text.strip().replace("\n", " ") for c in r.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if len(rows) >= 2:
        rows.insert(1, "|" + "---|" * len(table.columns))
    return rows


def _collect_inline_images(para, images: list[ReportImage], seen: set[str],
                           warnings: list[str]) -> None:
    """按行内 blip 关系提取图片 bytes，段落文本中追加 [[IMG:n]] 占位。"""
    from docx.oxml.ns import qn

    part = para.part
    for blip in para._p.findall(".//" + qn("a:blip")):
        rid = blip.get(qn("r:embed"))
        if not rid or rid not in part.rels:
            if rid:
                warnings.append(f"图片关系缺失，无法读取: {rid}")
            continue
        try:
            blob = part.rels[rid].target_part.blob
        except (KeyError, OSError) as exc:
            warnings.append(f"读取文档图片失败({rid}): {exc}")
            continue
        key = _content_key(blob)
        if key in seen:
            continue
        seen.add(key)
        ext = _sniff_ext(blob) or "png"
        images.append(ReportImage(index=len(images) + 1, ext=ext, data=blob))
        para.text += f"[[IMG:{len(images)}]]"  # 仅内存对象，不影响磁盘文件


def _extract_media(path: Path, warnings: list[str] | None = None) -> list[tuple[bytes, str]]:
    """直接从 docx 压缩包的 word/media 提取全部图片（兜底补充）。"""
    import zipfile

    out: list[tuple[bytes, str]] = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in sorted(z.namelist()):
                if name.startswith("word/media/"):
                    data = z.read(name)
                    sniffed = _sniff_ext(data)
                    ext = sniffed or Path(name).suffix.lstrip(".").lower() or "png"
                    if f".{ext}" in IMAGE_EXTS or sniffed:
                        out.append((data, ext))
    except (OSError, zipfile.BadZipFile) as exc:
        if warnings is not None:
            warnings.append(f"读取 docx 压缩包图片失败: {exc}")
    return out


def _docx_fallback_parse(path: Path, base_name: str,
                         warnings: list[str]) -> tuple[str, list[ReportImage]]:
    """python-docx 兜底：段落/表格转 md，图片按行内关系 + word/media 提取。"""
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(path))
    lines: list[str] = []
    images: list[ReportImage] = []
    seen: set[str] = set()

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, document)
            _collect_inline_images(para, images, seen, warnings)
            text = para.text.strip()
            if not text:
                lines.append("")
                continue
            style = (para.style.name or "") if para.style is not None else ""
            if style.lower().startswith("heading"):
                try:
                    level = min(int(style.split()[-1]), 6)
                except (ValueError, IndexError):
                    level = 2
                lines.append("#" * level + " " + text)
            elif _looks_like_list(text):
                lines.append("- " + text.lstrip("-*·• \t"))
            else:
                lines.append(text)
        elif child.tag == qn("w:tbl"):
            lines.extend(_table_to_md(Table(child, document)))

    # word/media 中未通过行内关系取到的图片，按压缩包内顺序补充
    for data, ext in _extract_media(path, warnings):
        if _content_key(data) in seen:
            continue
        images.append(ReportImage(index=len(images) + 1, ext=ext, data=data))

    md = "\n\n".join(lines)
    for img in images:
        md = md.replace(
            f"[[IMG:{img.index}]]",
            f"[[PIC:{base_name}-P{img.index}.{img.ext}]]",
            1,
        )
    located = {m.split("-P")[-1].split(".")[0] for m in PIC_PATTERN.findall(md) if "-P" in m}
    missing = [img for img in images if str(img.index) not in located]
    if missing:
        md += "\n\n" + "\n".join(
            f"[[PIC:{base_name}-P{img.index}.{img.ext}]]" for img in missing
        )
    return md, images


# ------------------------------------------------------------------------ 对外接口

def parse_report(
    path: Path,
    name_format: str = "{course}-{klass}-{student_id}-{stdName}-{reportName}",
    separator: str = "-",
    tag_dep: str = "系部专业",
) -> ParsedReport:
    """解析单份报告；docling 优先，失败回退 python-docx。"""
    meta = parse_filename(path, name_format=name_format, separator=separator)
    if meta is None:
        raise ValueError(
            f"文件名不符合命名格式 '{name_format}'（分隔符: {separator!r}）: {path.name}"
        )
    course, klass, student_id, std_name, report_name = meta

    warnings: list[str] = []
    base_name = f"{course}{separator}{klass}{separator}{student_id}{separator}{std_name}{separator}{report_name}"
    try:
        md, images = _docling_parse(path, base_name, warnings)
    except Exception as exc:  # docling 依赖模型，任何异常都走兜底
        warnings.append(f"docling 解析失败，回退 python-docx: {exc}")
        logger.warning("%s: docling 解析失败(%s)，回退 python-docx", path.name, exc)
        try:
            md, images = _docx_fallback_parse(path, base_name, warnings)
        except Exception as fallback_exc:
            # 兜底也失败：把两次失败原因都带上，便于运行日志定位中间错误
            raise RuntimeError(
                f"docling 解析失败({exc})，python-docx 兜底也失败({fallback_exc})"
            ) from fallback_exc

    return ParsedReport(course=course, klass=klass, student_id=student_id, std_name=std_name,
                        report_name=report_name, tag_dep=tag_dep,
                        source_path=str(path), md_text=md, images=images, warnings=warnings,
                        name_separator=separator)


def parse_all(
    reports_dir: str | Path,
    name_format: str = "{course}-{klass}-{student_id}-{stdName}-{reportName}",
    separator: str = "-",
    tag_dep: str = "系部专业",
    on_error: Callable[[Path, Exception], None] | None = None,
    workers: int = 1,
    max_attempts: int = 2,
    on_retry: Callable[[Path, Exception, int], None] | None = None,
) -> list[ParsedReport]:
    """扫描并多线程解析目录下所有合规报告，解析结果全部驻留内存。

    - 单份文档解析失败不会中断其它文档；
    - 失败文件进入异常队列，全部解析完成后自动重试；
    - 每次失败且还会重试时通过 ``on_retry`` 记录中间错误；
    - 重试结束仍失败的文档通过 ``on_error`` 回调记录，其余文档照常返回。

    ``on_error`` 只在重试结束后对最终仍失败的文档调用一次，
    因此不会因重试产生重复的失败记录。
    """
    paths = scan_reports(reports_dir, name_format=name_format, separator=separator)

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

    outcome = run_file_batch(
        stage="doc",
        items=paths,
        worker=lambda path: parse_report(
            path, name_format=name_format, separator=separator, tag_dep=tag_dep),
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

    md 中的占位符可能是短格式 [[PIC:Pn]]（测试/兜底），也可能是解析时写入的
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