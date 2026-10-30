# -*- coding: utf-8 -*-
"""ADB Tool Studio 桌面版入口：内嵌服务 + 原生窗口 + 托盘。

跟网页版的关系只有一条：这里是**调用方**。server.py 没有被替代 ——
AppServer 起的是同一个 Handler、同一份 registry、同一套 static/ 页面，
所以 `python3 server.py` 照样能用，两边不会打架。

桌面版多出来的三件事：

  * 双击就开，不用自己记端口、也不用先开浏览器
  * 端口交给系统随机分配，不用跟 8765 抢
  * 关掉窗口不退出，缩进托盘继续跑（配合开机自启就是常驻）
"""

import argparse
import os
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import apppaths                                   # noqa: E402
import server as webapp                           # noqa: E402
from desktop import autostart, singleinstance     # noqa: E402
from desktop.tray import Tray                     # noqa: E402

APP_TITLE = "ADB Tool Studio"
LOG_MAX_BYTES = 512 * 1024

# WebView2 运行时的产品 ID（微软文档里给的固定 GUID）
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


# --------------------------------------------------------------------------
# 日志：打包成 exe 之后没有控制台，出问题只能靠这个文件
# --------------------------------------------------------------------------

_log_lock = threading.Lock()
_log_path = None


def _log_file():
    global _log_path
    if _log_path is None:
        try:
            os.makedirs(apppaths.logs_dir(), exist_ok=True)
            _log_path = os.path.join(apppaths.logs_dir(), "app.log")
        except OSError:
            _log_path = ""
    return _log_path


def _rotate_if_big(path):
    try:
        if os.path.getsize(path) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(path, path + ".1")     # 只留一份旧的，不做完整轮转
    except OSError:
        pass


def log(message):
    """写一行日志，同时尽量往控制台也吐一份（源码模式调试用）。"""
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with _log_lock:
        path = _log_file()
        if path:
            _rotate_if_big(path)
            try:
                with open(path, "a", encoding="utf-8") as fp:
                    fp.write(line)
            except OSError:
                pass
    if sys.stderr:                        # --noconsole 时它是 None
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass


def message_box(title, text, error=True):
    """没有控制台的时候，唯一能让人当场看见问题的办法。"""
    if sys.platform == "win32":
        try:
            import ctypes
            flags = 0x10 if error else 0x40        # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)
            return
        except Exception:
            pass
    if sys.stderr:
        try:
            sys.stderr.write("%s: %s\n" % (title, text))
            sys.stderr.flush()
        except Exception:
            pass


def webview2_installed():
    """查 WebView2 运行时在不在。

    查不到只是「可能不能用」—— 所以调用方只拿它来决定要不要先走浏览器兜底，
    而不是直接拒绝启动。Win11 和装了 Edge 的 Win10 都有。
    """
    if sys.platform != "win32":
        return True
    import winreg
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients"),
    )
    for root, path in roots:
        try:
            with winreg.OpenKey(root, path + "\\" + WEBVIEW2_CLIENT) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return True
    return False


# --------------------------------------------------------------------------

class PageApi(object):
    """暴露给页面 JS 的原生能力（`window.pywebview.api`）。

    网页版里没有这座桥 —— 浏览器不会把选中文件的完整路径告诉页面。
    前端先看桥在不在，不在就退回服务端的目录浏览器。

    注意：这个对象会被 pywebview 递归 introspection（util.get_functions），
    所以**别往上面挂窗口、GUI 之类的引用** —— 它会把整个对象树摸一遍，
    碰到 .NET 控件属性就可能把进程带崩。窗口从 webview.windows 里现取。
    """

    def pick_image(self, directory=""):
        """弹系统的选文件对话框，返回选中的完整路径；用户取消返回 None。"""
        import webview
        if not webview.windows:
            return None
        window = webview.windows[0]
        directory = os.path.expanduser(directory) if isinstance(directory, str) else ""
        if not directory or not os.path.isdir(directory):
            directory = ""
        try:
            paths = window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=directory,
                allow_multiple=False,
                file_types=("镜像文件 (*.img;*.bin;*.raw;*.iso)",
                            "所有文件 (*.*)"),
            )
        except Exception as exc:
            log("打开文件对话框失败: %r" % (exc,))
            return None
        if not paths:
            return None
        return paths[0]


