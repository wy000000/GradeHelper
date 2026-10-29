"""Excel 读写：rule 表读取 & result 表写入（总分列使用 Excel 公式）。"""

from __future__ import annotations

import logging
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

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
            method=str(ws.cell(row=row, column=header["处理方法"]).value or "rule").strip(),
            evaluation=str(ws.cell(row=row, column=header["评估方法"]).value or "").strip(),
            comment=str(ws.cell(row=row, column=header["评语"]).value or "").strip(),
        ))
    logger.info("从 %s 读取 %d 个评分项", template_path, len(rules))
    return rules


def write_results(template_path: str | Path, output_path: str | Path,
                  reports: list[ParsedReport], rules: list[RuleItem],
                  scores: dict[str, dict[str, ItemScore]]) -> Path:
    """把评分结果写入（同一文件的）result 表。

    列布局：学号 | 姓名 | 班级 | 总分 | (每项: <key>评分 [<key>置信度 仅LLM] <key>评语)
    总分列使用 Excel 公式 =SUM(各评分列)。scores 结构：学号 -> 评分项 -> ItemScore。
    """
    wb = load_workbook(template_path, data_only=False)
    ws = wb[RESULT_SHEET] if RESULT_SHEET in wb.sheetnames else wb.create_sheet(RESULT_SHEET)
    ws.delete_rows(1, ws.max_row)  # 重建 result 表内容

    # 表头
    headers = ["学号", "姓名", "班级", "总分"]
    score_columns: list[int] = []   # 各评分项所在列号（1-based），供总分公式引用
    col = len(headers) + 1
    for rule in rules:
        score_columns.append(col)
        headers.append(f"{rule.key}评分")
        col += 1
        if rule.is_llm:
            headers.append(f"{rule.key}置信度")
            col += 1
        headers.append(f"{rule.key}评语")
        col += 1
    ws.append(headers)

    # 数据行
    for row_idx, report in enumerate(reports, start=2):
        row_scores = scores.get(report.student_id, {})
        # 总分公式：只汇总各评分项列
        formula = "=SUM(" + ",".join(f"{get_column_letter(c)}{row_idx}" for c in score_columns) + ")"
        cells: list = [report.student_id, report.name, report.klass, formula]
        for rule in rules:
            item = row_scores.get(rule.key)
            cells.append(item.score if item and item.score is not None else None)
            if rule.is_llm:
                cells.append(item.confidence if item and item.confidence is not None else None)
            cells.append(item.comment if item else "")
        ws.append(cells)

    # 冻结首行 + 自适应列宽（近似）
    ws.freeze_panes = "A2"
    for c in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(c)].width = 18

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    logger.info("评分结果已写入 %s（%d 行）", out, len(reports))
    return out
