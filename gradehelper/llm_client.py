"""通用 OpenAI 兼容客户端（不限定本地/llama 实现，llm 与 vllm 端点共用）。

调用方式固定为流式（``stream=true``），并且直接持有 socket：
从发出请求起就能被取消——关闭 socket 后服务端（llama.cpp、vLLM 等）即停止生成。

为什么不用 ``requests`` / ``http.client`` 的响应对象：
- ``requests.post`` 在收到响应头之前不返回 Response，无法中断排队/预处理阶段；
- ``http.client`` 的缓冲读在 socket 超时一次后会永久失效
  （``cannot read from timed out object``），无法做“短超时轮询 + 可取消”。
因此这里自己发送 HTTP/1.1 请求，用 ``select`` 轮询 socket，并解析
chunked / Content-Length / 连接关闭三种响应体。
"""

from __future__ import annotations

import base64
import json
import logging
import select
import socket
import ssl
import time
from urllib.parse import urlsplit

from .cancel import CancelToken, LLMAborted
from .config import EndpointConfig

logger = logging.getLogger(__name__)

# 撤销检查 / 静默超时检查的轮询间隔（秒）。Windows 上 close/shutdown 无法打断
# 已阻塞的 recv，因此改用短超时轮询，保证 Ctrl+C 后亚秒级退出。
_POLL_SECONDS = 0.25


class LLMTruncatedError(RuntimeError):
    """输出被截断（finish_reason == 'length'），重试无意义。"""


class _StreamResult:
    """一次流式响应的累积结果。"""

    __slots__ = ("content", "reasoning_chars", "finish_reason", "usage", "completion_id")

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning_chars = 0
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.completion_id: str | None = None


class _AbortHandle:
    """取消句柄：先 shutdown 再 close（两者都可能失败，忽略即可）。"""

    __slots__ = ("_sock",)

    def __init__(self, sock: socket.socket):
        self._sock = sock

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # 连接已关闭/未连接
        try:
            self._sock.close()
        except OSError:
            pass


class _SSEBuffer:
    """按 ``\\n`` 切分字节流；跨 TCP 分块的半行会被缓存。"""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        lines: list[bytes] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            lines.append(line)
        return lines

    def flush(self) -> list[bytes]:
        if not self._buf:
            return []
        line, self._buf = self._buf, b""
        return [line]


