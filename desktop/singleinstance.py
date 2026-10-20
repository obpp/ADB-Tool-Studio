# -*- coding: utf-8 -*-
"""只允许跑一个实例。

跑两个实例会各自随机挑一个端口，界面上看不出区别，但托盘会冒出两个图标，
两个进程还会同时往同一份 config/*.json 里写。所以第二个实例直接退出，
不弹第二个窗口。

用命名互斥体（named mutex）而不是锁文件：进程无论怎么死，互斥体都由系统自动释放，
不会留下一个「文件还在，但没人在跑」的假死锁。
"""

import sys

_MUTEX_NAME = "my-adb-app-single-instance"
_ERROR_ALREADY_EXISTS = 183

_handle = None


def acquire():
    """抢到返回 True；已有实例在跑返回 False。非 Windows 上不设限制。"""
    global _handle
    if sys.platform != "win32":
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not handle:
        return True                        # 建不出来就别拦着用户
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    _handle = handle                       # 故意不关：进程退出时由系统释放
    return True
