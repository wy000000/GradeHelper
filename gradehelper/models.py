"""核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RuleItem:
    """评分模板 rule 表中的一行。"""

    key: str                      # 评分项
    default: str                  # 默认值（原始字符串，如 "25"、"0-15"）
    weight: float                 # 权重
    method: str                   # 处理方法：'rule'（程序化）或 'LLM'
    evaluation: str               # 评估方法描述
    comment: str                  # 评语模板

    @property
    def is_llm(self) -> bool:
        return self.method.strip().upper() == "LLM"

    def default_numeric(self) -> Optional[float]:
        """尽力把默认值解析为数值；失败返回 None。"""
        try:
            return float(str(self.default).strip())
        except (TypeError, ValueError):
            return None

    def score_range(self) -> tuple[Optional[float], Optional[float]]:
        """解析 '0-15' 形式的取值范围；纯数值视为 [v, v]；无法解析返回 (None, None)。"""
        text = str(self.default).strip()
        # 找到第一个不在开头的 '-' 作为分隔符（兼容负数默认值，如 "-2"）
        sep = text.find("-", 1) if len(text) > 1 else -1
        if sep > 0:
            try:
                return float(text[:sep]), float(text[sep + 1:])
            except ValueError:
                pass
        value = self.default_numeric()
        if value is None:
            return None, None
        return value, value


@dataclass
class ReportImage:
    """报告中的一张图片（驻留内存）。"""

    index: int                    # n，从 1 递增
    ext: str                      # 图片后缀，如 'png'
    data: bytes                   # 图片二进制内容
    caption: str = ""             # vllm 识别出的描述

    @property
    def stem(self) -> str:
        return f"-P{self.index}"

    def txt_name(self, report: "ParsedReport") -> str:
        return f"{report.base_name}-P{self.index}.txt"


@dataclass
class ParsedReport:
    """一份解析完成的报告（md 与图片均驻留内存）。"""

    course: str                   # 课程名称
    klass: str                    # 专业班级
    student_id: str               # 学号
    name: str                     # 姓名
    source_path: str              # 原 docx 路径
    md_text: str                  # Markdown 正文（图片处为占位符 [[PIC:Pn]]）
    images: list[ReportImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def base_name(self) -> str:
        return f"{self.course}-{self.klass}-{self.student_id}-{self.name}"


@dataclass
class ItemScore:
    """单个评分项对单份报告的评分结果。"""

    key: str
    score: Optional[float] = None
    confidence: Optional[float] = None   # 仅 LLM 项有效
    comment: str = ""


@dataclass
class RuleOutcome:
    """程序化规则一次批处理的结果：学号 -> 评分。"""

    scores: dict[str, ItemScore] = field(default_factory=dict)
