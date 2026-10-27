#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ADB Tool Studio —— adb 相关小工具的集合。

所有功能共用一套外壳：左边侧边栏切换功能，右边是当前功能的控制台。
/ 直接进第一个功能，没有单独的总览页。

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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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
# 页面存活探针 / 前端错误上报
# --------------------------------------------------------------------------
# 页面每 10 秒发一次心跳，JS 报错也会 POST 回来。桌面版把 sink 接到
# desktop/app.py 的 log()，于是这些都落进 app.log —— 白屏时页面本身
# 什么都显示不了，但至少能知道「最后一次心跳是什么时候、之前报了什么错」。
# 网页版没有设置 sink，就退回 stderr（python3 server.py 的控制台）。

_client_log_sink = None
_ui_lock = threading.Lock()
# 页面 id -> (最后心跳 time.time(), 路径, 是否可见)。id 存在 sessionStorage 里，
# 所以同一个标签页里换页不会变成新页面；多开标签页则各算各的。
_ui_clients = {}
_UI_CLIENT_TTL = 3600
_UI_CLIENT_MAX = 200


def set_client_log_sink(fn):
    global _client_log_sink
    _client_log_sink = fn


def client_log(message):
    sink = _client_log_sink
    if sink is not None:
        try:
            sink(message)
            return
        except Exception:
            pass
    if sys.stderr:
        try:
            sys.stderr.write("[页面] %s\n" % message)
            sys.stderr.flush()
        except Exception:
            pass


def _prune_ui_clients(now):
    """清掉很久没心跳的页面。调用方必须已经持有 _ui_lock。"""
    for cid, (beat, _page, _visible) in list(_ui_clients.items()):
        if now - beat > _UI_CLIENT_TTL:
            _ui_clients.pop(cid, None)


def ui_beat(client_id, page, visible):
    """记一次心跳。返回 None / ('connect', 页面) / ('page', 旧页, 新页)。"""
    # 这些都是页面传上来的，压成一行再进日志，免得伪造出额外的日志行
    client_id = (client_id or "anonymous").replace("\r", " ").replace("\n", " ")[:64]
    page = (page or "/").replace("\r", " ").replace("\n", " ")[:200]
    now = time.time()
    event = None
    with _ui_lock:
        old = _ui_clients.get(client_id)
        if old is None:
            if len(_ui_clients) >= _UI_CLIENT_MAX:
                oldest = min(_ui_clients.items(), key=lambda kv: kv[1][0])[0]
                _ui_clients.pop(oldest, None)
            event = ("connect", page)
        elif old[1] != page:
            event = ("page", old[1], page)
        _ui_clients[client_id] = (now, page, bool(visible))
        _prune_ui_clients(now)
    return event


