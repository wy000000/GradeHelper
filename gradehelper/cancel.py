"""跨线程取消：主动断开在飞的 LLM 流式连接，让服务端停止生成。

llama.cpp / vLLM 等 OpenAI 兼容服务没有“取消生成”接口，唯一的机制是客户端
关闭 HTTP 连接：服务端检测到连接断开后释放 slot 并停止解码。因此这里把在飞的
连接句柄登记起来，取消时由任意线程统一 close()。
"""

from __future__ import annotations

import threading


class LLMAborted(Exception):
    """用户主动取消（Ctrl+C 等）；调用方不应重试。"""


def _close_quietly(handle: object) -> None:
    try:
        handle.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 关闭连接失败不影响取消语义
        pass


class CancelToken:
    """取消令牌：置位后所有已登记/新登记的连接都会被关闭。

    - ``bind()``   在连接建立后登记（若已取消则立刻关闭并抛 LLMAborted）
    - ``unbind()`` 在读取结束后注销
    - ``cancel()`` 置位并关闭全部在飞连接（幂等，可跨线程调用）

    登记对象只需实现 ``close()``（``http.client.HTTPConnection`` 等）。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._handles: set[object] = set()

    def is_set(self) -> bool:
        """是否已请求取消。"""
        return self._event.is_set()

    def bind(self, handle: object) -> None:
        """登记一个在飞连接；若此时已取消，立即关闭连接并抛出 LLMAborted。"""
        with self._lock:
            aborted = self._event.is_set()
            if not aborted:
                self._handles.add(handle)
        if aborted:
            _close_quietly(handle)
            raise LLMAborted("调用已被取消")

    def unbind(self, handle: object) -> None:
        with self._lock:
            self._handles.discard(handle)

    def cancel(self) -> None:
        """置位并关闭所有在飞连接；重复调用无副作用。"""
        with self._lock:
            self._event.set()
            active = list(self._handles)
            self._handles.clear()
        for handle in active:
            _close_quietly(handle)
