"""并行任务进度显示：pic（图片识别）与 sen（LLM 评分）共用。

特性：
- 线程安全：计数与终端写入分别加锁，工作线程可随时上报；
- 终端：单行 ``\\r`` 原地刷新，按显示宽度截断/补空格（兼容中文宽字符）；
- 非终端（重定向到文件/IDE）：自动降级为周期性普通日志；
- 在飞请求：显示最久 LLM/vllm 请求的已用/静默时长（静默达到
  ``stall_seconds``（如端点 ``read_timeout``）时加 ``!`` 标记）；
- 与 ``logging`` 协调：日志输出前先清掉进度行，避免日志和进度条串行；
- 取消/异常：``close(aborted=True)`` 保证不会留下半行。

用法::

    with ParallelProgress("sen", len(reports), unit="份",
                          workers=cfg.llm.llm_workers) as progress:
        with ThreadPoolExecutor(max_workers=...) as pool:
            futures = [pool.submit(work, report) for report in reports]
            for future in as_completed(futures):
                future.result()

    # 工作线程内部：
    with progress.task(label=report.student_id):
        ...  # 正常返回记为完成，抛异常记为失败
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from typing import Iterator, TextIO

from .cancel import LLMAborted
from .request_track import REQUEST_REGISTRY, current_label, set_current_label

logger = logging.getLogger(__name__)

# 所有控制台写入（进度行 + 日志）共用同一把锁，避免交叉覆盖。
CONSOLE_LOCK = threading.RLock()
# 当前处于活动状态的进度实例；日志输出前会把它们的行清掉。
_ACTIVE: set["ParallelProgress"] = set()

_RENDER_INTERVAL = 0.25   # 终端刷新间隔（秒）
_LOG_INTERVAL = 5.0       # 非终端进度日志间隔（秒）

# 这些异常表示“中止”，不计入完成/失败，只减少进行中计数。
_ABORT_EXCEPTIONS: tuple[type[BaseException], ...] = (KeyboardInterrupt, LLMAborted)


def _display_width(text: str) -> int:
    """按终端显示列宽计算（中日韩全角字符算 2 列）。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _fit_width(text: str, width: int) -> str:
    """把文本截断到不超过 width 个显示列。"""
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > width:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def _fmt_clock(seconds: float) -> str:
    """把秒数格式化为 MM:SS 或 H:MM:SS。"""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _fmt_hms(seconds: float) -> str:
    """把秒数格式化为 小时:分钟:秒（HH:MM:SS）。"""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class ParallelProgress:
    """并行任务进度：同一 stage 的多线程共享一个实例。"""

    def __init__(self, stage: str, total: int, *, unit: str = "项",
                 workers: int | None = None, stream: TextIO | None = None,
                 enabled: bool = True, retry_mode: bool = False,
                 initial_failed: int = 0,
                 stall_seconds: float | None = None):
        """``retry_mode``：重试轮次，本轮任务都是之前已失败的文档。

        此时失败数先按 ``initial_failed`` 计入（即本轮待重试数量），
        同一文档再次失败不重复累计；重试成功后失败数减一。

        ``stall_seconds``：在飞请求静默告警阈值（秒，通常传端点的
        ``read_timeout``）。进度行显示在飞最久请求的已用/静默时长，
        静默达到阈值时加 ``!`` 标记；0/None 表示不标记。
        """
        self.stage = stage
        self.total = max(0, int(total))
        self.unit = unit
        self.workers = workers
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled and self.total > 0)
        self.retry_mode = bool(retry_mode)
        self.stall_seconds = max(0.0, float(stall_seconds or 0))

        self._lock = threading.Lock()
        self._done = 0
        self._failed = max(0, int(initial_failed)) if self.retry_mode else 0
        self._inflight = 0
        self._last_label = ""
        self._start = time.monotonic()
        self._last_log = self._start
        self._line_width = 0
        self._closed = False
        self._started = False

        try:
            self._tty = bool(self.stream.isatty())
        except Exception:  # noqa: BLE001 无法判断时按非终端处理
            self._tty = False

        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> "ParallelProgress":
        if self._started or self._closed:
            return self
        self._started = True
        self._start = time.monotonic()
        self._last_log = self._start
        if not self.enabled:
            return self
        with CONSOLE_LOCK:
            _ACTIVE.add(self)
        if self._tty:
            self._refresh()
        self._ticker = threading.Thread(
            target=self._tick_loop, name=f"progress-{self.stage}", daemon=True)
        self._ticker.start()
        return self

    def close(self, aborted: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        ticker = self._ticker
        if ticker is not None and ticker is not threading.current_thread():
            ticker.join(timeout=1.0)
        with CONSOLE_LOCK:
            _ACTIVE.discard(self)
            if not self.enabled:
                return
            summary = self._summary(aborted)
            if self._tty:
                self._clear_locked()
                try:
                    self.stream.write(summary + "\n")
                    self.stream.flush()
                except (OSError, ValueError):  # 终端已关闭时忽略
                    pass
                return
        # 非终端在锁外走 logging：避免与日志 handler 的锁顺序反转
        logger.info("%s", summary)

    def __enter__(self) -> "ParallelProgress":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close(aborted=exc_type is not None)
        return False

    # ------------------------------------------------------------------ 任务计数

    @contextmanager
    def task(self, label: str = "") -> Iterator[None]:
        """包装一个并行任务：进入时 +进行中，退出时记完成/失败/中止。

        进入时把任务标签登记到线程本地（``request_track``）：同线程内
        发起的 LLM 请求据此关联标签，进度条可显示在飞请求属于哪个任务。
        """
        self._task_started(label)
        previous = current_label()
        if label:
            set_current_label(label)
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 含 KeyboardInterrupt
            aborted = isinstance(exc, _ABORT_EXCEPTIONS)
            self._task_finished(ok=False, aborted=aborted, label=label)
            raise
        else:
            self._task_finished(ok=True, aborted=False, label=label)
        finally:
            set_current_label(previous)

    def _task_started(self, label: str) -> None:
        with self._lock:
            self._inflight += 1
            if label:
                self._last_label = label
        self._refresh()

    def _task_finished(self, *, ok: bool, aborted: bool, label: str) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)
            if not aborted:
                self._done += 1
                if ok:
                    if self.retry_mode and self._failed > 0:
                        # 重试成功：该文档不再算失败
                        self._failed -= 1
                elif not self.retry_mode:
                    # 重试轮中失败项已计入失败数，不重复累计
                    self._failed += 1
            if label:
                self._last_label = label
        self._refresh()

    @property
    def done(self) -> int:
        """已完成（成功 + 失败）数量。"""
        with self._lock:
            return self._done

    @property
    def failed(self) -> int:
        with self._lock:
            return self._failed

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    # ------------------------------------------------------------------ 渲染

    def _snapshot(self) -> tuple[int, int, int, str, float, float]:
        with self._lock:
            done, failed, inflight = self._done, self._failed, self._inflight
            last = self._last_label
        elapsed = max(0.0, time.monotonic() - self._start)
        rate = done / elapsed if elapsed > 0 else 0.0
        return done, failed, inflight, last, elapsed, rate

    def _longest_request(self) -> tuple[str, float, float, bool] | None:
        """在飞最久的 LLM 请求：(标签, 已用秒, 静默秒, 静默是否超阈值)。

        静默 = 距该请求最近一次**有效数据**的时间（由 LLM 客户端在收到
        内容增量时刷新，心跳不算），与端点配置 ``read_timeout`` 同口径；
        无在飞请求时返回 None。
        """
        slot = REQUEST_REGISTRY.longest()
        if slot is None:
            return None
        now = time.monotonic()
        elapsed = max(0.0, now - slot.started)
        silence = max(0.0, now - slot.progress_ts)
        stalled = bool(self.stall_seconds and silence >= self.stall_seconds)
        return slot.label, elapsed, silence, stalled

    def _line_text(self) -> str:
        done, failed, inflight, last, elapsed, rate = self._snapshot()
        pct = done / self.total * 100 if self.total else 100.0
        rate_valid = rate > 0 and elapsed >= 0.5
        parts = [
            f"【{self.stage}】",
            f"{done}/{self.total} {self.unit}",
            f"({pct:3.0f}%)",
            (f"进行中 {inflight}/{self.workers}" if self.workers
             else f"进行中 {inflight}"),
            f"失败 {failed}",
        ]
        if rate_valid:
            parts.append(f"{rate:.1f} {self.unit}/s")
        parts.append(f"已用 {_fmt_clock(elapsed)}")
        remaining = self.total - done
        if rate_valid and remaining > 0:
            parts.append(f"剩余 {_fmt_clock(remaining / rate)}")
        longest = self._longest_request()
        if longest:
            label, req_elapsed, silence, stalled = longest
            silence_text = _fmt_clock(silence)
            if stalled:
                silence_text = f"!{silence_text}"
            parts.append(f"最久 {label} 已用 {_fmt_clock(req_elapsed)}"
                         f" / 静默 {silence_text}")
        if last:
            parts.append(f"最近 {last}")
        return " | ".join(parts)

    def _summary(self, aborted: bool) -> str:
        done, failed, inflight, _last, elapsed, rate = self._snapshot()
        pct = done / self.total * 100 if self.total else 100.0
        if aborted:
            return (
                f"【{self.stage}】已中断：{done}/{self.total} {self.unit} "
                f"({pct:.0f}%) | 进行中 {inflight} | 失败 {failed} | "
                f"耗时 {_fmt_hms(elapsed)}"
            )
        text = (
            f"【{self.stage}】完成 {done}/{self.total} {self.unit} "
            f"({pct:.0f}%) | 失败 {failed} | 耗时 {_fmt_hms(elapsed)}"
        )
        if rate > 0 and elapsed >= 0.5:
            text += f"，平均 {rate:.2f} {self.unit}/s"
        return text

    def _refresh(self) -> None:
        """任务开始/结束时立即刷新（仅终端；非终端交给 ticker 周期输出）。"""
        if not self.enabled or self._closed or not self._tty:
            return
        with CONSOLE_LOCK:
            if self._closed:
                return
            self._render_locked()

    def _tick_loop(self) -> None:
        try:
            while not self._stop.wait(_RENDER_INTERVAL):
                if self._closed:
                    return
                if self._tty:
                    with CONSOLE_LOCK:
                        if self._closed:
                            return
                        self._render_locked()
                else:
                    now = time.monotonic()
                    if now - self._last_log < _LOG_INTERVAL:
                        continue
                    with self._lock:
                        inflight = self._inflight
                    if inflight <= 0:
                        continue
                    self._last_log = now
                    logger.info("%s", self._line_text())
        except Exception:  # noqa: BLE001 后台刷新失败不应影响主流程
            logger.debug("进度刷新线程退出", exc_info=True)

    def _render_locked(self) -> None:
        """写单行进度（调用方必须持有 CONSOLE_LOCK）。"""
        width = max(20, _terminal_width() - 1)
        line = _fit_width(self._line_text(), width)
        pad = max(0, self._line_width - _display_width(line))
        try:
            self.stream.write("\r" + line + " " * pad)
            self.stream.flush()
        except (OSError, ValueError):  # 终端已关闭时忽略
            return
        self._line_width = _display_width(line)

    def _clear_locked(self) -> None:
        """清掉当前进度行（调用方必须持有 CONSOLE_LOCK）。"""
        if not self._tty or self._line_width <= 0:
            return
        try:
            self.stream.write("\r" + " " * self._line_width + "\r")
            self.stream.flush()
        except (OSError, ValueError):  # 终端已关闭时忽略
            pass
        finally:
            self._line_width = 0


