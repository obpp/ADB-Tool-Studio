# -*- coding: utf-8 -*-
"""脚本执行：把脚本推到设备上，用 adb shell 跑，输出实时回到日志。

流程等价于：

    adb -s <序列号> push check.sh /data/local/tmp/ats-7f2a-check.sh
    adb -s <序列号> shell sh /data/local/tmp/ats-7f2a-check.sh

每次执行都重新推一份，不做「设备上已有就跳过」的判断 —— 列表里显示的是哪份，
跑的就一定是哪份。

几个不显眼但会让功能直接不可用的点：

  * Windows 上编辑的脚本是 CRLF，Android 的 sh 会把 \\r 当成命令的一部分，
    报 `: not found` 或者 `syntax error`。导入时就统一成 LF。
  * adb push 需要文件路径，给不了字节流。所以脚本必须落盘，不能只存在内存里。
  * adb shell 断开后设备上那个进程不保证退出，所以「停止」只是断开连接。
"""

import json
import os
import re
import subprocess
import threading
import time

import adbkit
import spawn
from toolbase import Tool, as_num, as_str, shell_join

# 导入脚本的大小上限。脚本是文本，几 MB 已经离谱了。
MAX_SCRIPT_BYTES = 2 * 1024 * 1024

# 文件名里不能出现的字符：Windows 的保留字符 + 控制字符
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 远程路径只留 ASCII —— 它会被拼进 adb shell 的命令行，中文和空格都可能出岔子
_SAFE_REMOTE = re.compile(r"[^A-Za-z0-9._-]")

# Windows 上不能直接当文件名的保留字
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] +
    ["COM%d" % i for i in range(1, 10)] +
    ["LPT%d" % i for i in range(1, 10)]
)


def safe_filename(name):
    """把用户给的文件名收拾成能安全落盘的名字。"""
    base = os.path.basename(str(name or "").strip())
    base = _BAD_NAME.sub("_", base).strip(" .")
    if not base:
        base = "script.sh"
    if base.split(".")[0].upper() in _WIN_RESERVED:
        base = "_" + base
    if len(base) > 80:                      # 太长的名字某些文件系统存不下
        stem, ext = os.path.splitext(base)
        base = stem[:80 - len(ext)] + ext
    return base