def ui_latest():
    """最近还有心跳的那个页面，供看门狗判断「界面是不是还活着」。"""
    now = time.time()
    with _ui_lock:
        _prune_ui_clients(now)
        if not _ui_clients:
            return None
        cid, (beat, page, visible) = max(
            _ui_clients.items(), key=lambda kv: kv[1][0])
        return {"id": cid, "beat": beat, "page": page, "visible": visible}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    server_version = "ADB-Tool-Studio"

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

    # 请求体上限。脚本导入是唯一需要大 body 的地方，其余接口都是几百字节。
    MAX_BODY = 2 * 1024 * 1024 + 4096

    def _read_body(self):
        """把请求体整个读成 bytes。太大返回 None。

        两个都不能省的细节：
          * HTTP/1.1 keep-alive 下必须把 Content-Length 声明的字节读干净，
            不然后续字节会被当成下一个请求的请求行，整条连接就错乱了。
          * 超限时没法读完，那就把连接标成用完即关，避免残留污染下一条请求。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > self.MAX_BODY:
            self.close_connection = True
            return None
        return self.rfile.read(length)

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

        # 首页 = 第一个注册的工具。没有总览页了，打开就直接进一个通道，
        # 免得先看一眼空目录再点一下。
        if path in ("/", "/index.html"):
            tools = registry.all()
            if not tools:
                return self._send_text("没有注册任何工具", 500)
            return self._serve_static(tools[0].page + ".html")

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
                # 页面第一次要设备列表时顺手把长驻监听拉起来，之后设备
                # 上线/掉线会通过 SSE 推过去，不用等下一次轮询。
                ensure = getattr(tool, "ensure_device_watch", None)
                if ensure is not None:
                    ensure()
                devices, err = tool.refresh_devices()
                tool.publish_state()
                return self._send_json({"devices": devices, "error": err})

        return self._send_text("404 Not Found", 404)

    # ---------------- 页面探针 ----------------

    MAX_CLIENT_TEXT = 1000

    def _client_endpoint(self, action):
        raw = self._read_body()
        if raw is None:
            return self._send_json({"ok": False, "error": "请求体太大"}, 413)

        data = {}
        if raw:
            try:
                decoded = json.loads(raw.decode("utf-8"))
                if isinstance(decoded, dict):
                    data = decoded
            except (ValueError, UnicodeDecodeError):
                pass

        if action == "heartbeat":
            client_id = str(data.get("id") or "anonymous")
            page = str(data.get("page") or "/")
            visible = data.get("visible", True) is not False
            event = ui_beat(client_id, page, visible)
            if event and event[0] == "connect":
                client_log("页面已连接: %s" % event[1])
            elif event:
                client_log("页面切换: %s → %s" % (event[1], event[2]))
            return self._send_json({"ok": True})

        if action == "log":
            text = str(data.get("message") or "")
            # app.log 是按行读的，堆栈里的换行压成一行，免得一条记录拆成几十行
            text = text.replace("\r", " ").replace("\n", " | ").strip()
            if not text:
                return self._send_json({"ok": True})
            if len(text) > self.MAX_CLIENT_TEXT:
                text = text[:self.MAX_CLIENT_TEXT] + " …"
            level = str(data.get("level") or "error")[:16]
            page = (str(data.get("page") or "")
                    .replace("\r", " ").replace("\n", " ")[:200])
            client_log("页面报错[%s]%s: %s" % (
                level, (" " + page) if page else "", text))
            return self._send_json({"ok": True})

        return self._send_text("404 Not Found", 404)

    def do_POST(self):
        parsed_url = urlparse(self.path)
        parts = [p for p in parsed_url.path.split("/") if p]

        # 页面自身的探针，不属于任何工具，所以要在按 tool_id 分发之前接走。
        if parts[:2] == ["api", "client"] and len(parts) == 3:
            return self._client_endpoint(parts[2])

        if parts[:1] != ["api"] or len(parts) < 3:
            self.close_connection = True      # body 没读，这条连接不能再复用
            return self._send_text("404 Not Found", 404)

        tool_id, action = parts[1], "/".join(parts[2:])
        tool = registry.get(tool_id)
        if tool is None:
            self.close_connection = True
            return self._send_json({"error": "没有这个工具: %s" % tool_id}, 404)

        query = {k: v[0] for k, v in parse_qs(parsed_url.query).items() if v}

        # 文件上传是流式的，必须赶在 _read_body() 之前接走：那里会按 2 MB
        # 上限把整个 body 读进内存，而这里可能是几个 GB。工具自己边读边写盘。
        uploader = getattr(tool, "upload", None)
        if action == "files/upload" and uploader is not None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self.close_connection = True
                return self._send_json(
                    {"ok": False, "error": "缺少 Content-Length"}, 411)
            # 中途放弃（比如超过上限直接拒绝）时 body 没读完，
            # keep-alive 会把剩下的字节当成下一个请求，所以用完即关。
            self.close_connection = True
            payload, status = uploader(self.rfile, length, query)
            return self._send_json(payload, status)

        raw = self._read_body()
        if raw is None:
            return self._send_json({"ok": False, "error": "请求体太大"}, 413)

        # 大多数接口是 JSON；脚本导入直接传原始字节，这里解析失败就当没有
        data = {}
        if raw:
            try:
                decoded = json.loads(raw.decode("utf-8"))
                if isinstance(decoded, dict):
                    data = decoded
            except (ValueError, UnicodeDecodeError):
                pass

        if action == "start":
            ok, msg = tool.start(data)
            return self._send_json(
                {"ok": ok, "message": msg, "state": tool.snapshot()},
                200 if ok else 409)

        if action == "stop":
            ok, msg = tool.stop()
            return self._send_json(
                {"ok": ok, "message": msg, "state": tool.snapshot()},
                200 if ok else 409)

        if action == "config":
            tool.config = tool.sanitize_config(data, base=tool.config)
            try:
                tool.save_config()
                saved = True
            except (IOError, OSError) as exc:
                saved = False
                tool.log("warn", "配置写入失败: %s" % exc)
            tool.log("info", "配置已更新")
            kick = getattr(tool, "kick_device_watch", None)
            if kick is not None:
                kick()
            tool.publish_state()
            return self._send_json({"ok": True, "saved": saved, "config": tool.config})

        if action == "logs/clear":
            with tool.lock:
                tool.history.clear()
            return self._send_json({"ok": True})

        if action == "preview" and hasattr(tool, "preview"):
            result = tool.preview(data)
            if result is None:
                return self._send_json({"error": "这个工具不支持预览"}, 404)
            return self._send_json(result)

        # 工具自己的动作（导入脚本之类）。返回 None 表示它也不认识这个 action。
        # 这样加功能不用再回来改这里。
        handler = getattr(tool, "api", None)
        if handler is not None:
            result = handler(action, raw, data, query)
            if result is not None:
                payload, status = result
                return self._send_json(payload, status)

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
                stop_watch = getattr(tool, "stop_device_watch", None)
                if stop_watch is not None:
                    stop_watch()              # 收拾长驻的 adb wait-for-device
                if tool.is_alive():
                    tool.stop()               # 顺带杀掉子进程（比如 ssh）
                else:
                    tool.stop_event.set()
                cleanup = getattr(tool, "cleanup", None)
                if cleanup is not None:
                    cleanup()                 # 删掉临时文件（推送文件的暂存）
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
    parser = argparse.ArgumentParser(description="ADB Tool Studio：adb 小工具集合")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765,
                        help="监听端口（默认 8765；0 = 让系统随便挑一个空闲的）")
    args = parser.parse_args()

    server = AppServer(args.host, args.port)

    exposing = args.host not in ("127.0.0.1", "localhost")
    print("ADB Tool Studio 已启动")
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