def _terminal_width() -> int:
    try:
        return shutil.get_terminal_size((120, 24)).columns
    except Exception:  # noqa: BLE001 无法获取时用默认宽度
        return 120


# ---------------------------------------------------------------------- 日志协调


class _ProgressStreamHandler(logging.StreamHandler):
    """输出日志前先清掉进度行，避免日志与进度条写在同一行。"""

    def emit(self, record: logging.LogRecord) -> None:
        with CONSOLE_LOCK:
            _clear_active_locked()
            super().emit(record)


def _clear_active_locked() -> None:
    for progress in list(_ACTIVE):
        progress._clear_locked()  # noqa: SLF001 同类内部协作


def install_logging_bridge(target: logging.Logger | None = None) -> None:
    """把目标 logger 的终端 handler 换成“先清进度行再输出”的版本。

    在 ``logging.basicConfig`` 之后调用一次即可。只包装输出到
    stdout/stderr 的 ``StreamHandler``，文件 handler 保持原样。
    """
    explicit_target = target is not None
    target = target or logging.getLogger()
    for handler in list(target.handlers):
        if isinstance(handler, _ProgressStreamHandler):
            continue
        if not isinstance(handler, logging.StreamHandler):
            continue
        if not explicit_target and handler.stream not in (sys.stdout, sys.stderr):
            continue
        target.removeHandler(handler)
        bridge = _ProgressStreamHandler(handler.stream)
        bridge.setLevel(handler.level)
        bridge.setFormatter(handler.formatter)
        target.addHandler(bridge)
