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
        target = os.path.join(base, "my-adb-app")
        os.makedirs(target, exist_ok=True)
    _data_dir = target
    return _data_dir


def static_dir():
    return os.path.join(resource_dir(), "static")


def config_dir():
    return os.path.join(data_dir(), "config")


def logs_dir():
    return os.path.join(data_dir(), "logs")


def icon_path():
    return os.path.join(resource_dir(), "icon.ico")


def bundled_adb():
    """跟 exe 放在一起的 adb 也算数 —— 绿色版常这么放。"""
    out = []
    for name in ("adb", "adb.exe"):
        out.append(os.path.join(install_dir(), name))
        out.append(os.path.join(install_dir(), "platform-tools", name))
    return out
