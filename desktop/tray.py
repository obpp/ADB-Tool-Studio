# -*- coding: utf-8 -*-
"""托盘图标。窗口关掉之后程序就住在这里。

pystray 的菜单回调跑在它自己的线程里，而 pywebview 的窗口方法（show / hide /
destroy）本来就是设计成可以跨线程调用的，所以这里直接调，不用自己再排一次队列。

托盘是可选件：没装 pystray / Pillow、或者找不到图标文件时，available 就是 False，
app.py 会退回「没有托盘」的行为（关窗即退出）。
"""

import os
import threading

import apppaths

try:
    import pystray
    from PIL import Image
    _IMPORT_ERROR = ""
except ImportError as exc:                  # 桌面版的依赖没装齐
    pystray = None
    Image = None
    _IMPORT_ERROR = str(exc)


def _load_image():
    if Image is None:
        return None
    path = apppaths.icon_path()
    if not os.path.isfile(path):
        return None
    try:
        image = Image.open(path)
        image.load()
        return image
    except (OSError, ValueError):
        return None


class Tray(object):

    def __init__(self, app):
        self.app = app
        self.icon = None
        self.thread = None
        self.available = False
        self.error = ""

    def start(self):
        """起得来返回 True。失败不抛异常 —— 没有托盘也得能开窗口。"""
        if pystray is None:
            self.error = "没装 pystray/Pillow（%s）" % _IMPORT_ERROR
            return False
        image = _load_image()
        if image is None:
            self.error = "找不到可用的 %s" % apppaths.icon_path()
            return False

        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", self._on_show, default=True),
            pystray.MenuItem("全部停止", self._on_stop_all),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", self._on_toggle_autostart,
                             checked=lambda item: _autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._on_quit),
        )
        self.icon = pystray.Icon("ADB-Tool-Studio", image, "ADB Tool Studio", menu)
        self.thread = threading.Thread(target=self._run, name="tray")
        self.thread.daemon = True
        self.thread.start()
        self.available = True
        return True

    def _run(self):
        try:
            self.icon.run()
        except Exception as exc:
            self.available = False
            self.app.log("托盘线程挂了: %r" % (exc,))

    def stop(self):
        if self.icon is None:
            return
        try:
            self.icon.stop()
        except Exception:
            pass

    def notify(self, message, title="ADB Tool Studio"):
        if self.icon is None:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass                            # 通知不该影响正事

    # ---------------- 菜单回调 ----------------

    def _on_show(self, icon=None, item=None):
        self.app.show_window()

    def _on_stop_all(self, icon=None, item=None):
        count = self.app.stop_all_tools()
        self.notify("已请求停止 %d 个任务" % count if count else "当前没有任务在跑")

    def _on_toggle_autostart(self, icon=None, item=None):
        ok, message = self.app.toggle_autostart()
        self.notify(message if ok else "开机自启设置失败：%s" % message)

    def _on_quit(self, icon=None, item=None):
        self.app.quit()


def _autostart_enabled():
    # 放到函数里 import，免得 pystray 缺席时也去碰 winreg
    from desktop import autostart
    return autostart.is_enabled()
