"""测试公共夹具：把附属详情日志的回退目录（_DETAIL_LOG_DIR）重定向到临时目录，
避免测试运行污染仓库根目录下的 log 文件夹。

正常运行时详情日志由 RunLogMixin._append_failure_detail 解析为
「主 log 所在目录/log」；测试里设置了 ``grader._log_path`` 的会写到对应
临时目录下的 ``log`` 子文件夹，未设置的走这里的回退目录。
"""

import gradehelper.run_log as _run_log
import pytest


@pytest.fixture(autouse=True)
def _detail_log_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(_run_log, "_DETAIL_LOG_DIR", tmp_path / "log")
