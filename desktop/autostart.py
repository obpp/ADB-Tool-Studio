# -*- coding: utf-8 -*-
"""开机自启：往 HKCU\\...\\Run 写一个值。

只写当前用户（HKCU）—— 写 HKLM 要管理员权限、每次开机弹 UAC，而且对这台机器上
的其他用户也不合适。想关掉就在托盘里取消勾选，或者用任务管理器的「启动」页。
"""

import os
import sys

import apppaths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ADB-Tool-Studio"
# 改名前的自启项：还留着的话，开机时会去拉一个已经不存在的旧 exe。
# 每次写设置时顺手清掉，不单独做迁移。
_LEGACY_VALUES = ("my-adb-app",)


def launch_command():
    """开机要执行的那条命令行。"""
    if apppaths.is_frozen():
        return '"%s" --minimized' % sys.executable
    # 源码模式：优先用 pythonw.exe，否则每次开机都会闪一个黑框
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pythonw):
        exe = pythonw
    script = os.path.join(apppaths.install_dir(), "desktop", "app.py")
    return '"%s" "%s" --minimized' % (exe, script)


def is_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled):
    """返回 (是否成功, 给人看的说明)。"""
    if sys.platform != "win32":
        return False, "只有 Windows 支持开机自启"
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            for legacy in _LEGACY_VALUES:
                try:
                    winreg.DeleteValue(key, legacy)
                except FileNotFoundError:
                    pass
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True, ("已设为开机自启" if enabled else "已取消开机自启")
    except OSError as exc:
        return False, "改注册表失败: %s" % exc
