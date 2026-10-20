#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""my-adb-app —— adb 相关小工具的集合。

一级页面（/）列出所有功能，二级页面是每个功能自己的控制台。

* 后端：Python 3 标准库（http.server + ThreadingHTTPServer + SSE），零第三方依赖
* 前端：static/ 下的纯 HTML/CSS/JS，无构建步骤
* 加功能：在 tools/ 下写一个 Tool 子类，再登记到 tools/__init__.py 的 TOOL_CLASSES

用法：
    python3 server.py                       # 默认 127.0.0.1:8765
    python3 server.py --port 9000 --host 0.0.0.0

桌面版（desktop/app.py）也走这个文件，但不走 main()：
它用 AppServer 把服务挂到后台线程上，再套一个原生窗口。
所以这里的 AppServer / Handler / registry 都别写死成「只能有一个实例」。
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import apppaths

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = apppaths.static_dir()     # 打包后躺在 sys._MEIPASS 里
DATA_DIR = apppaths.data_dir()         # config/ 得写在可写的地方，绝不是 _MEIPASS

if APP_DIR not in sys.path:            # 允许从任意目录启动
    sys.path.insert(0, APP_DIR)

from tools import TOOL_CLASSES        # noqa: E402


class Registry(object):
    """按 id 存工具实例，顺序即一级页面的展示顺序。"""

    def __init__(self, app_dir, classes):
        self.order = []
        self.tools = {}
        for cls in classes:
            tool = cls(app_dir)
            self.tools[tool.id] = tool
            self.order.append(tool.id)

    def get(self, tool_id):
        return self.tools.get(tool_id)

    def all(self):
        return [self.tools[tid] for tid in self.order]

    def summaries(self):
        return [tool.summary() for tool in self.all()]


