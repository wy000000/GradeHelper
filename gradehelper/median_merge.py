"""多份评分结果按学号取总分中位数，合成新的评分结果（median 子命令）。

流程：匹配 ``{主名称}-<n>.xlsx`` → 全部读入内存 → 学号一致性硬校验（不一致
按「学号-姓名-班级」列出异常并终止，不生成结果文件）→ 按
``statistics.median_high`` 取每个学生总分的中位数（奇数取中、偶数就高取；
整行复制中位数所在源文件的那一行，同分取 n 最小的文件）→ 整体一次性写入
``{主名称}-median.xlsx``。运行信息写入同目录 ``{主名称}-median.log``。
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from .excel_io import RESULT_SHEET
from .result_paths import ensure_result_writable

logger = logging.getLogger(__name__)

# result 表必须存在的列（按表头文字定位，容忍列顺序变化）
_REQUIRED_COLUMNS = ("学号", "姓名", "班级", "总分")
# 公式单项：E2、$E$2、E2*1、E2*0.15（权重缺省视为 1）
_TERM_RE = re.compile(
    r"^\$?([A-Z]{1,3})\$?(\d+)"
    r"(?:\*\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?))?$"
)
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class MedianError(Exception):
    """median 流程的可预期错误（输入不合法等），由 CLI 转为退出码 1。"""


@dataclass
class _Row:
    """result 表中的一行（以学号唯一定位）。"""

    student_id: str
    std_name: str
    klass: str
    values: list          # 各列单元格值（与表头对齐；总分列为缓存值/None）
    total: float | None   # 解析出的总分；无法解析为 None（不参与中位数）
    formula: str | None   # 总分列原始公式（无公式为 None）


@dataclass
class _Table:
    """一份输入文件的 result 表（学号 -> 行，保持行序）。"""

    path: Path
    headers: list[str]
    rows: dict[str, _Row]


# ------------------------------------------------------------ 输入文件匹配

def find_inputs(base: str, directory: Path) -> tuple[list[tuple[int, Path]], list[str]]:
    """匹配 ``{base}-<n>.xlsx`` 并按 n 数值升序排序。

    返回 ``(匹配列表, 被忽略的相关文件名)``：只考虑文件名以 base 开头的文件，
    其中不匹配 ``-{n}.xlsx`` 的（如 base.xlsx、base-median.xlsx、base-median.log）
    一律忽略不读，列入返回的忽略清单。
    """
    pattern = re.compile(rf"^{re.escape(base)}-(\d+)\.xlsx$", re.IGNORECASE)
    prefix = base.lower()
    matched: list[tuple[int, Path]] = []
    ignored: list[str] = []
    for entry in directory.iterdir():
        if not entry.is_file() or entry.name.startswith("~$"):
            continue
        if not entry.name.lower().startswith(prefix):
            continue
        m = pattern.match(entry.name)
        if m:
            matched.append((int(m.group(1)), entry))
        else:
            ignored.append(entry.name)
    matched.sort(key=lambda item: item[0])
    ignored.sort()
    return matched, ignored


# ------------------------------------------------------------ 单文件读取

def _as_float(value) -> float | None:
    """把单元格值转换为 float；空值或无法转换时返回 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _total_from_formula(formula: str, values: list) -> float | None:
    """按公式内嵌权重计算总分（``=E2*1+F2*0.15...``）；无法解析返回 None。

    权重写在公式里，因此无需评分模板；引用到的空单元格按 0 计。
    """
    body = formula.strip()
    if body.startswith("="):
        body = body[1:]
    if not body:
        return None
    total = 0.0
    for term in body.split("+"):
        m = _TERM_RE.match(term.strip())
        if m is None:
            return None
        try:
            col_idx = column_index_from_string(m.group(1)) - 1
        except ValueError:
            return None
        weight = float(m.group(3)) if m.group(3) else 1.0
        value = _as_float(values[col_idx]) if 0 <= col_idx < len(values) else None
        total += (value if value is not None else 0.0) * weight
    return total


