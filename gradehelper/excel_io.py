"""Excel 读写：rule 表读取 & result 表写入（总分列使用 Excel 公式）。"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from .file_claim import ResultFileClaim
from .models import ItemScore, ParsedReport, RuleItem

logger = logging.getLogger(__name__)

RULE_SHEET = "rule"
RESULT_SHEET = "result"

# rule 表列名 -> 列索引（按表头文字定位，容忍列顺序变化）
RULE_COLUMNS = {"评分项": None, "默认值": None, "权重": None, "处理方法": None,
                "评估方法": None, "评语": None}


def read_rules(template_path: str | Path) -> list[RuleItem]:
    """读取模板 xlsx 的 rule 表，生成评分项列表。"""
    wb = load_workbook(template_path, data_only=False)
    if RULE_SHEET not in wb.sheetnames:
        raise ValueError(f"模板缺少 '{RULE_SHEET}' sheet: {template_path}")
    ws = wb[RULE_SHEET]

    header = {}
    for cell in ws[1]:
        if cell.value in RULE_COLUMNS:
            header[str(cell.value).strip()] = cell.column
    missing = [name for name in RULE_COLUMNS if name not in header]
    if missing:
        raise ValueError(f"rule 表缺少列: {missing}")

    rules: list[RuleItem] = []
    for row in range(2, ws.max_row + 1):
        key = ws.cell(row=row, column=header["评分项"]).value
        if key is None or not str(key).strip():
            continue
        weight = ws.cell(row=row, column=header["权重"]).value
        rules.append(RuleItem(
            key=str(key).strip(),
            default=str(ws.cell(row=row, column=header["默认值"]).value or "").strip(),
            weight=float(weight) if isinstance(weight, (int, float)) else 1.0,
            method=str(ws.cell(row=row, column=header["处理方法"]).value or "code").strip(),
            evaluation=str(ws.cell(row=row, column=header["评估方法"]).value or "").strip(),
            comment=str(ws.cell(row=row, column=header["评语"]).value or "").strip(),
        ))
    logger.info("从 %s 读取 %d 个评分项", template_path, len(rules))
    return rules


def read_result_items(xlsx_path: str | Path,
                      rule_keys: list[str] | None = None) -> dict[str, dict[str, ItemScore]]:
    """读取独立结果文件 result 表中已写入的评分项（分数/置信度/评语）。

    文件不存在或无 result 表时返回空。返回 {学号: {规则key: ItemScore}}。
    用于 sen 阶段在更新 LLM 项时保留 doc 阶段写入的程序化规则分数与评语。
    """
    if not Path(xlsx_path).is_file():
        return {}
    wb = load_workbook(xlsx_path, data_only=True)
    if RESULT_SHEET not in wb.sheetnames:
        return {}
    ws = wb[RESULT_SHEET]

    header = [str(cell.value).strip() if cell.value is not None else "" for cell in ws[1]]
    try:
        sid_col = header.index("学号") + 1
    except ValueError:
        return {}

    # 定位各评分项的三类列：<key>评分 / <key>置信度 / <key>评语
    columns: dict[str, dict[str, int]] = {}
    for idx, h in enumerate(header, start=1):
        for suffix, field in (("评分", "score"), ("置信度", "confidence"), ("评语", "comment")):
            if h.endswith(suffix):
                key = h[:-len(suffix)]
                if rule_keys is None or key in rule_keys:
                    columns.setdefault(key, {})[field] = idx
                break

    result: dict[str, dict[str, ItemScore]] = {}
    for row in range(2, ws.max_row + 1):
        sid = ws.cell(row=row, column=sid_col).value
        if sid is None:
            continue
        sid_str = str(sid).strip()
        row_items: dict[str, ItemScore] = {}
        for key, cols in columns.items():
            score_col = cols.get("score")
            if score_col is None:
                continue
            score = _cell_float(ws.cell(row=row, column=score_col).value)
            confidence = (_cell_float(ws.cell(row=row, column=cols["confidence"]).value)
                          if "confidence" in cols else None)
            comment_val = (ws.cell(row=row, column=cols["comment"]).value
                           if "comment" in cols else None)
            row_items[key] = ItemScore(
                key=key,
                score=score,
                confidence=confidence,
                comment=str(comment_val).strip() if comment_val is not None else "",
            )
        result[sid_str] = row_items
    return result


def _cell_float(value) -> float | None:
    """把单元格值转换为 float；空值或无法转换时返回 None。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_expected_headers(rules: list[RuleItem]) -> list[str]:
    """根据评分规则构建 result 表期望的完整表头。"""
    headers = ["学号", "姓名", "班级", "总分"]
    for rule in rules:
        headers.append(f"{rule.key}评分")
        if rule.is_llm:
            headers.append(f"{rule.key}置信度")
        headers.append(f"{rule.key}评语")
    return headers