class DesktopApp(object):

    # 页面每 10 秒来一次心跳。缩在托盘/最小化时 Chromium 会把定时器压到
    # 大约每分钟一次，所以阈值不能贴着 10 秒，否则天天误报。
    UI_BEAT_TIMEOUT = 120
    UI_BEAT_POLL = 5
    UI_STALE_GAP = 90          # 窗口重新显示时，超过这么久没心跳就认为画面僵了

    def __init__(self, host, port, minimized, debug=False, remote_debug_port=0):
        self.host = host
        self.port = port
        self.start_minimized = minimized
        self.debug = debug
        self.remote_debug_port = remote_debug_port
        self.httpd = None
        self.window = None
        self.tray = Tray(self)
        self._quitting = False
        self._told_about_tray = False
        self._ui_stale = False       # 心跳断了，页面很可能已经白屏
        self._auto_reloaded = False  # 本轮中断已经自动重载过，等新心跳再解锁
        self._reload_lock = threading.Lock()
        self._last_reload_check = 0

    def log(self, message):
        log(message)

    def _watch_ui(self):
        """页面心跳停了就记一笔，必要时把页面重新加载回来。

        只看最近还有心跳的那个页面：同一个标签页换页不会换 id，所以正常
        导航不会误报；开着的另一个标签页还在跳时，也不会因为旧的退出了就报警。
        """
        last_id = None
        last_beat = None
        alarm = False
        prev_now = time.time()
        while not self._quitting:
            time.sleep(self.UI_BEAT_POLL)
            now = time.time()

            # 系统休眠时整个进程也停着；醒来后第一次循环的时间跳变很明显
            if now - prev_now > max(30, self.UI_BEAT_POLL * 6):
                self.log("检测到系统休眠 / 长时间挂起: 约 %d 秒没有运行"
                         % int(now - prev_now))
            prev_now = now

            cur = webapp.ui_latest()
            if cur is None:               # 还没加载过页面（比如启动就缩托盘）
                continue

            cid, beat = cur["id"], cur["beat"]
            if cid != last_id:            # 换了一个页面会话，重新开始算
                last_id, last_beat, alarm = cid, beat, False
                self._ui_stale = False
                self._auto_reloaded = False
                continue
            if beat != last_beat:
                if alarm:
                    self.log("页面心跳恢复: %s（中断约 %d 秒）"
                             % (cur["page"] or "?", int(now - beat)))
                alarm = False
                self._ui_stale = False
                self._auto_reloaded = False
                last_beat = beat
                continue
            if alarm or now - beat < self.UI_BEAT_TIMEOUT:
                continue
            alarm = True
            self._ui_stale = True
            gap = int(now - beat)
            if cur["visible"]:
                self.log("页面心跳中断: %s 已 %d 秒没有心跳"
                         "（可能白屏 / 渲染进程退出 / 刚从休眠唤醒），"
                         "正在重新加载页面" % (cur["page"] or "?", gap))
                self.reload_page(auto=True)
            else:
                self.log("页面心跳中断: %s 已 %d 秒没有心跳"
                         "（最后状态是后台，可能只是缩在托盘里；"
                         "重新显示窗口时会自动重载）" % (cur["page"] or "?", gap))

    def reload_page(self, auto=False):
        """把窗口导航回最后一次心跳所在页面。

        白屏时页面自己已经没法执行 JS（location.reload 也叫不动），所以
        走浏览器的 load_url 重新导航。auto=True 时每轮中断只自动试一次，
        免得页面彻底起不来时反复刷新。
        """
        if self._quitting or self.window is None or self.httpd is None:
            return False
        with self._reload_lock:
            if auto and self._auto_reloaded:
                return False
            cur = webapp.ui_latest()
            page = (cur or {}).get("page") or "/"
            if (not page.startswith("/") or len(page) > 200 or
                    any(ch not in
                        "abcdefghijklmnopqrstuvwxyz"
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-."
                        for ch in page)):
                page = "/"
            target = self.httpd.url.rstrip("/") + page
            try:
                loader = getattr(self.window, "load_url", None)
                if loader is not None:
                    loader(target)
                else:                     # 老版本 pywebview 的兜底
                    self.window.evaluate_js("location.reload()")
            except Exception as exc:
                self.log("重新加载页面失败: %r" % (exc,))
                return False
            self._auto_reloaded = True
            self._ui_stale = False
            self.log("已重新加载页面: %s（%s）"
                     % (page, "心跳中断" if auto else "手动"))
            return True

    def _reload_if_stale(self, delay):
        """窗口重新显示时用：给页面两秒自己恢复，没恢复就重载。"""
        if delay:
            time.sleep(delay)
        if self._quitting:
            return
        cur = webapp.ui_latest()
        if cur is None:
            return
        if time.time() - cur["beat"] < self.UI_STALE_GAP:
            return
        self._ui_stale = True
        self.reload_page(auto=True)

    def _on_window_shown(self):
        # shown / restored 可能连着来，几秒内只检查一次
        now = time.time()
        if now - self._last_reload_check < 3:
            return
        self._last_reload_check = now
        threading.Thread(target=self._reload_if_stale, args=(2.0,),
                         daemon=True).start()

    # ---------------- 生命周期 ----------------

    def run(self):
        self.httpd = webapp.AppServer(self.host, self.port)
        self.httpd.start()
        log("服务已启动: %s" % self.httpd.url)
        log("数据目录: %s" % apppaths.data_dir())
        log("日志目录: %s" % apppaths.logs_dir())
        log("WebView 数据目录: %s" % apppaths.webview_dir())
        log("资源目录: %s" % apppaths.resource_dir())
        log("打包模式: %s" % ("是" if apppaths.is_frozen() else "否（源码运行）"))

        # 页面的心跳和 JS 报错都走这个 sink，落进同一份 app.log
        webapp.set_client_log_sink(log)
        threading.Thread(target=self._watch_ui, name="ui-watch",
                         daemon=True).start()

        if self.tray.start():
            log("托盘已就绪")
        else:
            log("托盘不可用: %s" % self.tray.error)

        try:
            return self._run_window()
        finally:
            self.shutdown()

    def _run_window(self):
        try:
            import webview
        except ImportError as exc:
            return self._run_without_window("没装 pywebview（%s）" % exc)

        if sys.platform == "win32" and not webview2_installed():
            return self._run_without_window("没检测到 WebView2 运行时")

        # 白屏时窗口里的 DevTools 可能点不开，留一个外部调试口更可靠
        if self.remote_debug_port:
            try:
                webview.settings["REMOTE_DEBUGGING_PORT"] = int(
                    self.remote_debug_port)
            except Exception as exc:
                log("设置远程调试端口失败: %r" % (exc,))

        # 开机自启时不弹窗；但没有托盘的话缩进去就再也叫不出来了
        hidden = self.start_minimized and self.tray.available

        page_api = PageApi()
        try:
            self.window = webview.create_window(
                APP_TITLE,
                self.httpd.url,
                js_api=page_api,
                width=1120, height=840,
                min_size=(900, 620),
                hidden=hidden,
                text_select=True,          # 日志和命令行要能选中复制
            )
        except Exception as exc:
            return self._run_without_window("建窗口失败: %r" % (exc,))

        self.window.events.closing += self._on_closing
        # 从托盘/最小化回来时，如果页面心跳已经断了，顺手重载一次
        for name in ("shown", "restored"):
            event = getattr(self.window.events, name, None)
            if event is not None:
                event += self._on_window_shown

        # WebView2 的用户数据必须在本机盘：exe 放在映射网络盘上时，
        # 拔网线 / 网络一抖，渲染进程就会卡死白屏（见 apppaths.webview_dir）
        storage = apppaths.webview_dir()
        try:
            os.makedirs(storage, exist_ok=True)
        except OSError:
            storage = None

        debug = bool(self.debug) or os.environ.get("ATS_DEBUG") == "1"
        start_args = {"private_mode": False, "debug": debug}
        if storage:
            start_args["storage_path"] = storage
        icon = apppaths.icon_path()
        if os.path.isfile(icon):
            start_args["icon"] = icon

        log("打开窗口: %s" % self.httpd.url)
        if debug:
            log("调试模式已开启：窗口里右键 → 检查，或按 Ctrl+Shift+I")
        if self.remote_debug_port:
            log("远程调试端口已开启: http://127.0.0.1:%d"
                % int(self.remote_debug_port))
        try:
            webview.start(**start_args)
        except Exception as exc:
            log("原生窗口起不来: %r" % (exc,))
            log(traceback.format_exc())
            return self._run_without_window("原生窗口起不来: %s" % exc)
        return 0

    def _run_without_window(self, reason):
        """窗口起不来时的兜底：用系统浏览器打开，托盘照常挂着。

        这样至少不是白屏或者一闪而过 —— 人能看见东西，也能看见到底是哪儿不对。
        """
        log("退回浏览器模式: %s" % reason)
        try:
            import webbrowser
            webbrowser.open(self.httpd.url)
        except Exception as exc:
            log("连浏览器都打不开: %r" % (exc,))
            message_box(APP_TITLE,
                        "原生窗口起不来（%s），浏览器也没能打开。\n\n"
                        "请手动访问：%s" % (reason, self.httpd.url))
            return 1

        if not self.tray.available:
            log("托盘也没有，进程会一直挂着，Ctrl+C 退出")
        try:
            while not self._quitting:
                time.sleep(0.4)
        except KeyboardInterrupt:
            pass
        return 0

    def shutdown(self):
        if self.httpd is not None:
            self.httpd.stop()
        log("=== 已退出 ===")

    # ---------------- 给托盘调用的 ----------------

    def _on_closing(self):
        """点窗口的 ×：不退出，缩进托盘。真要退出走托盘菜单。"""
        if self._quitting or not self.tray.available:
            return True                   # 返回真值 = 放行，这次关闭照做
        try:
            self.window.hide()
        except Exception:
            return True                   # 藏不起来就干脆关掉，别留个关不掉的窗口
        if not self._told_about_tray:
            self._told_about_tray = True
            self.tray.notify("窗口已收进托盘，程序还在后台跑。要退出请右键托盘图标。")
        return False                      # 返回假值 = 取消这次关闭

    def show_window(self):
        if self.window is None:
            # 浏览器模式：没有窗口可显示，那就再打开一次页面
            if self.httpd is not None:
                try:
                    import webbrowser
                    webbrowser.open(self.httpd.url)
                except Exception:
                    pass
            return
        try:
            self.window.show()
            self.window.restore()
        except Exception as exc:
            log("显示窗口失败: %r" % (exc,))
        self._on_window_shown()

    def stop_all_tools(self):
        stopped = 0
        for tool in webapp.registry.all():
            try:
                ok, _ = tool.stop()
            except Exception as exc:
                log("停止 %s 出错: %r" % (tool.id, exc))
                continue
            if ok:
                stopped += 1
        return stopped

    def toggle_autostart(self):
        enable = not autostart.is_enabled()
        ok, message = autostart.set_enabled(enable)
        log(message if ok else "开机自启设置失败: %s" % message)
        return ok, message

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        log("收到退出请求")
        self.tray.stop()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
        # httpd 的收尾交给 run() 的 finally，等 webview.start() 返回后再做，
        # 免得页面还在收尾就把服务掐了


# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="ADB Tool Studio 桌面版")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=0,
                        help="监听端口，0 表示让系统挑一个空闲的（默认）")
    parser.add_argument("--minimized", action="store_true",
                        help="启动后直接进托盘，不弹窗口（开机自启用的就是它）")
    parser.add_argument("--force", action="store_true",
                        help="跳过单实例检查（开发用：托盘里还挂着旧实例时）")
    parser.add_argument("--debug", action="store_true",
                        help="打开 WebView2 开发者工具"
                             "（也可以设环境变量 ATS_DEBUG=1）")
    parser.add_argument("--remote-debugging-port", type=int, default=0,
                        help="另开一个 DevTools 调试端口（0 = 关闭）；"
                             "白屏时用 Edge 打开 http://127.0.0.1:<端口>")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if not args.force and not singleinstance.acquire():
        log("已经有一个实例在跑了，这次退出")
        message_box(APP_TITLE,
                    "ADB Tool Studio 已经在运行了。\n\n"
                    "要打开它的窗口：看屏幕右下角的托盘图标，双击。",
                    error=False)
        return 0

    try:
        return DesktopApp(args.host, args.port, args.minimized, args.debug,
                          args.remote_debugging_port).run()
    except Exception as exc:
        log("启动失败: %r" % (exc,))
        log(traceback.format_exc())
        message_box(APP_TITLE,
                    "启动失败：\n\n%s\n\n详情见日志：\n%s"
                    % (exc, os.path.join(apppaths.logs_dir(), "app.log")))
        return 1


if __name__ == "__main__":
    sys.exit(main())