registry = Registry(DATA_DIR, TOOL_CLASSES)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "my-adb-app"

    def log_message(self, fmt, *args):
        pass                          # 别把每条静态请求都刷到控制台

    # ---------------- 响应工具 ----------------

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _serve_static(self, rel):
        safe = os.path.normpath(rel).lstrip("/\\")
        if safe.startswith("..") or os.path.isabs(safe):
            return self._send_text("400 Bad Request", 400)
        full = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(full):
            return self._send_text("404 Not Found", 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as fp:
            body = fp.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---------------- 路由 ----------------

    def do_GET(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")

        if parts[:1] == ["static"]:
            return self._serve_static("/".join(parts[1:]))

        # /always-adb -> static/always-adb.html，二级页面的短路由
        if len(parts) == 1 and parts[0] != "api":
            return self._serve_static(parts[0] + ".html")

        if parts[:1] != ["api"]:
            return self._send_text("404 Not Found", 404)

        if len(parts) == 2 and parts[1] == "tools":
            return self._send_json({"tools": registry.summaries()})

        if len(parts) == 3:
            tool = registry.get(parts[1])
            if tool is None:
                return self._send_json({"error": "没有这个工具: %s" % parts[1]}, 404)
            action = parts[2]
            if action == "state":
                return self._send_json(tool.snapshot(include_history=True))
            if action == "events":
                return self._sse(tool)
            if action == "devices" and hasattr(tool, "refresh_devices"):
                devices, err = tool.refresh_devices()
                tool.publish_state()
                return self._send_json({"devices": devices, "error": err})

        return self._send_text("404 Not Found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        if parts[:1] != ["api"] or len(parts) < 3:
            return self._send_text("404 Not Found", 404)

        tool_id, action = parts[1], "/".join(parts[2:])
        tool = registry.get(tool_id)
        if tool is None:
            return self._send_json({"error": "没有这个工具: %s" % tool_id}, 404)

        body = self._read_json()

        if action == "start":
            ok, msg = tool.start(body)
            return self._send_json(
                {"ok": ok, "message": msg, "state": tool.snapshot()},
                200 if ok else 409)

        if action == "stop":
            ok, msg = tool.stop()
            return self._send_json(
                {"ok": ok, "message": msg, "state": tool.snapshot()},
                200 if ok else 409)

        if action == "config":
            tool.config = tool.sanitize_config(body, base=tool.config)
            try:
                tool.save_config()
                saved = True
            except (IOError, OSError) as exc:
                saved = False
                tool.log("warn", "配置写入失败: %s" % exc)
            tool.log("info", "配置已更新")
            tool.publish_state()
            return self._send_json({"ok": True, "saved": saved, "config": tool.config})

        if action == "logs/clear":
            with tool.lock:
                tool.history.clear()
            return self._send_json({"ok": True})

        if action == "preview" and hasattr(tool, "preview"):
            result = tool.preview(body)
            if result is None:
                return self._send_json({"error": "这个工具不支持预览"}, 404)
            return self._send_json(result)

        return self._send_text("404 Not Found", 404)

    # ---------------- SSE ----------------

    def _sse(self, tool):
        q = tool.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_event("snapshot", tool.snapshot(include_history=True))
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    # 心跳，防中间设备掐掉空闲连接
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_event(item["type"], item["data"])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            tool.unsubscribe(q)

    def _sse_event(self, name, data):
        payload = "event: %s\ndata: %s\n\n" % (name, json.dumps(data, ensure_ascii=False))
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

class AppServer(object):
    """把 HTTP 服务包成一个能嵌进桌面程序的东西：后台线程跑、能查端口、能停。

    端口传 0 就让系统挑一个空闲的 —— 桌面版走的就是这条路，
    这样它和手动跑的 `server.py --port 8765` 永远不会撞车。
    """

    def __init__(self, host="127.0.0.1", port=0):
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.host, self.port = self.httpd.server_address[:2]
        self.thread = None

    @property
    def url(self):
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return "http://%s:%d/" % (host, self.port)

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="http")
        self.thread.daemon = True
        self.thread.start()
        return self

    def stop(self):
        for tool in registry.all():
            try:
                if tool.is_alive():
                    tool.stop()               # 顺带杀掉子进程（比如 ssh）
                else:
                    tool.stop_event.set()
            except Exception:
                pass
        # 只有真的 start() 过才能调 shutdown()：BaseServer.shutdown() 会等
        # serve_forever() 结束时才 set 的那个事件，serve_forever() 压根没跑过
        # 的话就是死等。所以拿 self.thread 当开关。
        if self.thread is not None:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
        try:
            self.httpd.server_close()
        except Exception:
            pass


def local_addresses():
    """列出本机在局域网里的 IPv4 地址，供 --host 0.0.0.0 时提示用。"""
    found = set()
    try:
        # 不真的发包，只是让内核按路由表挑一块出口网卡
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            found.add(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(addr for addr in found if not addr.startswith("127."))


def main():
    parser = argparse.ArgumentParser(description="my-adb-app：adb 小工具集合")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765,
                        help="监听端口（默认 8765；0 = 让系统随便挑一个空闲的）")
    args = parser.parse_args()

    server = AppServer(args.host, args.port)

    exposing = args.host not in ("127.0.0.1", "localhost")
    print("my-adb-app 已启动")
    for tool in registry.all():
        print("  - %-14s %s  (config/%s.json)" % (tool.name, tool.desc, tool.id))
    print()
    print("  本机打开:  %s" % server.url)
    if exposing:
        addrs = local_addresses()
        if addrs:
            print("  局域网内:  %s"
                  % "  ".join("http://%s:%d/" % (a, server.port) for a in addrs))
        else:
            print("  局域网内:  用本机的局域网 IP（Windows 上 ipconfig 查 IPv4 地址）")
        print()
        print("  ⚠ 已对外监听，且本服务没有鉴权：同网段的人都能通过这个页面")
        print("    跑 adb root、开 ssh 隧道。只在可信网络里这么做。")
    print()
    sys.stdout.flush()

    server.start()
    try:
        while server.thread.is_alive():
            server.thread.join(1.0)
    except KeyboardInterrupt:
        sys.stderr.write("\n正在退出...\n")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
