"""在飞 LLM 请求登记表：供进度条显示当前请求的已用时与静默时长。

背景：pic / sen 阶段偶发"一直等待"——僵死请求没有有效数据但连接未断，
此前只能靠 Ctrl+C 兜底。把每个在飞请求登记到这里，进度条即可实时显示
最久在飞请求「已用多久 / 距上次有效数据多久（静默）」，静默超过
``read_timeout`` 时加 ``!`` 标记，便于在前台直接判断是否即将超时。

- :meth:`RequestRegistry.register`  请求发出时登记（标签取当前进度任务，
  总时长上限 ``request_timeout`` 取自端点 yaml 配置）；
- :meth:`RequestRegistry.touch`     收到**有效数据**时刷新静默基准
  （心跳注释行 / 空行不算有效数据，调用方不得对其 touch）；
- :meth:`RequestRegistry.unregister` 请求结束（成功/失败/取消）时注销；
- :meth:`RequestRegistry.longest`   取在飞最久的请求，供进度条渲染。

时间统一使用 ``time.monotonic()``。请求标签由 ``ParallelProgress.task``
通过 :func:`set_current_label` / :func:`current_label`（线程本地）传递：
工作线程进入任务时设置标签，同线程内发起的 LLM 调用登记时即可取到；
无任务上下文（如单测直连）时回退为「请求」。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class RequestSlot:
    """一个在飞请求的登记信息（时间均为 ``time.monotonic()`` 秒）。

    ``request_timeout`` 为该请求的总时长上限（秒，来自端点 yaml 配置，
    经 ``LLMClient`` 传入）；0 表示不限，超时判定在 ``_recv_some`` 内完成。
    """

    label: str
    started: float           # 请求开始时间（登记时刻）
    progress_ts: float       # 最近一次有效数据到达时间；初始=开始时间
    request_timeout: float = 0.0


class RequestRegistry:
    """线程安全的在飞请求登记表（进程内单例 :data:`REQUEST_REGISTRY`）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: list[RequestSlot] = []

    def register(self, label: str = "",
                 request_timeout: float = 0.0) -> RequestSlot:
        """登记一个新请求；返回句柄供 touch / unregister 使用。"""
        now = time.monotonic()
        slot = RequestSlot(label=label or "请求", started=now, progress_ts=now,
                           request_timeout=max(0.0, float(request_timeout or 0)))
        with self._lock:
            self._slots.append(slot)
        return slot

    def touch(self, slot: RequestSlot) -> None:
        """收到有效数据：刷新该请求的静默基准。"""
        slot.progress_ts = time.monotonic()

    def unregister(self, slot: RequestSlot) -> None:
        """注销请求（未登记过时忽略）。"""
        with self._lock:
            try:
                self._slots.remove(slot)
            except ValueError:
                pass

    def longest(self) -> RequestSlot | None:
        """在飞最久的请求（开始时间最早）；无在飞请求时返回 None。"""
        with self._lock:
            if not self._slots:
                return None
            return min(self._slots, key=lambda slot: slot.started)


REQUEST_REGISTRY = RequestRegistry()

# 当前工作线程正在处理的任务标签（由 ParallelProgress.task 设置/恢复）
_LABEL_STATE = threading.local()


def current_label() -> str:
    """当前线程正在处理的任务标签；无任务上下文时为空串。"""
    return getattr(_LABEL_STATE, "label", "") or ""


def set_current_label(label: str) -> None:
    """设置当前线程的任务标签（ParallelProgress.task 进入/退出时调用）。"""
    _LABEL_STATE.label = label
