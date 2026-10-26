# -*- coding: utf-8 -*-
"""推送文件：把本机文件用 adb push 推到设备上的指定目录。

和替换镜像的区别：不写块设备、不重启、不保存预设，只是搬文件。

文件先传进本机的临时目录再 push —— 浏览器（包括桌面版的 WebView）不会把
本机完整路径交给页面，拖拽拿到的也只是 File 对象，所以「先传到后端再推」
是网页版和桌面版都成立的做法。上传是流式写盘，不把整个文件读进内存。

几个要知道的点：

  * 设备上同名文件会被覆盖，这是 adb push 本来的行为，日志里会写最终路径。
  * 推送目录必须是 / 开头的绝对路径；目录选择器只列目录，不列文件。
  * 推送超时按文件大小放宽：至少 2 分钟，每 1 MB 再多给 1 秒，上限 2 小时。
  * 停止只能打断「正在推的那一个」；已经推完的文件不回滚。
"""

import os
import re
import shutil
import subprocess
import tempfile
import time

import adbkit
import spawn
from toolbase import Tool, as_str, shell_join, STATUS_IDLE

# 单个文件上限。再大就不该走「拖进浏览器再上传」这条路了。
MAX_FILE_BYTES = 4 * 1024 ** 3

# 列设备目录的 adb 调用超时 / 一次最多列多少项
LIST_TIMEOUT = 20.0
MAX_LIST_ENTRIES = 500

# 文件名里的不可见字符：控制字符、NUL
_BAD_NAME = re.compile(r"[\x00-\x1f]")


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    if n < 1024 * 1024 * 1024:
        return "%.1f MB" % (n / (1024.0 * 1024.0))
    return "%.2f GB" % (n / (1024.0 * 1024.0 * 1024.0))


def safe_file_name(name):
    """设备上用的文件名：去掉路径、不可见字符，太长就截断但保留扩展名。"""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _BAD_NAME.sub("_", base)
    if not base:
        base = "file"
    # 多数文件系统单个名字上限 255 字节，留点余量
    while len(base.encode("utf-8")) > 180:
        stem, ext = os.path.splitext(base)
        if stem and len(stem) > 1:
            base = stem[:-1] + ext
        else:
            base = base.encode("utf-8")[:180].decode("utf-8", "ignore") or "file"
    return base


def clean_remote_dir(value):
    """推送目录：必须是从 / 开始的绝对路径。返回 (路径, 错误说明)。"""
    path = str(value or "").strip()
    if not path:
        path = "/data/local/tmp"
    if not path.startswith("/"):
        return None, "推送目录必须是 / 开头的绝对路径"
    if any(ch in path for ch in "\r\n\x00"):
        return None, "推送目录里有换行或不可见字符"
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return path, ""


def parent_dir(path):
    """设备路径的上一级；根目录返回空串（没有上一级）。"""
    if not path or path == "/":
        return ""
    head = path.rstrip("/").rsplit("/", 1)[0]
    return head or "/"


def push_timeout(size):
    """一个大文件推多久算超时。按 1 MB/s 的慢速估，最少 2 分钟，最多 2 小时。"""
    return min(7200.0, max(120.0, (size or 0) / (1024.0 * 1024.0) + 60.0))


def _clean_output(text):
    """adb push 的进度是用 \\r 回写的：转成换行、丢掉空行和连续重复行。"""
    lines = [line.strip() for line in (text or "").replace("\r", "\n").splitlines()]
    out = []
    for line in lines:
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


def parse_ls(output):
    """从 ls -a -p -L 的输出里挑出目录：只看以 / 结尾的行。

    adb 的输出里可能混着 `ls: ...: Permission denied` 之类的报错，
    它们不以 / 结尾，正好被过滤掉。
    """
    dirs = []
    for line in (output or "").splitlines():
        name = line.strip()
        if not name.endswith("/"):
            continue
        name = name.rstrip("/")
        if not name or name in (".", "..") or "/" in name:
            continue
        dirs.append(name)
    dirs.sort(key=lambda item: (item.lower(), item))
    return dirs