def normalize_text(data):
    """CRLF → LF，去掉 UTF-8 BOM。解不成文本的原样返回。

    这一步不做的话，Windows 上写的 .sh 推到板子上必定报 `: not found`。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if "\x00" in text:                      # 含 NUL，多半是二进制
        return data
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / (1024.0 * 1024.0))


class ScriptRunnerTool(Tool):

    id = "script_runner"
    name = "脚本执行"
    desc = "把脚本推到设备上执行，输出实时显示"
    page = "scripts"
    icon = "▷"

    def __init__(self, app_dir):
        self.scripts = []          # 清单，见 _load_scripts
        self.devices = []
        self.devices_error = ""
        self._running_id = ""      # 正在执行的脚本 id，空 = 没有
        Tool.__init__(self, app_dir)
        self._load_scripts()

    # ---------------- 路径 ----------------

    def script_dir(self):
        return os.path.join(self.app_dir, "scripts")

    def manifest_path(self):
        return os.path.join(self.app_dir, "config", self.id + ".scripts.json")

    # ---------------- 配置 ----------------

    def default_config(self):
        return {
            "adbPath": adbkit.resolve_adb(),
            "serial": "",
            "remoteDir": "/data/local/tmp",
            "timeout": 60.0,
            "selected": "",
        }

    def sanitize_config(self, raw, base=None):
        base = dict(base) if isinstance(base, dict) else self.default_config()
        raw = raw if isinstance(raw, dict) else {}

        remote = as_str(raw, "remoteDir", base["remoteDir"], nonempty=True)
        if not remote.startswith("/"):       # 相对路径推到设备上会落到 adb 的 cwd
            remote = "/data/local/tmp"
        remote = remote.rstrip("/") or "/"

        return {
            "adbPath": as_str(raw, "adbPath", base["adbPath"], nonempty=True),
            "serial": as_str(raw, "serial", base["serial"]),
            "remoteDir": remote,
            # 下限 5 秒：PC 休眠时 adb 的管道可能既不返回也不报错，
            # 没有超时这个后台线程就永久挂住了。
            "timeout": as_num(raw, "timeout", base["timeout"], 5.0, 3600.0),
            "selected": as_str(raw, "selected", base["selected"]),
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
        }

    # ---------------- 脚本清单 ----------------

    def _load_scripts(self):
        try:
            with open(self.manifest_path(), "r", encoding="utf-8") as fp:
                saved = json.load(fp)
        except (IOError, OSError, ValueError):
            return
        items = saved.get("scripts") if isinstance(saved, dict) else None
        if not isinstance(items, list):
            return
        cleaned = []
        for raw in items:
            entry = self._clean_entry(raw)
            if entry is not None:
                cleaned.append(entry)
        self.scripts = cleaned

    @staticmethod
    def _clean_entry(raw):
        """清单文件也是外部输入，逐字段挑，别整个信。"""
        if not isinstance(raw, dict):
            return None
        sid = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        fname = str(raw.get("file") or "").strip()
        if not sid or not name or not fname:
            return None
        if os.path.basename(fname) != fname:     # 防止清单里塞 ../ 之类的路径
            return None
        return {
            "id": sid,
            "name": name,
            "file": fname,
            "size": int(raw.get("size") or 0),
            "addedAt": raw.get("addedAt"),
            "lastRunAt": raw.get("lastRunAt"),
            "lastExitCode": raw.get("lastExitCode"),
            "lastDuration": raw.get("lastDuration"),
        }

    def _save_scripts(self):
        """原子写：先写临时文件再 os.replace，中途断电也不会留下半个 JSON。"""
        path = self.manifest_path()
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"scripts": self.scripts}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _save_scripts_quiet(self):
        try:
            self._save_scripts()
        except (IOError, OSError) as exc:
            self.log("warn", "脚本清单写入失败: %s" % exc)

    def _find(self, script_id):
        for entry in self.scripts:
            if entry["id"] == script_id:
                return entry
        return None

    def _add(self, name, payload):
        """落盘 + 加进清单。调用方持锁，并负责把清单写回去。"""
        directory = self.script_dir()
        os.makedirs(directory, exist_ok=True)

        target, fname = self._unique_path(directory, safe_filename(name))
        with open(target, "wb") as fp:
            fp.write(payload)

        entry = {
            "id": _new_id(),
            "name": safe_filename(name),
            "file": fname,
            "size": len(payload),
            "addedAt": time.time(),
            "lastRunAt": None,
            "lastExitCode": None,
            "lastDuration": None,
        }
        self.scripts.append(entry)
        return entry

    @staticmethod
    def _unique_path(directory, base):
        stem, ext = os.path.splitext(base)
        for i in range(1, 1000):
            name = base if i == 1 else "%s-%d%s" % (stem, i, ext)
            if not os.path.exists(os.path.join(directory, name)):
                return os.path.join(directory, name), name
        return os.path.join(directory, base), base

    def _remote_name(self, entry):
        """设备上的文件名。带 id 前缀，同名脚本不会互相覆盖。"""
        stem, ext = os.path.splitext(entry["name"])
        base = _SAFE_REMOTE.sub("_", stem).strip("._-")
        if not re.search(r"[A-Za-z0-9]", base):
            base = "script"
        ext = _SAFE_REMOTE.sub("", ext)[:10]
        return "ats-%s-%s%s" % (entry["id"][:8], base[:40], ext)

    # ---------------- 给前端的状态 ----------------

    def extra(self):
        with self.lock:
            scripts = [dict(s) for s in self.scripts]
            selected = self.config.get("selected", "")
            running_id = self._running_id
        if selected and not any(s["id"] == selected for s in scripts):
            selected = scripts[0]["id"] if scripts else ""
        return {
            "scripts": scripts,
            "selected": selected,
            "runningId": running_id,
            "devices": list(self.devices),
            "devicesError": self.devices_error,
            "remoteDir": self.config.get("remoteDir", ""),
        }

    def brief(self):
        stats = self.stats
        line = "%d 个脚本 · 已执行 %d 次" % (len(self.scripts), stats.get("runs", 0))
        if stats.get("failures"):
            line += " · 失败 %d 次" % stats["failures"]
        return line

    # ---------------- 设备 ----------------

    def refresh_devices(self):
        cfg = self.config
        timeout = min(10.0, cfg["timeout"])
        devices, err = adbkit.list_devices(cfg["adbPath"], cfg["serial"], timeout)
        with self.lock:
            self.devices = devices
            self.devices_error = err
            self.stats["deviceCount"] = len(devices)
        return devices, err

    def _pick_serial(self, devices):
        """确定这次用哪台设备。返回 (serial, 错误说明)。"""
        return adbkit.pick_serial(devices, self.config["serial"], self.devices_error)

    # ---------------- 本工具自己的 POST 动作 ----------------

    def api(self, action, raw, data, query):
        """server.py 的通用出口。返回 None 表示不认识这个动作。"""
        if action == "scripts/import":
            return self._api_import(raw, query)
        if action == "scripts/rename":
            return self._api_rename(data)
        if action == "scripts/delete":
            return self._api_delete(data)
        return None

    def _api_import(self, raw, query):
        if not raw:
            return {"ok": False, "error": "文件是空的"}, 400
        if len(raw) > MAX_SCRIPT_BYTES:
            return {"ok": False, "error": "文件太大（上限 2 MB）"}, 413

        payload = normalize_text(raw)
        name = safe_filename(query.get("name") or "script.sh")

        with self.lock:
            entry = self._add(name, payload)
            try:
                self._save_scripts()
            except (IOError, OSError) as exc:
                # 清单元数据和文件必须一起成功，否则列表和磁盘就对不上了
                self.scripts.remove(entry)
                try:
                    os.remove(os.path.join(self.script_dir(), entry["file"]))
                except OSError:
                    pass
                return {"ok": False, "error": "写入失败: %s" % exc}, 500

            self.config["selected"] = entry["id"]
            try:
                self.save_config()
            except (IOError, OSError):
                pass          # 清单已经存好了，配置丢了不影响用
            scripts = [dict(s) for s in self.scripts]

        self.log("ok", "已导入 %s（%s）" % (entry["name"], human_size(entry["size"])))
        self.publish_state()
        return {"ok": True, "script": entry, "scripts": scripts}, 200

    def _api_rename(self, data):
        data = data if isinstance(data, dict) else {}
        sid = str(data.get("id") or "")
        name = safe_filename(str(data.get("name") or ""))
        if not sid or not name:
            return {"ok": False, "error": "缺少参数"}, 400

        with self.lock:
            entry = self._find(sid)
            if entry is None:
                return {"ok": False, "error": "没有这个脚本"}, 404
            entry["name"] = name
            try:
                self._save_scripts()
                saved = True
            except (IOError, OSError) as exc:
                saved = False
                self.log("warn", "脚本清单写入失败: %s" % exc)
            result = dict(entry)

        self.log("info", "脚本改名为 %s" % name)
        self.publish_state()
        return {"ok": True, "saved": saved, "script": result}, 200

    def _api_delete(self, data):
        data = data if isinstance(data, dict) else {}
        sid = str(data.get("id") or "")

        with self.lock:
            entry = self._find(sid)
            if entry is None:
                return {"ok": False, "error": "没有这个脚本"}, 404

            # adb push 要的是文件路径，运行中就删不了（Windows 直接拒绝）
            if self._running_id == sid:
                return {"ok": False, "error": "这个脚本正在执行，先停止再删除"}, 409

            try:
                os.remove(os.path.join(self.script_dir(), entry["file"]))
            except OSError as exc:
                if os.path.exists(os.path.join(self.script_dir(), entry["file"])):
                    return {"ok": False, "error": "删不掉文件: %s" % exc}, 500
                # 文件本来就不在了，那就只清理清单

            self.scripts.remove(entry)
            try:
                self._save_scripts()
            except (IOError, OSError) as exc:
                self.log("warn", "脚本清单写入失败: %s" % exc)

            if self.config.get("selected") == sid:
                self.config["selected"] = self.scripts[0]["id"] if self.scripts else ""
                try:
                    self.save_config()
                except (IOError, OSError):
                    pass

        self.log("info", "已删除脚本 %s" % entry["name"])
        self.publish_state()
        return {"ok": True}, 200

    # ---------------- 停止 ----------------

    def on_stop(self):
        with self.lock:
            proc = self.process
        if proc is not None and proc.poll() is None:
            self.log("info", "正在结束 adb shell（pid %s）..." % proc.pid)
            try:
                proc.terminate()
            except OSError as exc:
                self.log("warn", "结束 adb 进程失败: %s" % exc)
            # 说清楚这条语义，别让人以为设备上的命令一定停了
            self.log("warn", "这只是断开 adb 连接，设备上正在跑的命令不一定跟着退出")

    # ---------------- 主流程 ----------------

    def run_loop(self):
        cfg = dict(self.config)
        sid = str(self.start_options.get("scriptId") or cfg.get("selected") or "")

        with self.lock:
            entry = self._find(sid)
            if entry is None:
                self.log("err", "没有可执行的脚本，先在左边导入一个")
                return
            script_path = os.path.join(self.script_dir(), entry["file"])
            script_name = entry["name"]
            remote_name = self._remote_name(entry)
            self._running_id = entry["id"]

        started = time.time()
        rc = None
        outcome = "fail"
        try:
            if not os.path.isfile(script_path):
                self.log("err", "脚本文件不见了: %s" % script_path)
                return

            devices, _ = self.refresh_devices()
            serial, err = self._pick_serial(devices)
            if not serial:
                self.log("err", err)
                return
            self.log("info", "执行 %s · 目标设备 %s" % (script_name, serial))

            remote = cfg["remoteDir"].rstrip("/") + "/" + remote_name
            push_cmd = adbkit.build_command(cfg["adbPath"], serial, ["push", script_path, remote])
            self.log("cmd", "$ " + shell_join(push_cmd))
            push_rc, push_out = adbkit.run(
                cfg["adbPath"], serial, ["push", script_path, remote], cfg["timeout"])
            text = (push_out or "").strip()
            if text:
                self.log("info", text)
            if adbkit.problem(push_rc, push_out) is not None:
                self.log("err", "推送失败，脚本没有执行")
                return
            if self.stop_event.is_set():
                self.log("warn", "已停止（推送完成，脚本未执行）")
                outcome = "stopped"
                return
            self.log("ok", "已推送到 %s" % remote)

            rc, outcome = self._shell(cfg, serial, remote)

        finally:
            elapsed = time.time() - started
            with self.lock:
                entry = self._find(sid)
                if entry is not None:
                    entry["lastRunAt"] = time.time()
                    entry["lastExitCode"] = rc
                    entry["lastDuration"] = round(elapsed, 2)
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
                self._running_id = ""
                self._save_scripts_quiet()
            self.publish_state()

    def _shell(self, cfg, serial, remote):
        """在设备上跑脚本，边跑边把输出推进日志。返回 (返回码, 结果)。"""
        cmd = adbkit.build_command(cfg["adbPath"], serial, ["shell", "sh", remote])
        self.log("cmd", "$ " + shell_join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                **spawn.quiet()          # 桌面版没控制台，不加就会闪黑框
            )
        except FileNotFoundError:
            self.log("err", "找不到 adb 可执行文件: %s" % cfg["adbPath"])
            return None, "fail"
        except OSError as exc:
            self.log("err", "无法启动 adb: %s" % exc)
            return None, "fail"

        with self.lock:
            self.process = proc

        reader = threading.Thread(target=self._pump, args=(proc,), name="script-out")
        reader.daemon = True
        reader.start()

        t0 = time.time()
        deadline = t0 + cfg["timeout"]
        timed_out = False
        while proc.poll() is None:
            if self.stop_event.is_set():
                try:
                    proc.terminate()
                except OSError:
                    pass
                break
            if time.time() > deadline:
                timed_out = True
                self.log("err", "执行超时（%.0f 秒），已强制结束" % cfg["timeout"])
                try:
                    proc.terminate()
                except OSError:
                    pass
                break
            self.stop_event.wait(0.2)

        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()

        elapsed = time.time() - t0
        reader.join(timeout=2)
        with self.lock:
            self.process = None

        if timed_out:
            return rc, "fail"
        if self.stop_event.is_set():
            self.log("warn", "已停止（用时 %.1f 秒）" % elapsed)
            return rc, "stopped"
        if rc == 0:
            self.log("ok", "执行完成，退出码 0（用时 %.1f 秒）" % elapsed)
            return rc, "ok"
        self.log("err", "执行失败，退出码 %s（用时 %.1f 秒）" % (rc, elapsed))
        return rc, "fail"

    def _pump(self, proc):
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip("\r\n")
                if line:
                    self.log("info", line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                proc.stdout.close()
            except (OSError, AttributeError):
                pass


def _new_id():
    """短 id：够区分就行，不追求全局唯一。"""
    return os.urandom(4).hex()