def build_results_workbook(source_path: str | Path,
                           reports: list[ParsedReport], rules: list[RuleItem],
                           scores: dict[str, dict[str, ItemScore]],
                           mode: str = "upsert") -> Workbook:
    """基于 source_path 构建写入评分后的工作簿（不落盘）。

    源文件不存在时新建工作簿；存在时加载以保留既有行供 upsert/strict 匹配。
    列布局：学号 | 姓名 | 班级 | 总分 | (每项: <key>评分 [<key>置信度 仅LLM] <key>评语)
    总分列使用 Excel 公式 =分项1*权重1+分项2*权重2+...（加权求和）。scores 结构：学号 -> 评分项 -> ItemScore。

    mode:
      - "upsert":  按学号匹配，存在则更新该行，不存在则追加新行；保留其余旧行
      - "strict":  按学号匹配更新，不存在则抛出 RuntimeError 退出程序；保留其余旧行
    """
    out = Path(source_path)
    if out.is_file():
        wb = load_workbook(out, data_only=False)
        ws = wb[RESULT_SHEET] if RESULT_SHEET in wb.sheetnames else wb.create_sheet(RESULT_SHEET)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = RESULT_SHEET

    # 计算列布局（与 replace 模式一致）
    headers = build_expected_headers(rules)
    score_columns: list[int] = []
    col = 5  # 学号(1) 姓名(2) 班级(3) 总分(4)
    for rule in rules:
        score_columns.append(col)
        col += 1  # 评分列
        if rule.is_llm:
            col += 1  # 置信度列
        col += 1  # 评语列
    last_col = col - 1

    def _write_row(row_idx: int, report: ParsedReport) -> None:
        """向指定行写入/更新一份报告的所有分数列。"""
        row_scores = scores.get(report.student_id, {})
        terms = [f"{get_column_letter(c)}{row_idx}*{rule.weight}"
                 for c, rule in zip(score_columns, rules)]
        formula = "=" + "+".join(terms)
        ws.cell(row=row_idx, column=1, value=report.student_id)
        ws.cell(row=row_idx, column=2, value=report.std_name)
        ws.cell(row=row_idx, column=3, value=report.klass)
        ws.cell(row=row_idx, column=4, value=formula)
        for i, rule in enumerate(rules):
            score_col = score_columns[i]
            item = row_scores.get(rule.key)
            ws.cell(row=row_idx, column=score_col,
                    value=item.score if item and item.score is not None else None)
            if rule.is_llm:
                conf_col = score_col + 1
                ws.cell(row=row_idx, column=conf_col,
                        value=item.confidence if item and item.confidence is not None else None)
                comment_col = conf_col + 1
            else:
                comment_col = score_col + 1
            ws.cell(row=row_idx, column=comment_col,
                    value=item.comment if item else "")

    if mode in ("upsert", "strict"):
        # 读取现有表头，建立 学号 -> 行号 映射
        existing_header = [cell.value for cell in ws[1]] if ws.max_row >= 1 else []
        if existing_header != headers:
            # 表头缺失或不匹配，重建表头（相当于全部插入）
            ws.delete_rows(1, ws.max_row)
            ws.append(headers)
            sid_to_row: dict[str, int] = {}
            next_row = 2
        else:
            sid_col = 1
            sid_to_row = {}
            for row in range(2, ws.max_row + 1):
                sid = ws.cell(row=row, column=sid_col).value
                if sid is not None:
                    sid_to_row[str(sid).strip()] = row
            next_row = ws.max_row + 1

        for report in reports:
            sid = report.student_id
            if sid in sid_to_row:
                row_idx = sid_to_row[sid]
            else:
                if mode == "strict":
                    raise RuntimeError(
                        f"sen 阶段：学号 '{sid}' 在 result 表中不存在，无法更新，程序退出"
                    )
                row_idx = next_row
                next_row += 1
                # 如果当前只有表头没有数据，先确保新行能写入
                if row_idx == 2 and ws.max_row == 1:
                    pass
            _write_row(row_idx, report)

    else:
        raise ValueError(f"未知的 write_results 模式: {mode}")

    # 冻结首行 + 自适应列宽（近似）
    ws.freeze_panes = "A2"
    for c in range(1, last_col + 1):
        ws.column_dimensions[get_column_letter(c)].width = 18
    return wb


