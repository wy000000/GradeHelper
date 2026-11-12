"""LLM 客户端静默超时测试：心跳注释不续命、静默基准取有效数据、连接超时回退。"""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import EndpointConfig  # noqa: E402
from gradehelper.llm_client import (  # noqa: E402
    LLMClient, _DEFAULT_CONNECT_TIMEOUT, _StreamResult,
)
from gradehelper.request_track import REQUEST_REGISTRY  # noqa: E402


class TestHandleSSESilenceBasis(unittest.TestCase):
    """静默基准只由有效数据行刷新：心跳注释行/空行不算。"""

    def setUp(self):
        self.client = LLMClient(EndpointConfig(read_timeout=300))
        self.slot = REQUEST_REGISTRY.register("2021001-P1")
        self.addCleanup(REQUEST_REGISTRY.unregister, self.slot)
        self.result = _StreamResult()

    def test_comment_and_blank_lines_do_not_touch(self):
        """``: ping`` 心跳注释与空行不刷新静默基准（僵死连接无法续命）。"""
        self.slot.progress_ts = 0.0
        self.assertFalse(self.client._handle_sse_line(b": ping", self.result, self.slot))
        self.assertFalse(self.client._handle_sse_line(b"", self.result, self.slot))
        self.assertFalse(
            self.client._handle_sse_line(b"   \r", self.result, self.slot))
        self.assertEqual(self.slot.progress_ts, 0.0)

    def test_data_lines_touch(self):
        """``data:`` 帧与不带帧头的 JSON 行刷新静默基准。"""
        self.slot.progress_ts = 0.0
        payload = b'data: {"choices":[{"delta":{"content":"a"}}]}'
        self.assertFalse(self.client._handle_sse_line(payload, self.result, self.slot))
        self.assertGreater(self.slot.progress_ts, 0.0)
        self.slot.progress_ts = 0.0
        self.assertFalse(
            self.client._handle_sse_line(b'{"choices":[]}', self.result, self.slot))
        self.assertGreater(self.slot.progress_ts, 0.0)

    def test_done_still_terminates(self):
        """[DONE] 帧语义保持不变。"""
        self.assertTrue(self.client._handle_sse_line(b"data: [DONE]",
                                                     self.result, self.slot))

    def test_slot_optional(self):
        """不传 slot（旧调用方）时不报错。"""
        self.assertFalse(self.client._handle_sse_line(
            b'data: {"choices":[]}', _StreamResult()))


class TestRecvSomeSilence(unittest.TestCase):
    """``_recv_some``：静默超时基于 slot 基准，检查在循环顶部。"""

    def setUp(self):
        self.client = LLMClient(EndpointConfig(read_timeout=1))
        server, client_sock = socket.socketpair()
        self.server = server
        self.sock = client_sock
        self.slot = REQUEST_REGISTRY.register("2021002-P3")
        self.addCleanup(server.close)
        self.addCleanup(client_sock.close)
        self.addCleanup(REQUEST_REGISTRY.unregister, self.slot)

    def test_stale_slot_raises_timeout(self):
        """静默超阈值即失败：对端不发任何数据（含心跳）也会触发。"""
        self.slot.progress_ts = time.monotonic() - 5.0
        with self.assertRaises(TimeoutError) as ctx:
            self.client._recv_some(self.sock, 1, self.slot)
        self.assertIn("未返回有效数据", str(ctx.exception))

    def test_data_received_before_deadline(self):
        """阈值内有数据即返回，不触发超时。"""
        self.slot.progress_ts = time.monotonic()
        self.server.sendall(b"chunk")
        data = self.client._recv_some(self.sock, 1, self.slot)
        self.assertEqual(data, b"chunk")

    def test_silence_disabled_when_zero(self):
        """``silence=0`` 时不判静默超时（保持既有语义）。"""
        self.slot.progress_ts = time.monotonic() - 999.0
        self.server.sendall(b"ok")
        data = self.client._recv_some(self.sock, 0, self.slot)
        self.assertEqual(data, b"ok")

    def test_request_timeout_exceeded_even_with_fresh_data(self):
        """总上限超限即失败：即使一直有有效数据（静默基准很新）。"""
        self.slot.request_timeout = 2.0
        self.slot.started = time.monotonic() - 3.0
        self.slot.progress_ts = time.monotonic()  # 静默很新，不触发静默超时
        with self.assertRaises(TimeoutError) as ctx:
            self.client._recv_some(self.sock, 1, self.slot)
        self.assertIn("request_timeout", str(ctx.exception))

    def test_request_timeout_disabled_when_zero(self):
        """``request_timeout=0`` 表示不限总时长。"""
        self.slot.request_timeout = 0.0
        self.slot.started = time.monotonic() - 999.0
        self.server.sendall(b"ok")
        data = self.client._recv_some(self.sock, 0, self.slot)
        self.assertEqual(data, b"ok")

    def test_register_carries_request_timeout(self):
        """登记时把 yaml 配置的总上限写入槽位；默认为 0（不限）。"""
        slot = REQUEST_REGISTRY.register("2021009-P1", request_timeout=600)
        self.addCleanup(REQUEST_REGISTRY.unregister, slot)
        self.assertEqual(slot.request_timeout, 600.0)
        default_slot = REQUEST_REGISTRY.register("")
        self.addCleanup(REQUEST_REGISTRY.unregister, default_slot)
        self.assertEqual(default_slot.request_timeout, 0.0)


class TestConnectTimeoutFallback(unittest.TestCase):
    def test_default_fallback_constant(self):
        """connect_timeout 未配置时按 30 秒回退，不再无限等待。"""
        self.assertEqual(_DEFAULT_CONNECT_TIMEOUT, 30)
        client = LLMClient(EndpointConfig())
        with patch("gradehelper.llm_client.socket.create_connection",
                   return_value=socket.socket()) as conn:
            client._open_socket("http", "127.0.0.1", 80)
            _args, kwargs = conn.call_args
            self.assertEqual(kwargs.get("timeout"), 30)


if __name__ == "__main__":
    unittest.main()