def _resolve_total(values: list, total_idx: int, raw_total) -> tuple[float | None, str | None]:
    """解析总分，返回 ``(总分, 源公式)``。

    三级回退：data_only 缓存值（Excel 打开保存过）→ 数值直写 → 公式内嵌权重
    自算（纯 openpyxl 产物无缓存的场景）；皆不可用时总分为 None。
    """
    formula = raw_total if isinstance(raw_total, str) and raw_total.startswith("=") else None
    cached = _as_float(values[total_idx])
    if cached is not None:
        return cached, formula
    direct = _as_float(raw_total)
    if direct is not None:
        return direct, formula
    if formula is not None:
        computed = _total_from_formula(formula, values)
        if computed is not None:
            return computed, formula
    return None, formula


def read_result_table(path: str | Path) -> _Table:
    """读取一份评分结果文件的 ``result`` 表；结构不合法时抛 MedianError。"""
    path = Path(path)
    try:
        wb_val = load_workbook(path, data_only=True)
        wb_raw = load_workbook(path, data_only=False)
    except Exception as exc:  # noqa: BLE001 — 统一转为可预期错误
        raise MedianError(f"无法读取 {path.name}: {exc}") from exc
    if RESULT_SHEET not in wb_val.sheetnames:
        raise MedianError(f"{path.name} 缺少 '{RESULT_SHEET}' sheet，无法读取")
    ws_val = wb_val[RESULT_SHEET]
    ws_raw = (wb_raw[RESULT_SHEET] if RESULT_SHEET in wb_raw.sheetnames else ws_val)

    headers = [str(c.value).strip() if c.value is not None else "" for c in ws_val[1]]
    while headers and headers[-1] == "":  # 去掉尾部空表头，避免空白列干扰表头比对
        headers.pop()
    col_idx: dict[str, int] = {}
    for name in _REQUIRED_COLUMNS:
        try:
            col_idx[name] = headers.index(name)
        except ValueError:
            raise MedianError(f"{path.name} 的 result 表缺少「{name}」列") from None

    rows: dict[str, _Row] = {}
    for r in range(2, ws_val.max_row + 1):
        sid_raw = ws_val.cell(row=r, column=col_idx["学号"] + 1).value
        if sid_raw is None or not str(sid_raw).strip():
            continue
        sid = str(sid_raw).strip()
        std_name = str(ws_val.cell(row=r, column=col_idx["姓名"] + 1).value or "").strip()
        klass = str(ws_val.cell(row=r, column=col_idx["班级"] + 1).value or "").strip()
        if sid in rows:
            other = rows[sid]
            raise MedianError(
                f"{path.name} 内学号重复，无法唯确定位行: "
                f"{sid}-{other.std_name}-{other.klass} / {sid}-{std_name}-{klass}"
            )
        values = [ws_val.cell(row=r, column=c).value for c in range(1, len(headers) + 1)]
        raw_total = ws_raw.cell(row=r, column=col_idx["总分"] + 1).value
        total, formula = _resolve_total(values, col_idx["总分"], raw_total)
        if total is None:
            logger.warning("%s 学号 %s 总分无法解析（既无数值也无可解析公式），"
                           "该样本不参与中位数", path.name, sid)
        rows[sid] = _Row(student_id=sid, std_name=std_name, klass=klass,
                         values=values, total=total, formula=formula)
    return _Table(path=path, headers=headers, rows=rows)


# ------------------------------------------------------------ 一致性校验

