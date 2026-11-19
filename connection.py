# -*- coding: utf-8 -*-
"""全局连接方式：ADB 或 SSH。

模式与 SSH 参数存在 <数据目录>/config/connection.json。所有工具都从这里
取当前模式，切一次就全部跟着走；这里不做任何 adb / ssh 调用。
"""

import copy
import json
import os
import threading
import time

import apppaths
import sshkit

MODE_ADB = "adb"
MODE_SSH = "ssh"
MODES = (MODE_ADB, MODE_SSH)

HOST_KEY_POLICIES = ("accept-new", "yes", "no")
DEFAULT_REMOUNT_COMMAND = "mount -o remount,rw /"

DEFAULT_SSH = {
    "host": "",
    "port": 22,
    "user": "",
    "keyFile": "",
    "hostKeyPolicy": "accept-new",
    "connectTimeout": 10,
    "remountCommand": DEFAULT_REMOUNT_COMMAND,
}

_SSH_KEYS = tuple(DEFAULT_SSH)


class ConfigError(ValueError):
    """连接参数不合法，服务端把它翻译成 400。"""


def default_config():
    return {"mode": MODE_ADB, "ssh": copy.deepcopy(DEFAULT_SSH)}


def _as_text(value):
    if value is None:
        return ""
    return str(value).strip()


class Connection(object):
    def __init__(self, app_dir):
        self.app_dir = app_dir
        self.path = os.path.join(app_dir, "config", "connection.json")
        self._lock = threading.RLock()
        self._data = self.load()

    # ---------------- 读写 ----------------

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                saved = json.load(fp)
        except (IOError, OSError, ValueError):
            return default_config()
        try:
            return self.validate(saved, base=default_config())
        except ConfigError:
            return default_config()

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def mode(self):
        with self._lock:
            return self._data["mode"]

    def ssh(self):
        with self._lock:
            return copy.deepcopy(self._data["ssh"])

    def validate(self, raw, base=None):
        """把一份（可能不完整的）配置合并、校验成完整配置。"""
        base = copy.deepcopy(base) if isinstance(base, dict) else default_config()
        raw = raw if isinstance(raw, dict) else {}

        base_ssh = base.get("ssh")
        if not isinstance(base_ssh, dict):
            base_ssh = copy.deepcopy(DEFAULT_SSH)

        raw_ssh = raw.get("ssh")
        if raw_ssh is None:
            raw_ssh = {}
        elif not isinstance(raw_ssh, dict):
            raise ConfigError("SSH 参数格式不对")

        mode = raw.get("mode", base.get("mode", MODE_ADB))
        if mode not in MODES:
            raise ConfigError("连接方式只能是 adb 或 ssh")

        def pick(key, fallback):
            if key in raw_ssh:
                return raw_ssh[key]
            return base_ssh.get(key, fallback)

        host = _as_text(pick("host", ""))
        user = _as_text(pick("user", ""))
        key_file = _as_text(pick("keyFile", ""))

        raw_port = pick("port", 22)
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            raise ConfigError("SSH 端口必须在 1-65535 之间")
        if port < 1 or port > 65535:
            raise ConfigError("SSH 端口必须在 1-65535 之间")

        policy = pick("hostKeyPolicy", "accept-new")
        if policy not in HOST_KEY_POLICIES:
            policy = "accept-new"

        try:
            timeout = int(pick("connectTimeout", 10))
        except (TypeError, ValueError):
            timeout = 10
        timeout = max(1, min(60, timeout))

        remount = _as_text(pick("remountCommand", DEFAULT_REMOUNT_COMMAND))
        if not remount:
            remount = DEFAULT_REMOUNT_COMMAND

        if mode == MODE_SSH:
            if not host:
                raise ConfigError("SSH 模式必须填写地址")
            if not user:
                raise ConfigError("SSH 模式必须填写用户名")

        return {
            "mode": mode,
            "ssh": {
                "host": host,
                "port": port,
                "user": user,
                "keyFile": key_file,
                "hostKeyPolicy": policy,
                "connectTimeout": timeout,
                "remountCommand": remount,
            },
        }

    def save(self, raw):
        with self._lock:
            merged = self.validate(raw, base=self._data)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(merged, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
            os.replace(tmp, self.path)
            self._data = merged
            return copy.deepcopy(merged)

    def info(self):
        with self._lock:
            data = copy.deepcopy(self._data)
        ssh = data["ssh"]
        if data["mode"] == MODE_SSH:
            display = "SSH %s@%s:%d" % (ssh["user"], ssh["host"], ssh["port"])
        else:
            display = "ADB"
        info = {
            "mode": data["mode"],
            "modeName": "SSH" if data["mode"] == MODE_SSH else "ADB",
            "ssh": ssh,
            "display": display,
            "online": False,
            "error": "",
            "model": "",
            "deviceCount": 0,
        }
        # 只读进程级监听器的缓存，不拿工具锁 —— 监听线程里也会调到这里
        if data["mode"] == MODE_SSH:
            mon = peek_monitor()
            if mon is not None:
                status = mon.status()
                info["online"] = status["online"]
                info["error"] = status["error"]
                info["model"] = status["model"]
                info["deviceCount"] = status["deviceCount"]
        return info


class ConnectionMonitor(object):
    """进程级 SSH 探测：开发板只有一块，所有工具共用一份在线状态。

    每 interval 秒探测一次；结果只在变化时推给注册的工具，避免每 10 秒
    让所有页面重绘一遍。
    """

    def __init__(self, connection, interval=10.0):
        self.connection = connection
        self.interval = float(interval)
        self._lock = threading.RLock()
        self._tools = []
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._devices = []
        self._error = ""
        self._checked_at = None
        self._last_key = None

    # ---------------- 生命周期 ----------------

    def register(self, tool):
        with self._lock:
            if tool not in self._tools:
                self._tools.append(tool)
        self.start()

    def unregister(self, tool):
        with self._lock:
            if tool in self._tools:
                self._tools.remove(tool)

    def is_running(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self):
        # ADB 模式由各工具的 adb wait-for-device 监听，这里不该有线程
        if self.connection.mode() != MODE_SSH:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._run, name="ssh-monitor")
            self._thread.daemon = True
            self._thread.start()

    def stop(self):
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        self._wake.set()
        if thread is not None:
            thread.join(2)

    def kick(self):
        """马上探测一次（保存了新的连接参数时用）。"""
        self._wake.set()

    def reset(self):
        self.stop()
        with self._lock:
            self._tools = []
            self._devices = []
            self._error = ""
            self._checked_at = None
            self._last_key = None

    # ---------------- 探测 ----------------

    def _run(self):
        while not self._stop.is_set():
            self.check()
            self._wake.wait(self.interval)
            self._wake.clear()

    def check(self):
        devices, error = sshkit.probe(self.connection.ssh())
        if error:
            devices = []
        key = (tuple((d.get("serial", ""), d.get("state", ""), d.get("model", ""))
                     for d in devices), error)
        with self._lock:
            self._devices = list(devices)
            self._error = error
            self._checked_at = time.time()
            changed = key != self._last_key
            self._last_key = key
            tools = list(self._tools)
        if changed:
            self._push(tools, devices, error)
        return self.status()

    def _push(self, tools, devices, error):
        for tool in tools:
            try:
                tool.apply_devices(devices, error)
                tool.publish_state()
            except Exception:
                # 单个工具的状态推送失败不该拖垮监听线程
                pass

    def devices(self):
        with self._lock:
            return list(self._devices), self._error

    def status(self):
        with self._lock:
            devices = list(self._devices)
            error = self._error
            checked_at = self._checked_at
        return {
            "online": bool(devices) and not error,
            "error": error,
            "model": devices[0].get("model", "") if devices else "",
            "deviceCount": len(devices),
            "checkedAt": checked_at,
        }


