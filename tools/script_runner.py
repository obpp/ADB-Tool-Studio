# -*- coding: utf-8 -*-
"""脚本执行：把脚本推到设备上，用 adb shell 跑，输出实时回到日志。

流程等价于：

    adb -s <序列号> push check.sh /data/local/tmp/ats-7f2a-check.sh
    adb -s <序列号> shell sh /data/local/tmp/ats-7f2a-check.sh [参数...]

参数是这个脚本自己的（页面上「脚本参数」一栏，跟着脚本清单一起存），
不是全局配置 —— 几个脚本各自要的开关、路径不一样。

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
from toolbase import Tool, as_bool, as_int, as_num, as_str, shell_join

# 导入脚本的大小上限。脚本是文本，几 MB 已经离谱了。
MAX_SCRIPT_BYTES = 2 * 1024 * 1024

# 参数串上限。真要传很长的参数，写进脚本里比塞在输入框里靠谱。
MAX_ARGS_CHARS = 1024

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


def clean_args(value):
    """把页面上的参数串收拾成一行。返回 (文本, 错误说明)。

    超长直接报错而不是截断 —— 截断会悄悄改掉要执行的命令，比不执行更糟。
    """
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > MAX_ARGS_CHARS:
        return None, "参数太长（上限 %d 字符）" % MAX_ARGS_CHARS
    return text, ""


def split_args(text):
    """把参数串拆成 argv。规则和 shell 差不多，但有一处故意不同：

      * 空白分隔；'...' 原样保留；"..." 里可以用 \\" 和 \\\\ 转义
      * 反斜杠在引号外不当转义符 —— Windows 路径（C:\\temp）不写引号也不会被吃掉
      * 空引号 '' / "" 算一个空参数

    引号没闭合时抛 ValueError：执行前就报错，别猜一个可能写错分区的参数出去。
    """
    args = []
    buf = []
    started = False
    quote = None
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                buf.append(ch)
        elif quote == '"':
            if ch == "\\" and i + 1 < n and text[i + 1] in '"\\':
                i += 1
                buf.append(text[i])
            elif ch == '"':
                quote = None
            else:
                buf.append(ch)
        elif ch in " \t\r\n":
            if started:
                args.append("".join(buf))
                buf = []
                started = False
        elif ch in "'\"":
            quote = ch
            started = True
        else:
            buf.append(ch)
            started = True
        i += 1

    if quote is not None:
        raise ValueError("引号没闭合")
    if started:
        args.append("".join(buf))
    return args


def check_args(text):
    """参数串现在能不能拆成 argv。返回错误说明，没问题返回空串。"""
    try:
        split_args(text)
    except ValueError as exc:
        return str(exc)
    return ""


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / (1024.0 * 1024.0))


def _seconds_text(seconds):
    """60.0 -> 60，0.5 -> 0.5。日志里别显示无意义的小数。"""
    seconds = float(seconds)
    if seconds == int(seconds):
        return str(int(seconds))
    return "%.1f" % seconds


class ScriptRunnerTool(Tool):

    id = "script_runner"
    name = "脚本执行"
    desc = "可以拖拽导入脚本，带参执行，会把脚本推到设备上执行，输出实时显示"
    page = "scripts"
    icon = "▷"

    def __init__(self, app_dir):
        self.scripts = []          # 清单，见 _load_scripts
        self.devices = []
        self.devices_error = ""
        self._running_id = ""      # 正在执行的脚本 id，空 = 没有
        self._loop_round = 0       # 循环执行：当前 / 刚跑完的一轮
        self._loop_total = 0       # 0 = 不限次数
        self._looping = False      # 正在按循环跑（配置中途改了也不影响这一轮）
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
            # 循环执行：默认关；开启后按间隔重复跑，0 次表示一直跑到手动停止。
            # 失败默认继续，老化/压测不会被一次偶发失败整个打断。
            "loop": False,
            "loopInterval": 60.0,
            "loopCount": 0,
            "loopStopOnFail": False,
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
            "loop": as_bool(raw, "loop", base["loop"]),
            # 0 秒 = 上一轮结束就接着跑；上限一天，够覆盖各种老化测试
            "loopInterval": as_num(raw, "loopInterval", base["loopInterval"],
                                   0.0, 86400.0),
            "loopCount": as_int(raw, "loopCount", base["loopCount"], 0, 1000000),
            "loopStopOnFail": as_bool(raw, "loopStopOnFail", base["loopStopOnFail"]),
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
            "args": clean_args(raw.get("args"))[0] or "",
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
            "args": "",
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
            loop = {
                "active": self._looping and self.is_alive(),
                "round": self._loop_round,
                "total": self._loop_total,
            }
        if selected and not any(s["id"] == selected for s in scripts):
            selected = scripts[0]["id"] if scripts else ""
        return {
            "scripts": scripts,
            "selected": selected,
            "runningId": running_id,
            "devices": list(self.devices),
            "devicesError": self.devices_error,
            "remoteDir": self.config.get("remoteDir", ""),
            "loop": loop,
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
        if action == "scripts/args":
            return self._api_args(data)
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

    def _api_args(self, data):
        """参数是跟着脚本存的，切回这个脚本时输入框里还是上次那串。"""
        data = data if isinstance(data, dict) else {}
        sid = str(data.get("id") or "")
        text, err = clean_args(data.get("args"))
        if not sid:
            return {"ok": False, "error": "缺少参数"}, 400
        if not err:
            err = check_args(text)
        if err:
            return {"ok": False, "error": err}, 400

        with self.lock:
            entry, saved = self._set_args(sid, text)
            if entry is None:
                return {"ok": False, "error": "没有这个脚本"}, 404
            result = dict(entry)

        # 故意不广播：这一改几乎总是紧跟着一次「切换脚本」的 config 请求，
        # 广播出去的那份 state 里 selected 还是旧的，页面会被拽回去闪一下。
        # 页面拿返回值同步本地就行，下一次自然的状态推送会带上新参数。
        return {"ok": True, "saved": saved, "script": result}, 200

    def _set_args(self, sid, text):
        """把参数记到脚本名下。调用方持锁，返回 (条目, 有没有写盘成功)。"""
        entry = self._find(sid)
        if entry is None:
            return None, True
        if entry.get("args", "") != text:
            entry["args"] = text
            try:
                self._save_scripts()
            except (IOError, OSError) as exc:
                self.log("warn", "脚本清单写入失败: %s" % exc)
                return entry, False
        return entry, True

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

    def start(self, options=None):
        """执行前先把参数记到这个脚本名下：页面上显示的是哪串，跑的就是哪串。

        参数也走 start 而不是单独一次保存请求 —— 中间少一个来回，
        就不会出现「参数存进去了但这次执行用的还是旧的」。
        """
        options = dict(options) if isinstance(options, dict) else {}
        if "args" in options:
            text, err = clean_args(options.get("args"))
            if not err:
                err = check_args(text)
            if err:
                return False, "参数写错了: %s" % err
            options["args"] = text
            sid = str(options.get("scriptId") or self.config.get("selected") or "")
            with self.lock:
                self._set_args(sid, text)
        with self.lock:
            self._loop_round = 0
            self._loop_total = 0
        return Tool.start(self, options)

    def run_loop(self):
        cfg = dict(self.config)
        sid = str(self.start_options.get("scriptId") or cfg.get("selected") or "")

        looping = bool(cfg.get("loop"))
        interval = cfg.get("loopInterval", 0.0)
        limit = cfg.get("loopCount", 0) if looping else 0
        stop_on_fail = bool(cfg.get("loopStopOnFail"))
        round_no = 0

        # 没有可执行的脚本是配置问题，不是「这一轮失败」：循环起来只会
        # 每轮刷一遍同样的报错，先在这儿拦下。
        if looping and self._find(sid) is None:
            self.log("err", "没有可执行的脚本，先在左边导入一个")
            return

        with self.lock:
            self._looping = looping
        try:
            while not self.stop_event.is_set():
                round_no += 1
                with self.lock:
                    self._loop_round = round_no
                    self._loop_total = limit
                self.publish_state()

                if looping:
                    self.log("info", "=== 第 %d 轮%s ===" % (
                        round_no, ("/%d" % limit) if limit else ""))

                outcome = self._run_once(cfg, sid)

                if self.stop_event.is_set():
                    if looping:
                        self.log("info", "循环已停止，共完成 %d 轮" % round_no)
                    return
                if not looping:
                    return
                if limit and round_no >= limit:
                    self.log("info", "已跑满 %d 轮，循环结束" % limit)
                    return
                if outcome == "fail" and stop_on_fail:
                    self.log("warn", "本轮失败，按设置停止循环")
                    return
                if interval > 0:
                    self.log("info", "等待 %s 秒后开始第 %d 轮"
                             % (_seconds_text(interval), round_no + 1))
                    if self.stop_event.wait(interval):
                        self.log("info", "循环已停止，共完成 %d 轮" % round_no)
                        return
        finally:
            with self.lock:
                self._looping = False

    def _run_once(self, cfg, sid):
        """跑一轮。返回 ok / fail / stopped，循环按这个决定要不要继续。"""
        with self.lock:
            entry = self._find(sid)
            if entry is None:
                self.log("err", "没有可执行的脚本，先在左边导入一个")
                return "fail"
            script_path = os.path.join(self.script_dir(), entry["file"])
            script_name = entry["name"]
            remote_name = self._remote_name(entry)
            args_text = entry.get("args", "")
            self._running_id = entry["id"]

        started = time.time()
        rc = None
        outcome = "fail"
        try:
            if not os.path.isfile(script_path):
                self.log("err", "脚本文件不见了: %s" % script_path)
                return outcome

            # 参数先拆好：写错了就别推到设备上白跑一趟
            try:
                argv = split_args(args_text)
            except ValueError as exc:
                self.log("err", "参数写错了: %s" % exc)
                return outcome

            devices, _ = self.refresh_devices()
            serial, err = self._pick_serial(devices)
            if not serial:
                self.log("err", err)
                return outcome
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
                return outcome
            if self.stop_event.is_set():
                self.log("warn", "已停止（推送完成，脚本未执行）")
                outcome = "stopped"
                return outcome
            self.log("ok", "已推送到 %s" % remote)

            rc, outcome = self._shell(cfg, serial, remote, argv)
            return outcome

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

    def _shell(self, cfg, serial, remote, argv=None):
        """在设备上跑脚本，边跑边把输出推进日志。返回 (返回码, 结果)。"""
        argv = list(argv or [])

        # adb shell 是把后面的参数用空格拼成一条命令交给设备上的 sh 的，
        # 不经过本地 shell，所以带空格的参数到那边会被拆开。这里自己按 POSIX
        # 规则把每个参数引号包好，整体作为一条命令传过去。
        device_cmd = " ".join(adbkit.device_quote(tok) for tok in ["sh", remote] + argv)
        cmd = adbkit.build_command(cfg["adbPath"], serial, ["shell", device_cmd])

        # 日志里按参数分开显示，和手敲的写法一致，方便直接抄走
        self.log("cmd", "$ " + shell_join(
            adbkit.build_command(cfg["adbPath"], serial, ["shell", "sh", remote] + argv)))

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
