# -*- coding: utf-8 -*-
"""长驻监听 adb 设备上下线，替代页面上的定时轮询。

思路很直接：没有在线设备时阻塞在 `adb wait-for-device`，有在线设备时阻塞在
`adb -s <序列号> wait-for-disconnect`。这两条命令本身不耗 CPU，事件发生才返回，
所以平时没有轮询开销。每次返回后查一次设备列表，只有列表真的变了才回调。

注意：`wait-for-device` 在设备已经在线时会立刻返回，所以不能无条件循环调用它
（会空转）。必须先用 `adb devices` 看清当前状态，再决定等上线还是等掉线。
"""

import subprocess
import threading
import time

import adbkit
import spawn

# adb 出错后的退避上限。正常路径不会走到这里。
MAX_BACKOFF = 30.0


class DeviceWatcher(object):
    """在后台线程里等设备上线/掉线，变化时回调 on_devices。

    get_config() 每次循环都会重新调用，所以配置（adb 路径、序列号）改了
    kick() 一下就能生效，不用重建对象。
    """

    def __init__(self, get_config, on_devices):
        self._get_config = get_config
        self._on_devices = on_devices
        self._stop_event = threading.Event()
        self._kick_event = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc = None
        self._thread = None
        self._last_key = None

    # ---------------- 生命周期 ----------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="device-watch", daemon=True)
        self._thread.start()

    def kick(self):
        """配置变了：杀掉当前等待，下一轮用新配置重来。"""
        self._kick_event.set()
        self._terminate_proc()

    def stop(self):
        self._stop_event.set()
        self._kick_event.set()
        self._terminate_proc()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    # ---------------- 主循环 ----------------

    def _run(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            self._kick_event.clear()
            config = self._get_config() or {}
            adb_path = config.get("adbPath") or "adb"
            serial = config.get("serial") or ""

            # 先看清当前状态，决定这一轮该等什么
            devices, error = adbkit.list_devices(adb_path, serial, 10.0)
            if self._stop_event.is_set() or self._kick_event.is_set():
                continue
            if not error:
                self._publish(devices)

            online = [d for d in devices if d.get("state") == "device"]
            wanted_online = bool(serial) and any(
                d.get("serial") == serial for d in online)
            if serial and not wanted_online:
                # 指定了序列号但它还没上线：优先等它出现。
                # （这时别的设备在线也先不管，页面的低频轮询会兜底。）
                args = ["-s", serial, "wait-for-device"]
            elif online:
                # 有设备在线：等它掉线，好为下一次上线重新布防
                target = serial if wanted_online else online[0]["serial"]
                args = ["-s", target, "wait-for-disconnect"]
            else:
                # 没指定序列号、也没有在线设备：等任意一台
                args = ["wait-for-device"]

            proc = self._spawn(adb_path, args)
            if proc is None:
                if self._sleep(backoff):
                    break
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            code = self._wait(proc)
            if self._stop_event.is_set():
                break
            if self._kick_event.is_set():
                continue

            if code != 0:
                # adb server 还没起来、或者这条 wait 报错：退避，别空转
                if self._sleep(backoff):
                    break
                backoff = min(backoff * 2, MAX_BACKOFF)
            else:
                backoff = 1.0
                # 刚发生事件，给 adb server 一点时间把列表更新完
                if self._sleep(0.2):
                    break

        self._terminate_proc()

    # ---------------- 子进程 ----------------

    def _spawn(self, adb_path, args):
        cmd = [adb_path] + list(args)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **spawn.quiet()
            )
        except (OSError, ValueError):
            return None

        with self._proc_lock:
            self._proc = proc
            kicked = self._stop_event.is_set() or self._kick_event.is_set()
        if kicked:
            self._terminate_proc()
        return proc

    def _wait(self, proc):
        """等子进程退出；stop/kick 时杀掉它并返回 None。"""
        while True:
            try:
                return proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if self._stop_event.is_set() or self._kick_event.is_set():
                    self._terminate_proc()
                    return None
            except OSError:
                return None

    def _terminate_proc(self):
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                return
            proc.terminate()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _sleep(self, seconds):
        """可被 stop/kick 打断的 sleep。返回 True 表示收到了 stop。"""
        deadline = time.time() + seconds
        while not self._stop_event.is_set():
            if self._kick_event.is_set():
                return False
            left = deadline - time.time()
            if left <= 0:
                return False
            self._stop_event.wait(min(0.2, left))
        return True

    # ---------------- 回调 ----------------

    def _publish(self, devices):
        key = tuple(
            (d.get("serial", ""), d.get("state", ""), d.get("model", ""),
             d.get("product", ""), d.get("transport", ""))
            for d in devices
        )
        if key == self._last_key:
            return
        self._last_key = key
        try:
            self._on_devices(list(devices))
        except Exception:
            # 回调是别人给的，出错不能把监听线程打死
            pass
