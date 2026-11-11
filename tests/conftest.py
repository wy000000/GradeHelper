"""测试公共夹具：把详情日志（append_failure_detail）重定向到临时目录，
避免测试运行污染仓库根目录下的 log 文件夹。"""

import gradehelper.run_log as _run_log
import pytest


@pytest.fixture(autouse=True)
def _detail_log_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(_run_log, "_DETAIL_LOG_DIR", tmp_path / "log")
