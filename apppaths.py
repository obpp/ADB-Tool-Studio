# -*- coding: utf-8 -*-
"""路径解析：源码运行 和 PyInstaller 打包后（frozen）两种模式。

打包后有两条路径必须分开，混在一起会出事：

  * 只读资源（static/、icon.ico）随 exe 打进包里，运行时躺在 sys._MEIPASS
  * 可写数据（config/、logs/）**绝不能**写进 _MEIPASS —— 单文件模式那是解压出来的
    临时目录，进程一退就被删，用户改的配置会无声无息地丢掉

所以对外只暴露 resource_dir()（只读）和 data_dir()（可写），别的地方不要自己拼路径。
"""

import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def is_frozen():
    return FROZEN


def install_dir():
    """exe 所在目录；源码模式下就是源码目录。"""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return _SOURCE_DIR


def resource_dir():
    """只读资源目录：static/、icon.ico 都在它下面。"""
    if FROZEN:
        return getattr(sys, "_MEIPASS", install_dir())
    return _SOURCE_DIR


def _can_write(directory):
    probe = os.path.join(directory, ".write-probe")
    try:
        with open(probe, "w") as fp:
            fp.write("")
        os.remove(probe)
        return True
    except OSError:
        return False


_data_dir = None


def data_dir():
    """可写数据目录，config/ 和 logs/ 都在它下面。

    优先用 exe 旁边（绿色版：整个文件夹拷到 U 盘也能带走配置）；
    旁边写不进去（装在 Program Files 之类）才退回用户目录。
    """
    global _data_dir
    if _data_dir:
        return _data_dir
    target = install_dir()
    if not _can_write(target):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        target = os.path.join(base, "ADB-Tool-Studio")
        os.makedirs(target, exist_ok=True)
    _data_dir = target
    return _data_dir


def _is_remote_path(path):
    """Windows 上判断路径是不是落在映射的网络盘 / UNC 共享上。

    exe 放在 Z: 这种映射盘上很常见（比如把 Linux 的 samba 目录映射过来）。
    网络一断，那块盘上的所有东西都不可用 —— 拔网线就会白屏。
    """
    if sys.platform != "win32":
        return False
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return True
    try:
        import ctypes
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return False
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        get_type.argtypes = [ctypes.c_wchar_p]
        get_type.restype = ctypes.c_uint
        return get_type(drive + "\\") == 4      # DRIVE_REMOTE
    except Exception:
        return False


_local_dir = None


def local_data_dir():
    """本机盘上的数据目录（%LOCALAPPDATA%），给不能碰网络盘的东西用。"""
    global _local_dir
    if _local_dir:
        return _local_dir
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        if sys.platform == "win32":
            base = os.path.expanduser("~")
        else:
            base = (os.environ.get("XDG_CACHE_HOME")
                    or os.path.join(os.path.expanduser("~"), ".cache"))
    target = os.path.join(base, "ADB-Tool-Studio")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return data_dir()
    if not _can_write(target):
        return data_dir()
    _local_dir = target
    return _local_dir


def webview_dir():
    """WebView2 的用户数据目录，永远放本机盘。

    Chromium 的缓存 / GPU 缓存 / Crashpad 都在这里，网络盘上要么锁不住，
    要么网络一断整个渲染进程就卡死，表现出来就是白屏。所以哪怕 exe 是
    绿色版、数据都放它旁边，这一份也必须留在本机。
    """
    return os.path.join(local_data_dir(), "webview")


def static_dir():
    return os.path.join(resource_dir(), "static")


def config_dir():
    return os.path.join(data_dir(), "config")


_logs_dir = None


def logs_dir():
    """日志目录。数据目录在网络盘上时改用本机盘 —— 拔网线之后恰恰最需要日志。"""
    global _logs_dir
    if _logs_dir:
        return _logs_dir
    target = os.path.join(data_dir(), "logs")
    if _is_remote_path(target):
        target = os.path.join(local_data_dir(), "logs")
    _logs_dir = target
    return _logs_dir


def icon_path():
    return os.path.join(resource_dir(), "icon.ico")


def bundled_adb():
    """跟 exe 放在一起的 adb 也算数 —— 绿色版常这么放。"""
    out = []
    for name in ("adb", "adb.exe"):
        out.append(os.path.join(install_dir(), name))
        out.append(os.path.join(install_dir(), "platform-tools", name))
    return out