class LLMClient:
    """调用 OpenAI 兼容的 /chat/completions 接口（流式）。"""

    def __init__(self, cfg: EndpointConfig, cancel: CancelToken | None = None):
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        self.cancel = cancel

    # ------------------------------------------------------------------ 基础调用

    def chat(self, messages: list[dict], **overrides) -> str:
        """请求 chat completions（流式），返回累积的 content 文本。"""
        cfg = self.cfg
        # YAML api 子区域中的所有参数自动透传；overrides 可覆盖或追加
        payload: dict = dict(cfg.api_params)
        payload["messages"] = messages
        payload["stream"] = True
        payload["model"] = overrides.pop("model", cfg.get_api_param("model"))
        payload.update(overrides)
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return self._stream_once(url, payload, headers)
            except (LLMAborted, LLMTruncatedError):
                raise  # 主动取消 / 输出截断：重试无意义
            except Exception as exc:  # noqa: BLE001 网络/格式错误统一重试
                # 连接是被我们自己关掉的：底层可能抛各种异常（OSError 等），
                # 统一视为用户取消，且绝不重试。
                if self.cancel is not None and self.cancel.is_set():
                    raise LLMAborted("调用已被取消") from exc
                last_exc = exc
                if attempt < self.cfg.max_retries:
                    wait = 2 ** attempt
                    logger.warning("LLM 调用失败(%s)，%ds 后重试 (%d/%d)",
                                   exc, wait, attempt + 1, self.cfg.max_retries)
                    time.sleep(wait)
        raise RuntimeError(f"LLM 调用最终失败: {last_exc}")

    # ------------------------------------------------------------------ 流式实现

    def _stream_once(self, url: str, payload: dict, headers: dict) -> str:
        """执行一次流式请求并返回完整 content；失败时抛出异常。"""
        cfg = self.cfg
        if self.cancel is not None and self.cancel.is_set():
            raise LLMAborted("调用已被取消")

        parts = urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        default_port = 443 if parts.scheme == "https" else 80
        port = parts.port or default_port
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = dict(headers)
        headers["Content-Length"] = str(len(body))
        host_header = host if port == default_port else f"{host}:{port}"
        headers.setdefault("Host", host_header)
        headers.setdefault("Connection", "close")

        result = _StreamResult()
        silence = cfg.read_timeout or 0
        sock = self._open_socket(parts.scheme, host, port)
        handle = _AbortHandle(sock)
        try:
            # 关键：连接建立后立刻登记。即使服务端还没发响应头（排队/预处理中），
            # cancel() 也能关闭 socket。
            if self.cancel is not None:
                self.cancel.bind(handle)
            try:
                self._send_request(sock, path, headers, body)
                # 读取阶段用短超时，配合 select 轮询，保证撤销及时生效
                sock.settimeout(_POLL_SECONDS)
                status, reason, resp_headers, buf = self._read_headers(sock, silence)
                if status >= 400:
                    text = self._read_error_body(sock, resp_headers, buf, silence)
                    reason = f" {reason}" if reason else ""
                    raise RuntimeError(
                        f"LLM 接口返回 HTTP {status}{reason}"
                        + (f": {text}" if text else "")
                    )
                self._consume_body(sock, resp_headers, buf, result, silence)
            finally:
                if self.cancel is not None:
                    self.cancel.unbind(handle)
        finally:
            try:
                sock.close()
            except OSError:
                pass

        content = "".join(result.content)
        if result.finish_reason == "length":
            raise LLMTruncatedError(self._truncated_message(result, content))
        if not content.strip():
            raise RuntimeError(self._empty_message(result))
        return content

    # ------------------------------------------------------------------ HTTP 收发

    def _open_socket(self, scheme: str, host: str, port: int) -> socket.socket:
        timeout = self.cfg.connect_timeout or None
        sock = socket.create_connection((host, port), timeout=timeout)
        if scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        return sock

    def _send_request(self, sock: socket.socket, path: str, headers: dict[str, str],
                      body: bytes) -> None:
        # 发送阶段用阻塞 socket（请求体很小，且连接已有超时）
        sock.settimeout(self.cfg.connect_timeout or None)
        lines = [f"POST {path} HTTP/1.1"]
        lines.extend(f"{k}: {v}" for k, v in headers.items())
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
        sock.sendall(head + body)

    def _recv_some(self, sock: socket.socket, silence: int,
                   last: float) -> tuple[bytes, float]:
        """等待并读取一段数据；返回 (data, last)。data 为空表示对端关闭。"""
        while True:
            if self.cancel is not None and self.cancel.is_set():
                raise LLMAborted("调用已被取消")
            # TLS 可能已在内部缓存了解密后的数据，此时 select 不会报可读。
            pending = 0
            if isinstance(sock, ssl.SSLSocket):
                try:
                    pending = sock.pending()
                except OSError:
                    pending = 0
            if pending <= 0:
                try:
                    readable, _, _ = select.select([sock], [], [], _POLL_SECONDS)
                except (OSError, ValueError) as exc:
                    if self.cancel is not None and self.cancel.is_set():
                        raise LLMAborted("调用已被取消") from exc
                    raise
                if not readable:
                    if silence and time.monotonic() - last > silence:
                        raise TimeoutError(f"LLM 超过 {silence}s 未返回数据")
                    continue
            try:
                data = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                if self.cancel is not None and self.cancel.is_set():
                    raise LLMAborted("调用已被取消") from exc
                raise
            if not data:
                return b"", last
            return data, time.monotonic()

    def _read_headers(self, sock: socket.socket, silence: int) -> tuple[int, str, dict, bytes]:
        """读取响应头；返回 (status, reason, headers, 已读到的响应体前缀)。"""
        buf = b""
        last = time.monotonic()
        while b"\r\n\r\n" not in buf:
            data, last = self._recv_some(sock, silence, last)
            if not data:
                raise ConnectionError("LLM 端点在返回响应头之前关闭了连接")
            buf += data
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status_line = lines[0].decode("latin-1", "replace").strip()
        parts = status_line.split(" ", 2)
        try:
            status = int(parts[1])
        except (IndexError, ValueError):
            raise RuntimeError(f"无法解析 LLM 响应状态行: {status_line!r}") from None
        reason = parts[2] if len(parts) > 2 else ""
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if b":" not in line:
                continue
            k, v = line.split(b":", 1)
            headers[k.strip().lower().decode("latin-1", "replace")] = \
                v.strip().decode("latin-1", "replace")
        return status, reason, headers, rest

    def _read_error_body(self, sock: socket.socket, headers: dict[str, str],
                         buf: bytes, silence: int) -> str:
        """尽力读取错误响应体（最多 800 字节）用于报错。"""
        pieces = [buf]
        total = len(buf)
        last = time.monotonic()
        chunked = "chunked" in headers.get("transfer-encoding", "").lower()
        remaining: int | None
        if chunked:
            remaining = None
        elif "content-length" in headers:
            try:
                remaining = int(headers["content-length"]) - total
            except ValueError:
                remaining = None
        else:
            remaining = None
        while total < 800 and (remaining is None or remaining > 0):
            data, last = self._recv_some(sock, silence, last)
            if not data:
                break
            pieces.append(data)
            total += len(data)
            if remaining is not None:
                remaining -= len(data)
        return b"".join(pieces)[:800].decode("utf-8", "replace").strip()

    # ------------------------------------------------------------------ 响应体解码

    def _consume_body(self, sock: socket.socket, headers: dict[str, str], buf: bytes,
                      result: _StreamResult, silence: int) -> None:
        parser = _SSEBuffer()
        transfer = headers.get("transfer-encoding", "").lower()
        if "chunked" in transfer:
            self._consume_chunked(sock, buf, parser, result, silence)
        elif "content-length" in headers:
            try:
                remaining: int | None = int(headers["content-length"])
            except ValueError:
                remaining = None
            self._consume_counted(sock, buf, remaining, parser, result, silence)
        else:
            self._consume_until_close(sock, buf, parser, result, silence)
        for line in parser.flush():
            self._handle_sse_line(line, result)

    def _feed(self, parser: _SSEBuffer, data: bytes, result: _StreamResult) -> bool:
        """把数据喂给 SSE 解析器；返回 True 表示收到 [DONE]。"""
        for line in parser.feed(data):
            if self._handle_sse_line(line, result):
                return True
        return False

    def _consume_until_close(self, sock: socket.socket, buf: bytes, parser: _SSEBuffer,
                             result: _StreamResult, silence: int) -> None:
        if buf and self._feed(parser, buf, result):
            return
        last = time.monotonic()
        while True:
            data, last = self._recv_some(sock, silence, last)
            if not data:
                return
            if self._feed(parser, data, result):
                return

    def _consume_counted(self, sock: socket.socket, buf: bytes, remaining: int | None,
                        parser: _SSEBuffer, result: _StreamResult, silence: int) -> None:
        last = time.monotonic()
        while remaining is None or remaining > 0:
            if not buf:
                data, last = self._recv_some(sock, silence, last)
                if not data:
                    return
                buf = data
            take = len(buf) if remaining is None else min(len(buf), remaining)
            piece, buf = buf[:take], buf[take:]
            if remaining is not None:
                remaining -= take
            if self._feed(parser, piece, result):
                return

    def _consume_chunked(self, sock: socket.socket, buf: bytes, parser: _SSEBuffer,
                         result: _StreamResult, silence: int) -> None:
        last = time.monotonic()
        while True:
            # 读 chunk 大小行
            while b"\r\n" not in buf:
                data, last = self._recv_some(sock, silence, last)
                if not data:
                    return
                buf += data
            line, _, buf = buf.partition(b"\r\n")
            size_token = line.split(b";", 1)[0].strip()
            try:
                size = int(size_token, 16)
            except ValueError:
                raise RuntimeError(f"无法解析 HTTP chunk 长度: {line[:80]!r}") from None
            if size == 0:
                return
            # 读满 size 字节数据 + 尾部 CRLF
            while len(buf) < size + 2:
                data, last = self._recv_some(sock, silence, last)
                if not data:
                    return
                buf += data
            piece = buf[:size]
            buf = buf[size + 2:]
            if self._feed(parser, piece, result):
                return

    def _handle_sse_line(self, raw: bytes, result: _StreamResult) -> bool:
        """处理一行 SSE；返回 True 表示收到 [DONE]，流结束。"""
        if self.cancel is not None and self.cancel.is_set():
            raise LLMAborted("调用已被取消")
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            return False  # 空行 / SSE 注释（llama.cpp 的 ping）
        if line.startswith("data:"):
            data = line[5:].strip()
        elif line.startswith("{"):
            data = line  # 少数后端不按 SSE 伪帧返回
        else:
            return False  # event: / id: 等字段忽略
        if data == "[DONE]":
            return True
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            logger.debug("忽略无法解析的 SSE 数据: %s", line[:200])
            return False
        if not isinstance(obj, dict):
            return False
        if obj.get("error"):
            raise RuntimeError(f"LLM 服务端返回错误: {obj['error']}")
        if obj.get("id") and not result.completion_id:
            result.completion_id = str(obj["id"])
        if obj.get("usage"):
            result.usage = obj["usage"]
        for choice in obj.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                result.content.append(piece)
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                result.reasoning_chars += len(reasoning)
            if choice.get("finish_reason"):
                result.finish_reason = choice["finish_reason"]
        return False

    # ------------------------------------------------------------------ 报错文案

    @staticmethod
    def _truncated_message(result: _StreamResult, content: str) -> str:
        usage = result.usage or {}
        head = content[:120].replace("\n", "\\n")
        tail = content[-120:].replace("\n", "\\n")
        return (
            "LLM 输出被截断（finish_reason=length），无法得到完整结果：\n"
            f"  prompt_tokens={usage.get('prompt_tokens', '?')}, "
            f"completion_tokens={usage.get('completion_tokens', '?')}, "
            f"total_tokens={usage.get('total_tokens', '?')}\n"
            f"  reasoning_content {result.reasoning_chars} 字符，"
            f"content {len(content)} 字符\n"
            f"  content 开头: {head!r}\n"
            f"  content 末尾: {tail!r}\n"
            "可能原因与处理：\n"
            "  1. 思考模式的 reasoning 占满生成预算 → 调大 max_tokens，"
            "改用非思考模型，或降低 parallel 以提高每请求上下文；\n"
            "  2. 单次输出确实超过 max_tokens → 减少单次提交的评分项或缩短报告。"
        )

    @staticmethod
    def _empty_message(result: _StreamResult) -> str:
        return (
            "LLM 返回空内容：\n"
            f"  finish_reason={result.finish_reason!r}, "
            f"reasoning_content {result.reasoning_chars} 字符\n"
            f"  usage={result.usage or {}}"
        )

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
    raise ValueError(
        f"无法从 LLM 输出中解析 JSON（长度={len(text)}）：\n"
        f"  前 200 字符: {text[:200]!r}\n"
        f"  后 200 字符: {text[-200:]!r}\n"
        "提示：若末尾明显被截断，请检查 finish_reason / max_tokens（思考模式可能耗尽生成预算）"
    )
