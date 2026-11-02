"""并行进度显示测试：计数、TTY 单行刷新、非 TTY 降级、日志清行。"""

from __future__ import annotations

import io
import logging
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.progress import (  # noqa: E402
    ParallelProgress, install_logging_bridge,
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


if __name__ == "__main__":
    unittest.main()
