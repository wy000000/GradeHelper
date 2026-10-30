"""配置加载：config.yaml（端点、路径、Prompt 外置配置）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

DEFAULT_LLM_ITEM_PROMPT = (
    "你是一份课程报告的评审助教。请严格按照给定的评分标准，对下述评分项进行评分。\n\n"
    "评分项编号：{key}\n评分标准：{evaluation}\n分数范围：{score_range}\n\n"
    "报告内容（Markdown，其中图片位置已嵌入由多模态模型生成的图片描述）：\n---\n{report}\n---\n\n"
    "要求：\n1. 评语用中文，简要说明给分理由。\n"
    "2. 如果评分标准中规定了超出上限的额外加分情形，按标准执行。\n"
    "3. confidence 表示你对本次评分的置信度，取值 0~1。\n"
    '4. 只输出一个 JSON 对象：{{"score": <数字>, "confidence": <0~1的数字>, "comment": "<评语>"}}'
)


@dataclass
class EndpointConfig:
    """OpenAI 兼容端点配置（llm 与 vllm 均使用此结构，默认值可相同）。"""

    # ---- 客户端专用（来自 YAML 的 client 子区域，不会发送给 API）----
    base_url: str = "http://localhost:8080/"
    api_key: str = ""
    connect_timeout: int = 5      # TCP/TLS 连接超时（秒）
    read_timeout: int = 600       # 等待响应数据的超时（秒）
    max_retries: int = 2
    batch_size: int = 8

    # ---- API 参数（来自 YAML 的 api 子区域，全部透传给 /chat/completions）----
    # api 子区域中出现的所有键值对原样存入此字典；新增任意键自动生效，无需改代码
    api_params: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EndpointConfig":
        cfg = cls()
        data = data or {}
        # client 子区域：解析为 dataclass 字段
        client_data = data.get("client") or {}
        for key, value in client_data.items():
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


@dataclass
class PathsConfig:
    reports_dir: str = "./reports"
    template: str = "云安全-课程报告-评分模板.xlsx"
    output: str = ""
    captions_dir: str = ""
    save_parsed: bool = False


@dataclass
class PromptsConfig:
    llm_item: str = DEFAULT_LLM_ITEM_PROMPT
    image_context: str = "[图片描述 P{n}: {caption}]"

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