def check_student_consistency(tables: list[_Table]) -> None:
    """校验各文件学号集合完全一致，并校验同学号的姓名/班级跨文件一致。

    不一致时抛 MedianError，异常按「学号-姓名-班级」逐条列出（含缺少于/仅
    存在于的文件名单），由调用方终止——读入之后、任何合成与写盘之前。
    """
    presence: dict[str, list[int]] = {}
    variants: dict[str, list[tuple[str, str, str]]] = {}  # sid -> [(文件名, 姓名, 班级)]
    order: list[str] = []
    for ti, table in enumerate(tables):
        for sid, row in table.rows.items():
            if sid not in presence:
                presence[sid] = []
                variants[sid] = []
                order.append(sid)
            presence[sid].append(ti)
            entry = (table.path.name, row.std_name, row.klass)
            if entry not in variants[sid]:
                variants[sid].append(entry)

    missing_problems: list[str] = []
    identity_problems: list[str] = []
    for sid in order:
        idxs = presence[sid]
        entries = variants[sid]
        label = f"{sid}-{entries[0][1]}-{entries[0][2]}"
        if len(idxs) < len(tables):
            missing_files = [t.path.name for i, t in enumerate(tables) if i not in idxs]
            present_files = [t.path.name for i, t in enumerate(tables) if i in idxs]
            missing_problems.append(
                f"{label}  缺少于: {', '.join(missing_files)}；"
                f"仅存在于: {', '.join(present_files)}")
        if len({(n, k) for _f, n, k in entries}) > 1:
            detail = "；".join(f"{f}={n}-{k}" for f, n, k in entries)
            identity_problems.append(f"{label}  姓名/班级跨文件不一致: {detail}")

    problems = missing_problems + identity_problems
    if not problems:
        return
    if missing_problems and identity_problems:
        header = "学号集合不一致（含姓名/班级跨文件不一致），异常记录（学号-姓名-班级）："
    elif missing_problems:
        header = "学号集合不一致，异常记录（学号-姓名-班级）："
    else:
        header = "同学号的姓名/班级跨文件不一致，异常记录（学号-姓名-班级）："
    raise MedianError(
        header + "\n" + "\n".join(f"  {p}" for p in problems)
        + f"\n共 {len(problems)} 条异常，已终止（不生成 median 结果）")


def check_headers(tables: list[_Table]) -> list[str]:
    """校验各输入文件表头完全一致（整行复制按列位置对齐的前提），返回表头。"""
    headers = tables[0].headers
    for t in tables[1:]:
        if t.headers != headers:
            raise MedianError(
                "各输入文件 result 表表头不一致，无法合并：\n"
                + "\n".join(f"  {t.path.name}: {' | '.join(t.headers)}" for t in tables))
    return headers


# ------------------------------------------------------------ 中位数合成

def merge_median(tables: list[_Table]) -> list[_Row]:
    """按学号取总分中位数（median_high，偶数就高取），整行复制中位数所在行。

    前提：已通过 check_student_consistency 与 check_headers；tables 按 n 升序。
    输出行序 = 首个（n 最小）文件的行序；总分无法解析的样本已在读取时剔除。
    """
    rows: list[_Row] = []
    for sid, first_row in tables[0].rows.items():
        entries: list[tuple[float, _Row]] = []
        for t in tables:
            row = t.rows.get(sid)
            if row is not None and row.total is not None:
                entries.append((row.total, row))
        if not entries:
            raise MedianError(
                f"学号 {sid}-{first_row.std_name}-{first_row.klass} "
                f"在所有文件中都无法解析总分，无法取中位数")
        median = statistics.median_high([total for total, _row in entries])
        # tables 按 n 升序：median_high 返回真实存在的值，同分取 n 最小的文件
        rows.append(next(row for total, row in entries if total == median))
    return rows


# ------------------------------------------------------------ 输出写盘

def _rewrite_formula_row(formula: str, dest_row: int) -> str:
    """把源公式中的行号改写为目标行号（列引用与权重保持不变）。"""
    return re.sub(r"([A-Z]+)(\d+)", rf"\g<1>{dest_row}", formula)


