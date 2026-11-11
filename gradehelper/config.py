"""配置加载：config.yaml（端点、路径、Prompt 外置配置）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
MINERU_TIER_CHOICES = {"flash", "basic", "standard", "advanced"}
# 默认处理的文档后缀（可在 config.yaml 的 paths.document_suffixes 中覆盖）
DEFAULT_DOCUMENT_SUFFIXES = (".docx", ".doc", ".pdf")


def _normalize_document_suffixes(value: Any) -> list[str]:
    """把 YAML 中的后缀配置规范化为小写、带前导点的列表。

    兼容 ``[".docx", ".pdf"]``、``"docx, doc, pdf"`` 与 ``"docx"`` 三种写法；
    去重并保持顺序。空值或全部非法时回退到默认 .docx/.doc/.pdf。
    """
    if isinstance(value, str):
        items = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        items = []
    suffixes: list[str] = []
    for item in items:
        suffix = item.strip().lower()
        if not suffix:
            continue
        if not suffix.startswith("."):
            suffix = "." + suffix
        if suffix not in suffixes:
            suffixes.append(suffix)
    return suffixes or list(DEFAULT_DOCUMENT_SUFFIXES)





@dataclass
class EndpointConfig:
    """OpenAI 兼容端点配置（llm 与 vllm 均使用此结构，全部来自 YAML）。"""

    # ---- 客户端专用（来自 YAML 的 connection/client 子区域，不会发送给 API）----
    base_url: str = ""
    api_key: str = ""
    model: str = ""               # 模型名（也兼容旧配置的 api.model）
    connect_timeout: int = 0      # TCP/TLS 连接超时（秒）
    read_timeout: int = 0         # 等待响应数据的超时（秒）
    max_retries: int = 0
    llm_workers: int = 0   # sen 阶段 LLM 评分并发线程数；进入 retry_rounds 重试轮后按此值减半（只减一次，下限 1）
    vllm_workers: int = 0  # pic 阶段图片识别并发线程数；进入 retry_rounds 重试轮后按此值减半（只减一次，下限 1）

    # ---- API 参数（来自 YAML 的 api 子区域，全部透传给 /chat/completions）----
    # api 子区域中出现的所有键值对原样存入此字典；新增任意键自动生效，无需改代码
    api_params: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EndpointConfig":
        cfg = cls()
        data = data or {}
        # connection 子区域：连接与模型参数（推荐写法）
        # client 子区域：兼容旧配置，同样解析为 dataclass 字段
        for section in ("connection", "client"):
            for key, value in (data.get(section) or {}).items():
                if key.startswith("_"):
                    continue
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        # api 子区域：全部原样存入 api_params，自动透传
        api_data = data.get("api") or {}
        for key, value in api_data.items():
            cfg.api_params[key] = value
        return cfg

    def get_api_param(self, name: str, default: Any = None) -> Any:
        """读取某个 API 参数的值。"""
        return self.api_params.get(name, default)

    def get_model(self) -> Any:
        """模型名：优先 connection.model，兼容旧配置的 api.model。"""
        return self.model or self.get_api_param("model")


@dataclass
class PathsConfig:
    """路径配置（reports_dir 与 template 仅由启动参数提供，不在 YAML 中配置）。"""

    output: str = ""
    captions_dir: str = ""
    log_path: str = ""  # 运行日志文件路径；留空表示按 log_name_format 生成
    log_name_format: str = "{course}-{tagDep}-{reportName}.log"  # 日志文件名格式
    log_max_size_kb: int = 32       # 日志超过该大小（KB）才清理旧记录；0 表示不清理
    log_retention_days: float = 1.0  # 日志记录保留天数；0 表示不清理
    # 单文件处理并发数（doc 解析/落盘，以及 pic/sen 加载 md）
    parse_workers: int = 4
    # 单文件失败后的额外重试轮数；0 表示不重试
    retry_rounds: int = 1
    # 需要处理的报告文档后缀（默认 .docx / .doc / .pdf；可写成 YAML 列表或逗号分隔字符串）
    document_suffixes: list[str] = field(default_factory=lambda: list(DEFAULT_DOCUMENT_SUFFIXES))
    # MinerU 解析档位（仅 PDF/图片生效；办公文档固定使用 MinerU 的 flash 原生解析）
    mineru_tier: str = "standard"
    # 报告文件命名格式（用于解析文件名提取课程/班级/学号/姓名/报告名）
    report_name_format: str = ""
    report_name_separator: str = ""
    # 结果文件命名模式（与模板同目录；占位符 {course}/{tagDep}/{reportName}）
    result_name_format: str = ""

    def __post_init__(self) -> None:
        self.document_suffixes = _normalize_document_suffixes(self.document_suffixes)
        tier = str(self.mineru_tier or "").strip().lower()
        self.mineru_tier = tier if tier in MINERU_TIER_CHOICES else "standard"


@dataclass
class PromptsConfig:
    llm_batch: str = ""
    image_context: str = ""
    image_prompt: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PromptsConfig":
        cfg = cls()
        for key, value in (data or {}).items():
            if hasattr(cfg, key) and value:
                setattr(cfg, key, str(value).strip() + "\n")
        return cfg


@dataclass
class AppConfig:
    llm: EndpointConfig = field(default_factory=EndpointConfig)
    vllm: EndpointConfig = field(default_factory=EndpointConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载 YAML 配置；文件缺失时返回默认配置。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    endpoints = data.get("endpoints") or {}
    return AppConfig(
        llm=EndpointConfig.from_dict(endpoints.get("llm")),
        vllm=EndpointConfig.from_dict(endpoints.get("vllm")),
        paths=PathsConfig(**{k: v for k, v in (data.get("paths") or {}).items()
                             if k in PathsConfig.__dataclass_fields__}),
        prompts=PromptsConfig.from_dict(data.get("prompts")),
    )