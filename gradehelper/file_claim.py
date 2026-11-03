"""doc / sen 阶段独占占用结果文件，避免运行期间被 Excel 等程序打开后写不进去。

Windows 上以 ``FILE_SHARE_READ`` 共享模式持有目标文件句柄：其它进程（如 Excel）请求
写权限会得到共享冲突，只能以只读方式打开，因此运行期间不会出现"跑到最后写不进
结果文件"的情况。非 Windows 平台没有 Excel 抢锁的场景，退化为空操作。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33

_kernel32 = None


def _get_kernel32():
    """延迟加载 kernel32（仅 Windows 有效）。"""
    global _kernel32
    if _kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        _kernel32 = kernel32
    return _kernel32


class ResultFileClaim:
    """以独占方式占用结果文件（进程内持有句柄，release() 时释放）。

    用法::

        claim = ResultFileClaim(result_path)
        claim.acquire()          # 被其它程序打开时抛 RuntimeError（提示后退出）
        ...                      # 阶段运行期间一直持有
        claim.release()
    """

    def __init__(self, path: str | Path | None = None):
        self.path: Path | None = Path(path) if path else None
        self._handle: int | None = None

    # ---------------------------------------------------------------- 状态

    @property
    def held(self) -> bool:
        """是否已持有独占句柄。"""
        return self._handle is not None

    # ---------------------------------------------------------------- 占用

    def acquire(self, raise_on_error: bool = True) -> bool:
        """占用结果文件；返回是否真的持有句柄。

        文件不存在（首次运行）或非 Windows 平台上不占用，返回 False。
        被其它程序打开时：raise_on_error=True 抛 RuntimeError，否则记 warning 并返回 False。
        """
        if self.held or self.path is None or sys.platform != "win32":
            return self.held
        if not self.path.exists():
            return False
        handle = _get_kernel32().CreateFileW(
            str(self.path), _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ, None,
            _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            message = self._describe_error(ctypes.get_last_error())
            if raise_on_error:
                raise RuntimeError(message)
            logger.warning(message)
            return False
        self._handle = handle
        logger.debug("已独占占用结果文件: %s", self.path)
        return True

    def release(self) -> None:
        """释放独占句柄（可重复调用）。"""
        if self._handle is None:
            return
        _get_kernel32().CloseHandle(self._handle)
        self._handle = None
        logger.debug("已释放结果文件占用: %s", self.path)

    def retarget(self, path: str | Path | None) -> bool:
        """切换到新的目标文件；路径变化时释放旧占用并占用新文件。"""
        new_path = Path(path) if path else None
        if new_path == self.path:
            return self.held
        self.release()
        self.path = new_path
        return self.acquire(raise_on_error=True)

    # ---------------------------------------------------------------- 错误文案

    def _describe_error(self, err: int) -> str:
        where = f"结果文件：{self.path}"
        if err in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
            return f"结果文件正被其它程序（如 Excel）打开，请关闭后重试。\n{where}"
        if err == _ERROR_ACCESS_DENIED:
            return f"结果文件不可写（可能为只读或权限不足）。\n{where}"
        return f"结果文件无法占用（Windows 错误码 {err}）。\n{where}"

    # ---------------------------------------------------------------- 上下文管理

    def __enter__(self) -> "ResultFileClaim":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False