class PushFileTool(Tool):

    id = "push_file"
    name = "推送文件"
    desc = "把本地文件推到设备上的指定目录，不写分区、不重启"
    page = "push-file"
    icon = "⇪"

    def __init__(self, app_dir):
        self.files = []            # 暂存清单，见 upload()
        self.devices = []
        self.devices_error = ""
        self._stage = None         # 临时目录，第一次上传时才建
        Tool.__init__(self, app_dir)

    # ---------------- 配置 ----------------

    def default_config(self):
        return {
            "adbPath": adbkit.resolve_adb(),
            "serial": "",
            # 普通文件推送不需要 root，/data/local/tmp 比替换镜像的 /mnt 更合适
            "pushDir": "/data/local/tmp",
        }

    def sanitize_config(self, raw, base=None):
        base = dict(base) if isinstance(base, dict) else self.default_config()
        raw = raw if isinstance(raw, dict) else {}

        push_dir, err = clean_remote_dir(as_str(raw, "pushDir", base["pushDir"]))
        if err:                       # 不合法就沿用原来那个，别把配置写坏
            push_dir = base.get("pushDir") or "/data/local/tmp"

        return {
            "adbPath": as_str(raw, "adbPath", base["adbPath"], nonempty=True),
            "serial": as_str(raw, "serial", base["serial"]),
            "pushDir": push_dir,
        }

    def default_stats(self):
        return {
            "runs": 0,
            "failures": 0,
            "consecutiveFailures": 0,
            "lastRunAt": None,
            "lastExitCode": None,
            "lastDuration": None,
            "deviceCount": 0,
            "filesPushed": 0,
            "filesFailed": 0,
        }

    # ---------------- 给前端的状态 ----------------

    def _public_files(self):
        return [{
            "id": item["id"],
            "name": item["name"],
            "size": item["size"],
            "addedAt": item["addedAt"],
        } for item in self.files]

    def extra(self):
        with self.lock:
            return {
                "files": self._public_files(),
                "devices": list(self.devices),
                "devicesError": self.devices_error,
            }

    def brief(self):
        stats = self.stats
        line = "%d 个文件待推送 · 已推送 %d 个" % (
            len(self.files), stats.get("filesPushed", 0))
        if stats.get("filesFailed"):
            line += " · 失败 %d 个" % stats["filesFailed"]
        return line

    # ---------------- 设备 ----------------

    def refresh_devices(self):
        cfg = self.config
        devices, err = adbkit.list_devices(cfg["adbPath"], cfg["serial"], 10.0)
        self.apply_devices(devices, err)
        return devices, err

    def _pick_serial(self, devices):
        return adbkit.pick_serial(devices, self.config["serial"], self.devices_error)

    # ---------------- 暂存 ----------------

    def _stage_dir(self):
        with self.lock:
            if self._stage is None:
                self._stage = tempfile.mkdtemp(prefix="ats-push-")
            return self._stage

    def upload(self, rfile, length, query):
        """流式接收一个文件。server.py 直接把 rfile 交进来，不走 _read_body。

        返回 (payload, status)，和 Tool.api() 的约定一致。
        """
        try:
            length = int(length)
        except (TypeError, ValueError):
            length = 0
        if length < 0:
            return {"ok": False, "error": "Content-Length 不合法"}, 400
        if length > MAX_FILE_BYTES:
            return {"ok": False, "error": "文件太大（上限 4 GB）"}, 413

        name = safe_file_name((query or {}).get("name") or "file")
        fid = _new_id()
        path = os.path.join(self._stage_dir(), "%s-%s" % (fid, name))

        remaining = length
        try:
            with open(path, "wb") as fp:
                while remaining > 0:
                    chunk = rfile.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise IOError("连接提前断开")
                    fp.write(chunk)
                    remaining -= len(chunk)
        except (IOError, OSError) as exc:
            try:
                os.remove(path)
            except OSError:
                pass
            return {"ok": False, "error": "上传失败: %s" % exc}, 400

        entry = {
            "id": fid,
            "name": name,
            "size": length,
            "path": path,
            "addedAt": time.time(),
        }
        with self.lock:
            self.files.append(entry)
            files = self._public_files()

        self.log("ok", "已加入 %s（%s）" % (name, human_size(length)))
        self.publish_state()
        return {
            "ok": True,
            "file": {"id": fid, "name": name, "size": length,
                     "addedAt": entry["addedAt"]},
            "files": files,
        }, 200

    # ---------------- 本工具自己的 POST 动作 ----------------

    def api(self, action, raw, data, query):
        """server.py 的通用出口。返回 None 表示不认识这个动作。"""
        if action == "files/remove":
            return self._api_remove(data)
        if action == "files/clear":
            return self._api_clear()
        if action == "fs/list":
            return self._api_list(data)
        return None

    def _api_remove(self, data):
        data = data if isinstance(data, dict) else {}
        fid = str(data.get("id") or "")

        with self.lock:
            if self.status != STATUS_IDLE:
                return {"ok": False, "error": "正在推送，先停止再删"}, 409
            entry = None
            for item in self.files:
                if item["id"] == fid:
                    entry = item
                    break
            if entry is None:
                return {"ok": False, "error": "没有这个文件"}, 404
            self.files.remove(entry)
            files = self._public_files()

        try:
            os.remove(entry["path"])
        except OSError:
            pass

        self.log("info", "已移除 %s" % entry["name"])
        self.publish_state()
        return {"ok": True, "files": files}, 200

    def _api_clear(self):
        with self.lock:
            if self.status != STATUS_IDLE:
                return {"ok": False, "error": "正在推送，先停止再清空"}, 409
            removed = list(self.files)
            self.files = []

        for entry in removed:
            try:
                os.remove(entry["path"])
            except OSError:
                pass

        if removed:
            self.log("info", "已清空 %d 个待推送文件" % len(removed))
        self.publish_state()
        return {"ok": True, "files": []}, 200

    def _api_list(self, data):
        """列设备上的目录。只返回目录，不返回文件。"""
        data = data if isinstance(data, dict) else {}
        raw_path = data.get("path")
        if raw_path is None:
            raw_path = self.config.get("pushDir") or "/"
        path, err = clean_remote_dir(raw_path)
        if err:
            return {"ok": False, "error": err}, 400

        cfg = dict(self.config)
        devices, _ = self.refresh_devices()
        serial, perr = self._pick_serial(devices)
        if not serial:
            return {"ok": False, "error": perr or "没有可用的设备"}, 409

        # adb shell 会把参数拼成一条命令交给设备上的 sh，路径带空格要自己引号
        command = "ls -a -p -L " + adbkit.device_quote(path)
        rc, out = adbkit.run(cfg["adbPath"], serial, ["shell", command], LIST_TIMEOUT)
        dirs = parse_ls(out)

        # 有个别条目读不了时 ls 也可能返回非 0；只要还列出了东西就照常显示
        if rc != 0 and not dirs:
            text = (out or "").strip() or ("ls 返回码 %s" % rc)
            return {"ok": False, "error": text}, 502

        return {
            "ok": True,
            "path": path,
            "parent": parent_dir(path),
            "dirs": dirs[:MAX_LIST_ENTRIES],
            "truncated": len(dirs) > MAX_LIST_ENTRIES,
        }, 200

    # ---------------- 停止 ----------------

    def on_stop(self):
        with self.lock:
            proc = self.process
        if proc is not None and proc.poll() is None:
            self.log("info", "正在结束 adb push（pid %s）..." % proc.pid)
            try:
                proc.terminate()
            except OSError as exc:
                self.log("warn", "结束 adb 进程失败: %s" % exc)
            self.log("warn", "已经推完的文件不会回滚；当前这个可能只推了一半")

    # ---------------- 主流程 ----------------

    def run_loop(self):
        cfg = dict(self.config)
        with self.lock:
            files = [dict(item) for item in self.files]

        if not files:
            self.log("err", "没有要推送的文件，先把文件拖进虚线框")
            return

        push_dir, err = clean_remote_dir(cfg.get("pushDir"))
        if err:
            self.log("err", err)
            return

        names = [item["name"] for item in files]
        dups = sorted({name for name in names if names.count(name) > 1})
        if dups:
            self.log("warn", "有同名文件，设备上后面的会覆盖前面的: %s"
                     % "、".join(dups))

        started = time.time()
        pushed = 0
        failed = 0
        rc = None
        stopped = False
        outcome = "fail"

        try:
            devices, _ = self.refresh_devices()
            serial, err = self._pick_serial(devices)
            if not serial:
                self.log("err", err)
                return

            self.log("info", "推送到 %s · 目标设备 %s · 共 %d 个文件"
                     % (push_dir, serial, len(files)))

            for index, item in enumerate(files, 1):
                if self.stop_event.is_set():
                    stopped = True
                    break
                if not os.path.isfile(item["path"]):
                    failed += 1
                    self.log("err", "[%d/%d] 临时文件不见了: %s"
                             % (index, len(files), item["name"]))
                    continue

                dest = push_dir.rstrip("/") + "/" + item["name"]
                rc, out, state = self._push_one(cfg, serial, item["path"], dest)
                text = _clean_output(out)
                if text:
                    self.log("info" if state == "ok" else
                             ("warn" if state == "stopped" else "err"), text)

                if state == "stopped":
                    stopped = True
                    break
                if state == "ok" and adbkit.problem(rc, out) is None:
                    pushed += 1
                    self.log("ok", "[%d/%d] 已推送 %s → %s"
                             % (index, len(files), item["name"], dest))
                    with self.lock:
                        self.stats["filesPushed"] = \
                            self.stats.get("filesPushed", 0) + 1
                else:
                    failed += 1
                    self.log("err", "[%d/%d] 推送失败: %s"
                             % (index, len(files), item["name"]))
                    with self.lock:
                        self.stats["filesFailed"] = \
                            self.stats.get("filesFailed", 0) + 1
                self.publish_state()

            if stopped:
                outcome = "stopped"
            elif failed == 0 and pushed:
                outcome = "ok"

        finally:
            elapsed = time.time() - started
            with self.lock:
                self.stats["runs"] = self.stats.get("runs", 0) + 1
                self.stats["lastRunAt"] = time.time()
                self.stats["lastExitCode"] = rc
                self.stats["lastDuration"] = round(elapsed, 2)
                if outcome == "ok":
                    self.stats["consecutiveFailures"] = 0
                elif outcome == "fail":
                    self.stats["failures"] = self.stats.get("failures", 0) + 1
                    self.stats["consecutiveFailures"] = \
                        self.stats.get("consecutiveFailures", 0) + 1
                if pushed or failed:
                    summary = "成功 %d 个，失败 %d 个" % (pushed, failed)
                else:
                    summary = ""
            if summary:
                level = "ok" if outcome == "ok" else (
                    "warn" if outcome == "stopped" else "err")
                self.log(level, "推送结束（用时 %.1f 秒）：%s" % (elapsed, summary))
            self.publish_state()

    def _push_one(self, cfg, serial, src, dest):
        """推一个文件。返回 (返回码, 输出, ok/fail/stopped)。"""
        cmd = adbkit.build_command(cfg["adbPath"], serial, ["push", src, dest])
        self.log("cmd", "$ " + shell_join(cmd))

        timeout = push_timeout(os.path.getsize(src))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                **spawn.quiet()          # 桌面版没控制台，不加就会闪黑框
            )
        except FileNotFoundError:
            self.log("err", "找不到 adb 可执行文件: %s" % cfg["adbPath"])
            return None, "", "fail"
        except OSError as exc:
            self.log("err", "无法启动 adb: %s" % exc)
            return None, "", "fail"

        with self.lock:
            self.process = proc

        out = ""
        timed_out = False
        deadline = time.time() + timeout
        try:
            while True:
                if self.stop_event.is_set():
                    break
                if time.time() > deadline:
                    timed_out = True
                    break
                try:
                    # communicate(timeout) 超时后可以再调，输出不会丢
                    out, _ = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    out, _ = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
            with self.lock:
                self.process = None

        if timed_out:
            self.log("err", "推送超时（%.0f 秒），已结束" % timeout)
            return proc.returncode, out, "fail"
        if self.stop_event.is_set():
            return proc.returncode, out, "stopped"
        return proc.returncode, out, "ok"

    # ---------------- 退出清理 ----------------

    def cleanup(self):
        """程序退出时删掉暂存目录 —— 里面是本机副本，不是用户的原文件。"""
        # 先等正在推的那个文件收尾（stop_event 已经由 AppServer.stop 设上），
        # 否则 Windows 上 adb 还开着文件，删不掉只能留下垃圾
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(5.0)
        with self.lock:
            directory = self._stage
            self._stage = None
            self.files = []
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


def _new_id():
    """短 id：够区分就行，不追求全局唯一。"""
    return os.urandom(4).hex()
