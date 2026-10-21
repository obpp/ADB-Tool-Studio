# -*- coding: utf-8 -*-
"""my-adb-app 的工具基类。

每个功能都是一个 Tool 子类，各自带：
  * 一份配置   config/<id>.json
  * 一条日志流（订阅者队列 + 环形历史），供 SSE 推送
  * 一个后台线程，以及 start() / stop()

一级页面只读 summary()；二级页面用 snapshot() + SSE 拿实时状态和日志。
加新功能 = 写一个子类 + 在 server.py 的 TOOLS 里登记一行。
"""

import json
import os
import queue
import threading
import time
from collections import deque

LEVELS = ("cmd", "info", "ok", "warn", "err")

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_RECONNECTING = "reconnecting"


# --------------------------------------------------------------------------
# 配置里每个字段怎么清洗 —— 页面传上来的东西不可全信
# --------------------------------------------------------------------------

def as_str(raw, key, default, nonempty=False):
    value = raw.get(key, default)
    value = "" if value is None else str(value).strip()
    if nonempty and not value:
        return default
    return value


def as_num(raw, key, default, low, high):
    try:
        value = float(raw[key])
    except (KeyError, TypeError, ValueError):
        return default
    return min(high, max(low, value))


def as_int(raw, key, default, low, high):
    try:
        value = int(float(raw[key]))
    except (KeyError, TypeError, ValueError):
        return default
    return min(high, max(low, value))


def as_bool(raw, key, default):
    try:
        return bool(raw[key])
    except KeyError:
        return default


def as_enum(raw, key, default, allowed):
    value = str(raw.get(key, default))
    return value if value in allowed else default


def shell_join(argv):
    """把参数拼成一条可以直接粘进终端的命令（POSIX 风格引号）。"""
    out = []
    for token in argv:
        if token == "" or any(ch in token for ch in " \t\"'\\$&|;<>()*?[]{}`!#~"):
            out.append("'" + token.replace("'", "'\\''") + "'")
        else:
            out.append(token)
    return " ".join(out)


# --------------------------------------------------------------------------

class Tool(object):

    id = ""
    name = ""
    desc = ""
    page = ""      # 二级页面路由，如 "always-adb" -> /always-adb
    icon = "•"     # 一级页面卡片上的字形

    def __init__(self, app_dir):
        self.app_dir = app_dir
        self.lock = threading.RLock()
        self.subscribers = []
        self.history = deque(maxlen=500)
        self.seq = 0
        self.status = STATUS_IDLE
        self.started_at = None
        self.stop_event = threading.Event()
        self.thread = None
        self.process = None          # 子类跑外部进程时放这里，便于 stop 时收拾
        self.start_options = {}      # start() 时页面传来的选项（如 {"once": true}）
        self.config = self.load_config()
        self.stats = self.default_stats()

    # ---------------- 子类要实现/可覆盖的 ----------------

    def default_config(self):
        return {}

    def default_stats(self):
        return {}

    def sanitize_config(self, raw, base=None):
        """把页面提交的内容并进 base，做白名单 + 范围夹紧。"""
        return dict(base) if isinstance(base, dict) else self.default_config()

    def run_loop(self):
        """后台线程主体，返回即视为任务结束。"""
        raise NotImplementedError

    def on_stop(self):
        """stop() 里被调用，子类可以借此杀掉自己的子进程。"""

    def extra(self):
        """给前端的补充状态，不分页也要能序列化。"""
        return {}

    def brief(self):
        """一级页面卡片上那一行小字。"""
        return ""

    def preview(self, query):
        """给 GET /api/<id>/command 用：把一组参数变成一条命令行预览。"""
        return None

    # ---------------- 配置持久化 ----------------

    def config_path(self):
        return os.path.join(self.app_dir, "config", self.id + ".json")

    def load_config(self):
        cfg = self.default_config()
        try:
            with open(self.config_path(), "r", encoding="utf-8") as fp:
                saved = json.load(fp)
        except (IOError, OSError, ValueError):
            return cfg
        return self.sanitize_config(saved, base=cfg)

    def save_config(self):
        path = self.config_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(self.config, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ---------------- 广播 ----------------

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _broadcast(self, kind, data):
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            item = {"type": kind, "data": data}
            try:
                q.put_nowait(item)
            except queue.Full:
                # 客户端太慢：丢最旧的一条，保证它还能收到最新状态
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except (queue.Empty, queue.Full):
                    pass

    def log(self, level, message):
        with self.lock:
            self.seq += 1
            entry = {
                "seq": self.seq,
                "ts": time.time(),
                "time": time.strftime("%H:%M:%S", time.localtime()),
                "level": level if level in LEVELS else "info",
                "msg": message,
            }
            self.history.append(entry)
        self._broadcast("log", entry)
        return entry

    def publish_state(self):
        self._broadcast("state", self.snapshot())

    # ---------------- 状态快照 ----------------

    def _uptime(self):
        if self.started_at and self.status != STATUS_IDLE:
            return time.time() - self.started_at
        return 0.0

    def snapshot(self, include_history=False):
        with self.lock:
            snap = {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "startedAt": self.started_at,
                "uptime": self._uptime(),
                "stats": dict(self.stats),
                "config": dict(self.config),
                "extra": self.extra(),
                "serverTime": time.time(),
            }
            if include_history:
                snap["history"] = list(self.history)
        return snap

    def summary(self):
        """侧边栏 / 工具列表用的概要数据（不含历史、不摊开配置）。"""
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "desc": self.desc,
                "page": self.page,
                "icon": self.icon,
                "status": self.status,
                "startedAt": self.started_at,
                "uptime": self._uptime(),
                "brief": self.brief(),
                "stats": dict(self.stats),
                "extra": self.extra(),
            }

    # ---------------- 启停 ----------------

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, options=None):
        with self.lock:
            if self.is_alive():
                return False, "任务已在运行中"
            self.start_options = dict(options) if isinstance(options, dict) else {}
            self.stop_event.clear()
            self.status = STATUS_RUNNING
            self.started_at = time.time()
        self.log("info", "=== %s 启动 ===" % self.name)
        with self.lock:
            self.thread = threading.Thread(target=self._worker, name=self.id)
            self.thread.daemon = True
            self.thread.start()
        self.publish_state()
        return True, "已启动"

    def stop(self):
        with self.lock:
            if not self.is_alive():
                return False, "当前没有在运行的任务"
            self.status = STATUS_STOPPING
        self.log("warn", "收到停止请求，正在结束...")
        self.on_stop()
        self.stop_event.set()
        self.publish_state()
        return True, "已请求停止"

    def _worker(self):
        try:
            self.run_loop()
        except Exception as exc:          # 后台线程的异常不能静默吞掉
            self.log("err", "内部错误: %r" % (exc,))
        finally:
            with self.lock:
                self.status = STATUS_IDLE
            self.log("info", "=== 任务结束 ===")
            self.publish_state()

    # ---------------- 供子类使用的小工具 ----------------

    def set_status(self, status):
        with self.lock:
            self.status = status
        self.publish_state()

    def bump(self, key, delta=1):
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + delta

    def set_stat(self, key, value):
        with self.lock:
            self.stats[key] = value

    def interruptible_sleep(self, seconds):
        """返回 True 表示期间收到了停止请求。"""
        if seconds <= 0:
            return self.stop_event.is_set()
        return self.stop_event.wait(seconds)
