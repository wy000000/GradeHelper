"""通用文件批处理：多线程执行、失败入队、整批结束后自动重试。

三个阶段（doc / pic / sen）统一使用本模块处理“按文件逐个解析、保存”的流程：

- 每个文件独立执行，单个文件失败不会中断其它文件；
- 失败文件进入异常队列（按文件路径去重）；
- 一轮全部完成后，对异常队列中的文件再重试；
- 每次失败且还会重试时通过 ``on_retry`` 回调记录中间错误；
- 重试结束仍有失败时只记录日志并返回，不影响已经成功的文件。

以上只保证**阶段内**的容错；**阶段之间**是否继续由 ``grader.run`` 决定：
当前阶段完成后若仍有最终失败（或阶段中断），不再执行下一阶段。

``KeyboardInterrupt`` / ``LLMAborted`` 等中止信号不会入队，而是立即向上传播。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from .cancel import LLMAborted
from .progress import ParallelProgress

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# 这些异常表示“用户中止”，不计入异常队列，直接向上传播。
_ABORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    LLMAborted,
    SystemExit,
    GeneratorExit,
)


@dataclass
class FailedFile(Generic[T]):
    """异常队列中的一个文件。"""

    item: T
    path: Path
    error: BaseException
    attempts: int

    @property
    def reason(self) -> str:
        return str(self.error) or self.error.__class__.__name__


class FailureQueue(Generic[T]):
    """线程安全的异常队列：同一路径只保留最后一次失败。"""

    def __init__(self) -> None:
        self._items: dict[str, FailedFile[T]] = {}
        self._lock = threading.Lock()

    def add(self, item: T, path: Path, error: BaseException, attempts: int) -> None:
        with self._lock:
            self._items[str(path)] = FailedFile(item, path, error, attempts)

    def discard(self, path: Path) -> None:
        with self._lock:
            self._items.pop(str(path), None)

    def values(self) -> list[FailedFile[T]]:
        with self._lock:
            return list(self._items.values())

    def drain(self) -> list[FailedFile[T]]:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __bool__(self) -> bool:
        return len(self) > 0


@dataclass
class BatchOutcome(Generic[R]):
    """一次批处理的结果：成功结果按输入顺序返回，失败文件保留在队列中。"""

    results: list[R]
    failures: FailureQueue


def run_file_batch(
    *,
    stage: str,
    items: Sequence[T],
    worker: Callable[[T], R],
    workers: int,
    path_of: Callable[[T], Path],
    label_of: Callable[[T], str] | None = None,
    unit: str = "个文件",
    on_failure: Callable[[FailedFile[T]], None] | None = None,
    on_retry: Callable[[FailedFile[T]], None] | None = None,
    on_success: Callable[[R], None] | None = None,
    max_attempts: int = 2,
    progress_enabled: bool = True,
    progress_stage: str | None = None,
    retry_workers: int | None = None,
) -> BatchOutcome[R]:
    """多线程处理文件，失败入队并在整批结束后自动重试。

    参数：
    - ``stage``：阶段名，用于日志（doc / pic / sen）；
    - ``items``：待处理文件/报告列表；
    - ``worker``：单文件处理函数；抛异常表示该文件失败；
    - ``workers``：并发线程数；
    - ``path_of``：从 item 取文件路径，异常队列用它去重；
    - ``label_of``：进度显示用的简短标签；
    - ``on_failure``：最终仍未成功的文件回调（提供后由调用方记录失败，避免与
     批处理自身的控制台提示重复）；
    - ``on_retry``：某文件本次失败、且后面还会重试时回调，用于记录中间错误
      （提供后由调用方输出该次失败，批处理不再重复提示）；
    - ``on_success``：每个文件成功后立即回调（用于 sen 的实时回写）；
    - ``max_attempts``：总尝试轮数，至少为 1；默认 2 表示失败后再重试一轮；
    - ``progress_stage``：进度条显示的阶段名；默认与 ``stage`` 相同。
      加载 md、LLM 评分等同一阶段内的多个批处理可借此区分，避免进度提示混淆。
    - ``retry_workers``：重试轮（第 2 次及以后尝试）的并发线程数；``None`` 表示
      与首轮相同。``pic`` / ``sen`` 进入 ``retry_rounds`` 时传入 ``batch_size``
      减半后的值：配置值只减半一次，后续重试轮沿用同一值，不再递归减半（下限 1）。

    返回成功结果（保持输入顺序）与最终异常队列。
    """
    total_attempts = max(1, int(max_attempts))
    work = list(items)
    if not work:
        return BatchOutcome(results=[], failures=FailureQueue())

    # 记录原始顺序，重试成功后仍按输入顺序返回结果
    index_by_path = {str(path_of(item)): index for index, item in enumerate(work)}
    results: dict[int, R] = {}
    pending: list[tuple[int, T]] = list(enumerate(work))
    failures: FailureQueue[T] = FailureQueue()

    for attempt in range(1, total_attempts + 1):
        if not pending:
            break
        failures = FailureQueue()
        progress_label = progress_stage or stage
        round_stage = progress_label if attempt == 1 else f"{progress_label}重试"
        # 重试轮中 pending 全是上一轮失败的文档：失败数先按 pending 计入，
        # 同一文档再次失败不重复累计，重试成功后失败数减一。
        # 首轮用 workers；重试轮可用更小的 retry_workers（pic/sen 进入
        # retry_rounds 时传入 batch_size 减半后的值，只减一次、不逐轮再减）
        round_workers = (max(1, int(workers))
                         if attempt == 1 or retry_workers is None
                         else max(1, int(retry_workers)))
        progress = ParallelProgress(
            round_stage, len(pending), unit=unit,
            workers=round_workers, enabled=progress_enabled,
            retry_mode=attempt > 1,
            initial_failed=len(pending) if attempt > 1 else 0,
        )
        with progress:
            pool = ThreadPoolExecutor(max_workers=round_workers)
            try:
                futures = {}
                for _index, item in pending:
                    label = label_of(item) if label_of is not None else str(path_of(item))
                    futures[pool.submit(_run_one, progress, worker, item, label)] = item
                # 用带超时的 wait 替代无超时的 as_completed：
                # Windows 上 condition.wait() 无法被 KeyboardInterrupt 打断，
                # 必须等到某个 future 完成才返回。设置短超时让主线程定期检查中断。
                remaining = dict(futures)
                while remaining:
                    done, _ = wait(remaining, timeout=0.5, return_when=FIRST_COMPLETED)
                    if not done:
                        continue  # 超时未完成，继续轮询（KeyboardInterrupt 在此被处理）
                    for future in done:
                        item = remaining.pop(future)
                        try:
                            result = future.result()
                        except _ABORT_EXCEPTIONS:
                            raise
                        except BaseException as exc:  # noqa: BLE001 单文件失败不影响其它文件
                            path = path_of(item)
                            failed = FailedFile(item, path, exc, attempt)
                            failures.add(item, path, exc, attempt)
                            # 无回调时由批处理自己提示；有 on_retry 时由调用方
                            # 统一记录中间错误（避免控制台重复输出同一条失败）
                            if on_retry is None:
                                logger.warning(
                                    "【阶段 %s】%s 处理失败（第 %d/%d 次）: %s",
                                    stage, _label(item, path_of, label_of), attempt,
                                    total_attempts, str(exc) or exc.__class__.__name__,
                                )
                            # 本次失败且后续还会重试：通知调用方记录中间错误
                            if attempt < total_attempts and on_retry is not None:
                                try:
                                    on_retry(failed)
                                except BaseException:  # noqa: BLE001 记录失败不应影响批处理
                                    logger.error("【阶段 %s】中间失败回调异常", stage,
                                                 exc_info=True)
                            continue
                        results[index_by_path[str(path_of(item))]] = result
                        if on_success is not None:
                            try:
                                on_success(result)
                            except _ABORT_EXCEPTIONS:
                                raise
                            except BaseException:  # noqa: BLE001 回调失败不应中断批处理
                                logger.error("【阶段 %s】成功回调异常", stage, exc_info=True)
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)

        if not failures or attempt >= total_attempts:
            break

        next_workers = (max(1, int(workers)) if retry_workers is None
                        else max(1, int(retry_workers)))
        logger.warning(
            "【阶段 %s】本轮有 %d 个文件失败，开始第 %d/%d 轮重试"
            "（只重试异常队列，并发 %d）",
            stage, len(failures), attempt + 1, total_attempts, next_workers,
        )
        retry_items: list[tuple[int, T]] = []
        for failed in failures.values():
            index = index_by_path.get(str(failed.path))
            if index is not None:
                retry_items.append((index, failed.item))
        pending = retry_items

    if failures:
        logger.error(
            "【阶段 %s】重试结束，仍有 %d 个文件失败（已跳过，其余文件处理完成）",
            stage, len(failures),
        )
        # 有 on_failure 时由调用方逐条记录失败（含文档名与原因），这里不再重复逐条输出
        if on_failure is None:
            for failed in failures.values():
                logger.error("  %s: %s",
                             _label(failed.item, path_of, label_of), failed.reason)
        if on_failure is not None:
            for failed in failures.values():
                try:
                    on_failure(failed)
                except BaseException:  # noqa: BLE001 记录失败不应中断收尾流程
                    logger.error("【阶段 %s】失败回调异常", stage, exc_info=True)

    ordered = [results[index] for index in sorted(results)]
    return BatchOutcome(results=ordered, failures=failures)


def _label(item: T, path_of: Callable[[T], Path],
           label_of: Callable[[T], str] | None) -> str:
    return label_of(item) if label_of is not None else path_of(item).name


def _run_one(progress: ParallelProgress, worker: Callable[[T], R],
             item: T, label: str) -> R:
    """在线程池中执行单个任务，并上报进度。"""
    with progress.task(label):
        return worker(item)