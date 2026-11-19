# -*- coding: utf-8 -*-
"""路径解析：源码运行 和 PyInstaller 打包后（frozen）两种模式。

打包后有两条路径必须分开，混在一起会出事：

  * 只读资源（static/、icon.ico）随 exe 打进包里，运行时躺在 sys._MEIPASS
  * 可写数据（config/、logs/）**绝不能**写进 _MEIPASS —— 单文件模式那是解压出来的
    临时目录，进程一退就被删，用户改的配置会无声无息地丢掉

所以对外只暴露 resource_dir()（只读）和 data_dir()（可写），别的地方不要自己拼路径。
"""

import json
import os
import sys
import tempfile

FROZEN = bool(getattr(sys, "frozen", False))
_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

# 记住自定义数据目录的指针文件。它永远放在默认数据目录里：
# 指针自己跟着搬走的话，下次启动就找不到它了。
DATA_POINTER_NAME = "data_dir.json"


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
    # 用 mkstemp 生成互不相同的名字。设置页会反复问、多个标签页还会并发，
    # 固定叫 .write-probe 的话两个线程会互相删掉对方的探针，把可写的目录
    # 误判成只读。
    try:
        fd, probe = tempfile.mkstemp(prefix=".write-probe-", dir=directory)
    except OSError:
        return False
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def default_data_dir():
    """默认数据目录。

    优先用 exe 旁边（绿色版：整个文件夹拷到 U 盘也能带走配置）；
    旁边写不进去（装在 Program Files 之类）才退回用户目录。
    """
    target = install_dir()
    if not _can_write(target):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        target = os.path.join(base, "ADB-Tool-Studio")
        os.makedirs(target, exist_ok=True)
    return target


def data_dir_pointer_path():
    return os.path.join(default_data_dir(), DATA_POINTER_NAME)


def read_data_dir_pointer():
    """读记住的自定义位置，返回 (路径, 错误)。

    没有设置过时路径为空；文件坏了也给空路径 + 一句错误，
    调用方回退默认目录，不要因为一个坏文件起不来。
    """
    pointer = data_dir_pointer_path()
    try:
        with open(pointer, "r", encoding="utf-8") as fp:
            saved = json.load(fp)
    except FileNotFoundError:
        return "", ""
    except (IOError, OSError, ValueError) as exc:
        return "", "读不了 %s: %s" % (pointer, exc)
    if not isinstance(saved, dict):
        return "", "%s 的格式不对" % pointer
    value = saved.get("dataDir")
    if not isinstance(value, str) or not value.strip():
        return "", "%s 里没有 dataDir" % pointer
    return os.path.abspath(os.path.expanduser(value.strip())), ""


def write_data_dir_pointer(path):
    pointer = data_dir_pointer_path()
    os.makedirs(os.path.dirname(pointer), exist_ok=True)
    tmp = pointer + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump({"dataDir": os.path.abspath(path)}, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, pointer)


def clear_data_dir_pointer():
    try:
        os.remove(data_dir_pointer_path())
    except FileNotFoundError:
        pass


_data_dir = None
_data_dir_error = ""


def data_dir():
    """可写数据目录，config/、scripts/、files/、logs/ 都在它下面。

    默认是 exe 旁边，其次用户目录。用户在设置页换过位置时，
    data_dir.json 记着新位置；那个目录不存在（比如 U 盘没插）或
    写不进去就回退默认目录 —— 启动失败比丢掉设置更糟。
    """
    global _data_dir, _data_dir_error
    if _data_dir:
        return _data_dir
    default = default_data_dir()
    wanted, error = read_data_dir_pointer()
    if wanted:
        if os.path.isdir(wanted) and _can_write(wanted):
            _data_dir = wanted
            _data_dir_error = ""
            return _data_dir
        _data_dir_error = ("data_dir.json 指向的目录不可用（不存在或写不进去）："
                           "%s，已回退到默认目录" % wanted)
    elif error:
        _data_dir_error = error + "，已回退到默认目录"
    _data_dir = default
    return _data_dir


def data_dir_status():
    """给设置页用：当前目录、默认目录、指针状态和回退原因。"""
    current = data_dir()
    default = default_data_dir()
    pointer, error = read_data_dir_pointer()
    same = (os.path.normcase(os.path.abspath(current)) ==
            os.path.normcase(os.path.abspath(default)))
    return {
        "dataDir": current,
        "defaultDir": default,
        "isCustom": not same,
        "pointerPath": data_dir_pointer_path(),
        "pointer": pointer,
        "error": _data_dir_error or error,
    }


def apply_data_dir(path):
    """迁移成功后让当前进程改从新目录读写，并让日志路径重新解析。"""
    global _data_dir, _data_dir_error, _logs_dir
    _data_dir = os.path.abspath(path)
    _data_dir_error = ""
    _logs_dir = None


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


def logs_dir_for(directory):
    """指定数据目录对应的日志目录；网络盘上仍改用本机盘。

    设置页用它预览「切过去之后日志会写在哪」，此时还没 apply_data_dir。
    """
    target = os.path.join(directory, "logs")
    if _is_remote_path(target):
        target = os.path.join(local_data_dir(), "logs")
    return target


def logs_dir():
    """日志目录。数据目录在网络盘上时改用本机盘 —— 拔网线之后恰恰最需要日志。"""
    global _logs_dir
    if _logs_dir:
        return _logs_dir
    _logs_dir = logs_dir_for(data_dir())
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
