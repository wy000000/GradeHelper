"""通用 OpenAI 兼容客户端（不限定本地/llama 实现，llm 与 vllm 端点共用）。"""

from __future__ import annotations

import base64
import json
import logging
import time

import requests

from .config import EndpointConfig

logger = logging.getLogger(__name__)


class LLMClient:
    """调用 OpenAI 兼容的 /chat/completions 接口。"""

    def __init__(self, cfg: EndpointConfig):
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")

    # ------------------------------------------------------------------ 基础调用

    def chat(self, messages: list[dict], **overrides) -> str:
        """同步请求 chat completions，带简单重试；返回 content 文本。"""
        payload = {
            "model": overrides.pop("model", self.cfg.model),
            "messages": messages,
            "temperature": overrides.pop("temperature", self.cfg.temperature),
            "max_tokens": overrides.pop("max_tokens", self.cfg.max_tokens),
            "stream": False,
            **overrides,
        }
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=self.cfg.timeout)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    raise ValueError("LLM 返回空内容")
                return content
            except Exception as exc:  # noqa: BLE001 网络/格式错误统一重试
                last_exc = exc
                if attempt < self.cfg.max_retries:
                    wait = 2 ** attempt
                    logger.warning("LLM 调用失败(%s)，%ds 后重试 (%d/%d)",
                                   exc, wait, attempt + 1, self.cfg.max_retries)
                    time.sleep(wait)
        raise RuntimeError(f"LLM 调用最终失败: {last_exc}")

    def chat_json(self, messages: list[dict], **overrides) -> dict:
        """调用并解析响应中的 JSON 对象（容忍 markdown 代码块/前后缀文本）。"""
        text = self.chat(messages, **overrides)
        return parse_json_object(text)

    # ------------------------------------------------------------------ 多模态

    def describe_image(self, image_bytes: bytes, ext: str, prompt: str) -> str:
        """对单张图片做识别描述（vllm 端点多模态调用）。"""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{ext};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }]
        return self.chat(messages).strip()


def parse_json_object(text: str) -> dict:
    """从 LLM 输出中提取第一个 JSON 对象。"""
    text = text.strip()
    # 去掉 ```json ... ``` 包裹
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"无法从 LLM 输出中解析 JSON: {text[:200]}")