# --------------------------------------------------------------------------
# 进程内单例
# --------------------------------------------------------------------------

_lock = threading.RLock()
_connection = None
_monitor = None


def current(app_dir=None):
    global _connection
    with _lock:
        if _connection is None:
            _connection = Connection(app_dir or apppaths.data_dir())
        return _connection


def monitor():
    """取（必要时创建）进程级 SSH 监听器。"""
    global _monitor
    with _lock:
        conn = current()
        if _monitor is None:
            _monitor = ConnectionMonitor(conn)
        return _monitor


def peek_monitor():
    """只读单例，不存在时返回 None —— 给 info() 这类不能顺手起线程的地方用。"""
    with _lock:
        return _monitor


def reload(app_dir=None):
    global _connection, _monitor
    new_conn = Connection(app_dir or apppaths.data_dir())
    with _lock:
        # 先装新 Connection 再摘监听器：reset() 要 join 线程、耗时可能较长，
        # 期间并发的 monitor() 若看到 _connection 还是旧的，会新建并缓存一个
        # 绑着旧配置的监听器（reset() 不会改绑），正好是 reload 要避免的状态。
        _connection = new_conn
        old = _monitor
        _monitor = None
    # reset() 会 join 监听线程，而线程里的 publish_state 可能回头取 _lock，
    # 所以必须在锁外 reset
    if old is not None:
        old.reset()
    return new_conn