def _save_atomic(wb: Workbook, out: Path) -> None:
    """先写同目录临时文件再原子替换；目标被占用时给出关闭 Excel 的提示。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    ensure_result_writable(out)
    tmp = out.with_name(f".~{out.stem}.{os.getpid()}.tmp{out.suffix}")
    try:
        wb.save(tmp)
        os.replace(tmp, out)
    except OSError as exc:
        hint = (f"本次结果已完整保存到临时文件：{tmp}" if tmp.is_file()
                else "结果文件未被改动")
        raise RuntimeError(
            "结果文件无法写入（可能已被其它程序如 Excel 打开），请关闭后重试；" + hint
        ) from exc


def write_median_result(output: str | Path, headers: list[str], rows: list[_Row]) -> Path:
    """在内存构建完整结果表并一次性写入（总分列保持加权公式约定）。"""
    out = Path(output)
    wb = Workbook()
    ws = wb.active
    ws.title = RESULT_SHEET
    ws.append(headers)
    total_idx = headers.index("总分")
    for dest_row, row in enumerate(rows, start=2):
        values = list(row.values)
        # 源为公式则改写行号后继续写公式（Excel 重算即该次成绩=中位数值），
        # 否则写中位数常量
        values[total_idx] = (_rewrite_formula_row(row.formula, dest_row)
                             if row.formula is not None else row.total)
        for c, value in enumerate(values, start=1):
            ws.cell(row=dest_row, column=c, value=value)
    ws.freeze_panes = "A2"
    for c in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 18
    _save_atomic(wb, out)
    logger.info("median 结果已写入 %s（%d 行）", out, len(rows))
    return out


# ------------------------------------------------------------ 运行日志

def _attach_file_log(log_path: Path) -> logging.Handler | None:
    """把 {主名称}-median.log 挂到根 logger（与控制台双路输出）；失败返回 None。"""
    try:
        handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    except OSError as exc:
        logger.warning("无法写入运行日志 %s: %s（本次仅输出到控制台）", log_path, exc)
        return None
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    return handler


def _detach_file_log(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    handler.close()


# ------------------------------------------------------------ 主流程

def run_median(base: str, directory: str | Path | None = None,
               output: str | Path | None = None) -> Path:
    """median 子命令主流程：匹配 → 读入 → 校验 → 合成 → 整体写入。

    运行信息写入 ``{目录}/{主名称}-median.log``；可预期错误抛 MedianError。
    ``base`` 为主名称（必须参数，带不带 .xlsx 后缀均可）；``directory`` 缺省
    为当前工作目录；``output`` 缺省为 ``{目录}/{主名称}-median.xlsx``。
    """
    started = time.monotonic()
    name = str(base or "").strip()
    if name.lower().endswith(".xlsx"):
        name = name[: -len(".xlsx")]
    if not name:
        exc = MedianError("主名称不能为空")
        logger.error("%s", exc)  # 日志文件尚无法定位，只进控制台
        raise exc
    dir_path = Path(directory) if directory else Path.cwd()
    if not dir_path.is_dir():
        exc = MedianError(f"目录不存在: {dir_path}")
        logger.error("%s", exc)
        raise exc

    matched, ignored = find_inputs(name, dir_path)
    out_path = Path(output) if output else dir_path / f"{name}-median.xlsx"
    log_path = dir_path / f"{name}-median.log"

    module_logger = logging.getLogger(__name__)
    prev_level = module_logger.level
    handler = _attach_file_log(log_path)
    if handler is not None:
        # 保证运行信息一定写入日志文件（不受调用方日志级别影响）
        module_logger.setLevel(logging.INFO)
    try:
        logger.info("median 开始: 主名称=%s 目录=%s 输出=%s", name, dir_path, out_path)
        if not matched:
            raise MedianError(f"未找到匹配 {name}-<n>.xlsx 的输入文件（目录: {dir_path}）")
        logger.info("匹配到 %d 个输入文件:", len(matched))
        for n, path in matched:
            logger.info("  -%d: %s", n, path.name)
        if ignored:
            logger.info("忽略 %d 个不带 -n 的相关文件: %s",
                        len(ignored), ", ".join(ignored))

        # 全部读入内存（以学号唯一定位一行），再做一致性硬校验
        tables = [read_result_table(path) for n, path in matched]
        for table in tables:
            logger.info("读入 %s: %d 名学生", table.path.name, len(table.rows))

        check_student_consistency(tables)
        logger.info("学号一致性校验通过: %d 个文件，%d 名学生",
                    len(tables), len(tables[0].rows))
        headers = check_headers(tables)
        rows = merge_median(tables)
        write_median_result(out_path, headers, rows)
        logger.info("median完成: %d 名学生，耗时 %.2fs，结果已写入 %s",
                    len(rows), time.monotonic() - started, out_path)
        return out_path
    except MedianError as exc:
        logger.error("%s", exc)  # 控制台与 median.log 双路输出
        raise
    except Exception:
        logger.exception("median 处理失败（耗时 %.2fs）", time.monotonic() - started)
        raise
    finally:
        _detach_file_log(handler)
        module_logger.setLevel(prev_level)