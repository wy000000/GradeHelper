"""并行进度显示测试：计数、TTY 单行刷新、非 TTY 降级、日志清行。"""

from __future__ import annotations

import io
import logging
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.progress import (  # noqa: E402
    ParallelProgress, install_logging_bridge,
)
from gradehelper.request_track import (  # noqa: E402
    REQUEST_REGISTRY, current_label, set_current_label,
)


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestParallelProgress(unittest.TestCase):
    def test_tty_single_line_and_summary(self):
        stream = _FakeTTY()
        progress = ParallelProgress("pic", 2, unit="张", stream=stream)
        with progress:
            with progress.task("2021001-P1"):
                pass
            with progress.task("2021002-P1"):
                pass
        out = stream.getvalue()
        self.assertIn("\r", out)                  # 原地刷新
        self.assertIn("【pic】", out)
        self.assertIn("完成 2/2 张", out)          # 最终汇总
        self.assertTrue(out.endswith("\n"))       # 收尾换行

    def test_non_tty_uses_logging(self):
        with self.assertLogs("gradehelper.progress", level="INFO") as logs:
            progress = ParallelProgress("sen", 2, unit="份", stream=io.StringIO())
            with progress:
                with progress.task("2021001"):
                    pass
                with progress.task("2021002"):
                    pass
        text = "\n".join(logs.output)
        self.assertIn("【sen】完成 2/2 份", text)
        self.assertNotIn("\r", text)

    def test_zero_total_is_silent(self):
        stream = _FakeTTY()
        with ParallelProgress("pic", 0, stream=stream):
            pass
        self.assertEqual(stream.getvalue(), "")

    def test_thread_safe_counts(self):
        progress = ParallelProgress("sen", 64, unit="份", stream=io.StringIO())

        def work(i: int) -> None:
            with progress.task(f"id{i}"):
                pass

        with progress:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(work, range(64)))
        self.assertEqual(progress.done, 64)
        self.assertEqual(progress.failed, 0)
        self.assertEqual(progress.inflight, 0)

    def test_retry_mode_dedup_and_decrement(self):
        """重试轮：失败数按待重试数量起始，重复失败不累计，成功则减一。"""
        progress = ParallelProgress("sen重试", 3, unit="份", stream=io.StringIO(),
                                    retry_mode=True, initial_failed=3)
        with progress:
            with progress.task("ok1"):
                pass  # 重试成功 → 失败数 3 → 2
            try:
                with progress.task("bad"):
                    raise RuntimeError("仍然失败")  # 已在失败数中，不重复累计
            except RuntimeError:
                pass
            with progress.task("ok2"):
                pass  # 重试成功 → 失败数 2 → 1
        self.assertEqual(progress.done, 3)
        self.assertEqual(progress.failed, 1)
        self.assertEqual(progress.inflight, 0)

    def test_failure_counted_as_done(self):
        progress = ParallelProgress("sen", 3, unit="份", stream=io.StringIO())
        with progress:
            with progress.task("ok1"):
                pass
            try:
                with progress.task("bad"):
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            with progress.task("ok2"):
                pass
        self.assertEqual(progress.done, 3)
        self.assertEqual(progress.failed, 1)
        self.assertEqual(progress.inflight, 0)

    def test_abort_not_counted_as_failure(self):
        progress = ParallelProgress("sen", 2, unit="份", stream=io.StringIO())
        with progress:
            with self.assertRaises(KeyboardInterrupt):
                with progress.task("x"):
                    raise KeyboardInterrupt
        self.assertEqual(progress.done, 0)
        self.assertEqual(progress.failed, 0)
        self.assertEqual(progress.inflight, 0)

    def test_logging_bridge_clears_progress_line(self):
        stream = _FakeTTY()
        progress = ParallelProgress("sen", 2, unit="份", stream=stream)
        log = logging.getLogger("test.progress.bridge")
        old_handlers = log.handlers[:]
        old_propagate = log.propagate
        old_level = log.level
        try:
            log.handlers.clear()
            log.propagate = False
            log.setLevel(logging.INFO)
            handler = logging.StreamHandler(stream)
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
            install_logging_bridge(log)

            with progress:
                with progress.task("a"):
                    log.info("hello")
        finally:
            log.handlers.clear()
            log.handlers.extend(old_handlers)
            log.propagate = old_propagate
            log.setLevel(old_level)

        out = stream.getvalue()
        self.assertIn("hello\n", out)
        self.assertIn("\r", out)  # 日志前清过进度行

    # ------------------------------------------------------------------ 在飞请求显示

    def test_inflight_request_display(self):
        """进度行显示在飞最久请求的已用/静默时长，静默超阈值加 ``!``。"""
        slot = REQUEST_REGISTRY.register("2021002-P3")
        self.addCleanup(REQUEST_REGISTRY.unregister, slot)
        slot.started = time.monotonic() - 65.0        # 已用 01:05
        slot.progress_ts = time.monotonic() - 310.0   # 静默 05:10 > 300s 阈值

        progress = ParallelProgress("pic", 4, unit="张", stream=io.StringIO(),
                                    stall_seconds=300)
        line = progress._line_text()
        self.assertIn("最久已用 01:05 / 静默 !05:10 | 2021002-P3", line)

    def test_inflight_request_not_stalled_has_no_marker(self):
        """静默未超阈值时不加 ``!`` 标记。"""
        slot = REQUEST_REGISTRY.register("2021003-P1")
        self.addCleanup(REQUEST_REGISTRY.unregister, slot)
        slot.started = time.monotonic() - 10.0
        slot.progress_ts = time.monotonic() - 2.0

        progress = ParallelProgress("pic", 4, unit="张", stream=io.StringIO(),
                                    stall_seconds=300)
        line = progress._line_text()
        self.assertIn("最久已用 00:10 / 静默 00:02 | 2021003-P1", line)
        self.assertNotIn("!", line)

    def test_no_inflight_request_no_display(self):
        """无在飞请求（如加载 md 阶段）时不显示请求用时段。"""
        progress = ParallelProgress("sen", 2, unit="份", stream=io.StringIO(),
                                    stall_seconds=300)
        line = progress._line_text()
        self.assertNotIn("最久", line)

    def test_stall_disabled_when_no_threshold(self):
        """未传 stall_seconds 时显示请求用时但不加标记。"""
        slot = REQUEST_REGISTRY.register("2021004-P2")
        self.addCleanup(REQUEST_REGISTRY.unregister, slot)
        slot.started = time.monotonic() - 400.0
        slot.progress_ts = time.monotonic() - 400.0

        progress = ParallelProgress("pic", 4, unit="张", stream=io.StringIO())
        line = progress._line_text()
        self.assertIn("最久已用 06:40 / 静默 06:40 | 2021004-P2", line)
        self.assertNotIn("!", line)

    def test_tty_render_full_line_and_erase_block(self):
        """TTY 渲染输出完整行（不截断，换行交给终端），并擦除旧行块不累积新行。"""
        from unittest import mock
        stream = _FakeTTY()
        label = ("云安全-信息安全2301-3220802028-胡崎-"
                 "实验报告：docker的安装配置与操作.md")
        slot = REQUEST_REGISTRY.register(label)
        self.addCleanup(REQUEST_REGISTRY.unregister, slot)
        slot.started = time.monotonic() - 400.0
        slot.progress_ts = time.monotonic() - 400.0

        with mock.patch("gradehelper.progress._enable_ansi_stream",
                        return_value=True):
            progress = ParallelProgress("sen", 2, unit="份", stream=stream,
                                        stall_seconds=120)
            with progress:
                with progress.task(label):
                    pass
        out = stream.getvalue()
        # 完整行写入（未被截断到终端宽度）
        self.assertIn("最久已用 06:40 / 静默 !06:40", out)
        self.assertIn(label, out)
        # 渲染前擦除旧行块（不追加新行）
        self.assertIn("\x1b[J", out)

    def test_elapsed_shows_used_and_remaining(self):
        """阶段耗时显示「用时 已用/剩余」，完成速率显示为 s/份。"""
        progress = ParallelProgress("sen", 4, unit="份", stream=io.StringIO())
        # 无速率样本时：只显示已用，速率 0.0 s/份，不显示剩余
        line = progress._line_text()
        self.assertIn("用时 00:00", line)
        self.assertIn("0.0 s/份", line)
        used_seg = line.split("用时 00:00", 1)[1].split(" | ", 1)[0]
        self.assertNotIn("/", used_seg)

        # 模拟已运行 2 秒、完成 1 份：rate=0.5 份/s → 2.0 s/份，剩余 3/0.5=6 秒
        progress._start = time.monotonic() - 2.0
        progress._done = 1
        line = progress._line_text()
        self.assertIn("用时 00:02/00:06", line)
        self.assertIn("2.0 s/份", line)

    def test_long_label_keeps_times_visible(self):
        """长文件名标签不挤掉时长：数值段前置于标签，渲染不截断。"""
        label = ("云安全-信息安全2301-3220802028-胡崎-"
                 "实验报告：docker的安装配置与操作.md")
        slot = REQUEST_REGISTRY.register(label)
        self.addCleanup(REQUEST_REGISTRY.unregister, slot)
        slot.started = time.monotonic() - 400.0
        slot.progress_ts = time.monotonic() - 400.0

        progress = ParallelProgress("sen", 2, unit="份", stream=io.StringIO(),
                                    stall_seconds=120)
        line = progress._line_text()
        self.assertIn("最久已用 06:40 / 静默 !06:40", line)
        self.assertIn(label, line)

    def test_task_sets_and_restores_thread_label(self):
        """任务进入时登记线程本地标签，退出（含异常）后恢复原值。"""
        set_current_label("")
        progress = ParallelProgress("sen", 1, unit="份", stream=io.StringIO())
        with progress:
            with progress.task("2021001.md"):
                self.assertEqual(current_label(), "2021001.md")
            self.assertEqual(current_label(), "")
            set_current_label("outer")
            try:
                with self.assertRaises(RuntimeError):
                    with progress.task("2021002.md"):
                        raise RuntimeError("boom")
                self.assertEqual(current_label(), "outer")
            finally:
                set_current_label("")


if __name__ == "__main__":
    unittest.main()