def write_results(output_path: str | Path,
                  reports: list[ParsedReport], rules: list[RuleItem],
                  scores: dict[str, dict[str, ItemScore]],
                  mode: str = "upsert") -> Path:
    """把评分结果就地写入独立结果文件的 result 表（不修改评分模板文件）。"""
    out = Path(output_path)
    wb = build_results_workbook(out, reports, rules, scores, mode)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    logger.info("评分结果已写入 %s（%d 行，模式=%s）", out, len(reports), mode)
    return out


def write_results_atomic(output_path: str | Path,
                         reports: list[ParsedReport], rules: list[RuleItem],
                         scores: dict[str, dict[str, ItemScore]],
                         mode: str = "upsert",
                         claim: ResultFileClaim | None = None,
                         retries: int = 6, retry_delay: float = 1.0) -> Path:
    """写结果表：先写同目录临时文件再原子替换，目标被占用时重试且不损坏原文件。

    与 write_results 的区别只在落盘方式：内容先完整写到
    `.<结果文件名>.<pid>.tmp.xlsx`，再 os.replace() 覆盖目标文件。
    Windows 上目标文件被 Excel 等程序打开时替换会被拒绝，因此每次尝试前临时释放
    claim、替换后重新占用（claim 见 file_claim.ResultFileClaim，用于 doc/sen 阶段
    运行期间独占结果文件）。重试用尽仍失败时：原结果文件保持不变，新结果完整
    保留在临时文件中，异常信息会给出该路径，关闭 Excel 后可改名取用或重新运行。
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".~{out.stem}.{os.getpid()}.tmp{out.suffix}")
    total = max(1, retries)
    last_exc: OSError | None = None
    for attempt in range(1, total + 1):
        released = False
        try:
            # 先完整构建并写临时文件（不与任何程序冲突），再替换目标文件
            wb = build_results_workbook(out, reports, rules, scores, mode)
            wb.save(tmp)
            if claim is not None and claim.held:
                claim.release()  # 替换目标文件需要独占，Windows 上被占用会拒绝
                released = True
            os.replace(tmp, out)
            last_exc = None
            break
        except OSError as exc:
            last_exc = exc
            logger.warning("结果文件被占用，第 %d/%d 次写入失败: %s", attempt, total, exc)
            if attempt < total:
                time.sleep(max(0.0, retry_delay))
        finally:
            if released:
                claim.acquire(raise_on_error=False)  # 尽力恢复独占占用
    if last_exc is not None:
        hint = (f"本次结果已完整保存到临时文件：{tmp}" if tmp.is_file()
                else "结果文件未被改动")
        raise RuntimeError(
            "结果文件无法写入（可能已被其它程序如 Excel 打开），请关闭后重试；" + hint
        ) from last_exc
    logger.info("评分结果已写入 %s（%d 行，模式=%s）", out, len(reports), mode)
    return out