# -*- coding: utf-8 -*-
"""推送文件：把本机文件用 adb push 推到设备上的指定目录。

和替换镜像的区别：不写块设备、不重启，只是搬文件。

临时文件先传进本机的临时目录再 push —— 浏览器（包括桌面版的 WebView）不会把
本机完整路径交给页面，拖拽拿到的也只是 File 对象，所以「先传到后端再推」
是网页版和桌面版都成立的做法。上传是流式写盘，不把整个文件读进内存。

预设要跨重启记住文件，不能用临时目录：桌面版用原生对话框选的只记路径；
拖进来 / 网页版选的没有路径可用，复制一份到 <数据目录>/files/push_file/，
推送时再从那份副本读。

几个要知道的点：

  * 设备上同名文件会被覆盖，这是 adb push 本来的行为，日志里会写最终路径。
  * 推送目录必须是 / 开头的绝对路径；目录选择器只列目录，不列文件。
  * 推送超时按文件大小放宽：至少 2 分钟，每 1 MB 再多给 1 秒，上限 2 小时。
  * 停止只能打断「正在推的那一个」；已经推完的文件不回滚。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import adbkit
import spawn
from toolbase import Tool, as_bool, as_str, shell_join, STATUS_IDLE

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


def _clean_preset_files(raw):
    """预设里的本机文件：统一成绝对路径，顺带去重、丢掉空行。

    只记路径不复制 —— 和替换镜像一样，文件挪走以后预设里那一行会失效，
    执行时点名报错，不影响其他文件。
    """
    out = []
    seen = set()
    for item in (raw if isinstance(raw, list) else [])[:1000]:
        if isinstance(item, str):
            path = item
        elif isinstance(item, dict):
            path = item.get("path") or ""
        else:
            continue
        path = str(path).strip()
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        if path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "name": safe_file_name(os.path.basename(path))})
    return out


def _preset_name(raw_name, files):
    """名称只是列表里的标签：没填就借第一个文件的文件名。"""
    name = str(raw_name or "").strip()[:60]
    if name:
        return name
    if files:
        stem = os.path.splitext(os.path.basename(files[0]["path"]))[0].strip()
        if stem:
            return stem[:60]
    return "未命名"


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
        self.presets = []          # 预设清单，见 _load_presets
        self.devices = []
        self.devices_error = ""
        self._stage = None         # 临时目录，第一次上传时才建
        self._running_preset = ""  # 正在执行的预设 id（临时推送时为空）
        Tool.__init__(self, app_dir)
        self._load_presets()

    # ---------------- 配置 ----------------

    def default_config(self):
        return {
            "adbPath": adbkit.resolve_adb(),
            "serial": "",
            # 普通文件推送不需要 root，/data/local/tmp 比替换镜像的 /mnt 更合适
            "pushDir": "/data/local/tmp",
            # 往 /system、/vendor 这类只读分区推时，先 root 再 remount
            "remount": False,
            "selected": "",
            "lastBrowseDir": "",
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
            "remount": as_bool(raw, "remount", base["remount"]),
            "selected": as_str(raw, "selected", base["selected"]),
            "lastBrowseDir": as_str(raw, "lastBrowseDir", base["lastBrowseDir"]),
        }

    # ---------------- 预设清单 ----------------

    def manifest_path(self):
        return os.path.join(self.app_dir, "config", self.id + ".presets.json")

    def _load_presets(self):
        try:
            with open(self.manifest_path(), "r", encoding="utf-8") as fp:
                saved = json.load(fp)
        except (IOError, OSError, ValueError):
            return
        items = saved.get("presets") if isinstance(saved, dict) else None
        if not isinstance(items, list):
            return
        cleaned = []
        for raw in items:
            entry = self._clean_entry(raw)
            if entry is not None:
                cleaned.append(entry)
        self.presets = cleaned

    @staticmethod
    def _clean_entry(raw):
        """清单文件也是外部输入，逐字段挑，别整个信。"""
        if not isinstance(raw, dict):
            return None
        pid = str(raw.get("id") or "").strip()
        if not pid:
            return None
        directory, err = clean_remote_dir(raw.get("dir"))
        if err or directory == "/":
            return None
        files = _clean_preset_files(raw.get("files"))
        if not files:
            return None
        return {
            "id": pid,
            "name": _preset_name(raw.get("name"), files),
            "dir": directory,
            "files": files,
            "addedAt": raw.get("addedAt"),
            "lastRunAt": raw.get("lastRunAt"),
            "lastExitCode": raw.get("lastExitCode"),
            "lastDuration": raw.get("lastDuration"),
        }

    def _save_presets(self):
        """原子写：先写临时文件再 os.replace，中途断电不会留下半个 JSON。"""
        path = self.manifest_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"presets": self.presets}, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _save_presets_quiet(self):
        try:
            self._save_presets()
        except (IOError, OSError) as exc:
            self.log("warn", "预设清单写入失败: %s" % exc)

    def _find(self, preset_id):
        for entry in self.presets:
            if entry["id"] == preset_id:
                return entry
        return None

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
            presets = [dict(p) for p in self.presets]
            selected = self.config.get("selected", "")
            return {
                "files": self._public_files(),
                "presets": presets,
                "selected": selected,
                "runningId": self._running_preset,
                "devices": list(self.devices),
                "devicesError": self.devices_error,
            }

    def brief(self):
        stats = self.stats
        line = "%d 个文件待推送 · 已推送 %d 个" % (
            len(self.files), stats.get("filesPushed", 0))
        if stats.get("filesFailed"):
            line += " · 失败 %d 个" % stats["filesFailed"]
        if self.presets:
            line += " · %d 个预设" % len(self.presets)
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

    def upload(self, rfile, length, query, action="files/upload"):
        """流式接收一个文件。server.py 直接把 rfile 交进来，不走 _read_body。

        action 区分两种去处：
          files/upload    → 临时暂存目录，推完/退出就没了（临时文件标签）
          presets/upload  → 持久目录，给预设用（拖进来或网页版选的文件要先复制一份）

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
        if action == "presets/upload":
            return self._receive_preset_file(rfile, length, name)

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

    # ---------------- 预设文件的持久副本 ----------------

    def _preset_copy_dir(self):
        """拖进来 / 网页版选的文件没有本机路径可用，复制一份放到这里。

        放在可写数据目录下（和 config/、logs/ 同级），exit 后仍然在，
        预设下次打开还能用；内容一样就复用同一份，重复添加不会越堆越多。
        """
        path = os.path.join(self.app_dir, "files", "push_file")
        os.makedirs(path, exist_ok=True)
        return path

    def _receive_preset_file(self, rfile, length, name):
        try:
            directory = self._preset_copy_dir()
        except OSError as exc:
            return {"ok": False, "error": "建不了存放目录: %s" % exc}, 500

        tmp = os.path.join(directory, ".part-%s" % _new_id())
        digest = hashlib.sha1()
        remaining = length
        try:
            with open(tmp, "wb") as fp:
                while remaining > 0:
                    chunk = rfile.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise IOError("连接提前断开")
                    fp.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            # 文件名前缀用内容哈希：同一个文件加两次只有一份
            final = os.path.join(directory, "%s-%s"
                                 % (digest.hexdigest()[:16], name))
            if os.path.exists(final):
                os.remove(tmp)
            else:
                os.replace(tmp, final)
        except (IOError, OSError) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"ok": False, "error": "上传失败: %s" % exc}, 400

        self.log("info", "已复制到预设文件目录: %s（%s）"
                 % (name, human_size(length)))
        return {
            "ok": True,
            "path": os.path.abspath(final),
            "name": name,
            "size": length,
        }, 200

    def _drop_unused_copies(self, files):
        """预设不再引用某个副本时删掉它；别的预设还在用就留着。

        只删本工具复制进来的那份（直接位于副本目录下），用户自己的文件不碰。
        """
        try:
            root = os.path.abspath(self._preset_copy_dir())
        except OSError:
            return
        with self.lock:
            keep = set()
            for preset in self.presets:
                for item in preset["files"]:
                    keep.add(os.path.abspath(item["path"]))
        for item in files or []:
            path = os.path.abspath(item.get("path") or "")
            if not path or path in keep:
                continue
            if os.path.dirname(path) != root:
                continue
            try:
                os.remove(path)
            except OSError:
                pass

    # ---------------- 本工具自己的 POST 动作 ----------------

    def api(self, action, raw, data, query):
        """server.py 的通用出口。返回 None 表示不认识这个动作。"""
        if action == "files/remove":
            return self._api_remove(data)
        if action == "files/clear":
            return self._api_clear()
        if action == "fs/list":
            return self._api_list(data)
        if action == "presets/save":
            return self._api_save_preset(data)
        if action == "presets/delete":
            return self._api_delete_preset(data)
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

    # ---------------- 预设 ----------------

    def _api_save_preset(self, data):
        data = data if isinstance(data, dict) else {}
        pid = str(data.get("id") or "").strip()
        directory, err = clean_remote_dir(data.get("dir"))
        if err:
            return {"ok": False, "error": "推送目录：%s" % err}, 400
        if directory == "/":
            return {"ok": False, "error": "推送目录不能是根目录"}, 400

        files = _clean_preset_files(data.get("files"))
        if not files:
            return {"ok": False, "error": "预设里至少要有 1 个文件"}, 400
        name = _preset_name(data.get("name"), files)

        with self.lock:
            entry = self._find(pid) if pid else None
            if pid and entry is None:
                return {"ok": False, "error": "这个预设已经不在了，刷新一下"}, 404
            replaced = list(entry["files"]) if entry is not None else []
            if entry is None:
                entry = {
                    "id": _new_id(),
                    "addedAt": time.time(),
                    "lastRunAt": None,
                    "lastExitCode": None,
                    "lastDuration": None,
                }
                self.presets.append(entry)
            entry.update({"name": name, "dir": directory, "files": files})

            saved = True
            try:
                self._save_presets()
            except (IOError, OSError) as exc:
                saved = False
                self.log("warn", "预设清单写入失败: %s" % exc)

            self.config["selected"] = entry["id"]
            try:
                self.save_config()
            except (IOError, OSError):
                pass
            result = dict(entry)
            presets = [dict(p) for p in self.presets]

        # 从预设里去掉的文件，如果是本工具复制进来的，顺手清掉。
        # 清单没写成功就不动副本：重启后读的还是旧清单，那些文件还要用。
        if saved:
            self._drop_unused_copies(replaced)

        missing = [item["path"] for item in files
                   if not os.path.isfile(item["path"])]
        names = [item["name"] for item in files]
        dups = sorted({n for n in names if names.count(n) > 1})

        self.log("info", "预设已保存: %s（%d 个文件 → %s）"
                 % (name, len(files), directory))
        if dups:
            self.log("warn", "预设里有同名文件，设备上后面的会覆盖前面的: %s"
                     % "、".join(dups))
        if missing:
            self.log("warn", "有 %d 个文件现在找不到，推送时会跳过: %s"
                     % (len(missing), missing[0]))
        self.publish_state()
        return {
            "ok": True,
            "saved": saved,
            "preset": result,
            "presets": presets,
            "missing": missing,
        }, 200

    def _api_delete_preset(self, data):
        data = data if isinstance(data, dict) else {}
        pid = str(data.get("id") or "")

        with self.lock:
            entry = self._find(pid)
            if entry is None:
                return {"ok": False, "error": "没有这个预设"}, 404
            if self._running_preset == pid:
                return {"ok": False, "error": "这个预设正在推送，先停止再删除"}, 409

            self.presets.remove(entry)
            saved = True
            try:
                self._save_presets()
            except (IOError, OSError) as exc:
                saved = False
                self.log("warn", "预设清单写入失败: %s" % exc)

            if self.config.get("selected") == pid:
                self.config["selected"] = (
                    self.presets[0]["id"] if self.presets else "")
                try:
                    self.save_config()
                except (IOError, OSError):
                    pass
            presets = [dict(p) for p in self.presets]
            selected = self.config.get("selected", "")

        if saved:
            self._drop_unused_copies(entry["files"])

        self.log("info", "已删除预设 %s" % entry["name"])
        self.publish_state()
        return {"ok": True, "presets": presets, "selected": selected}, 200

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
        preset_id = str(self.start_options.get("presetId") or "")
        preset = None

        if preset_id:
            preset = self._find(preset_id)
            if preset is None:
                self.log("err", "这个预设已经不在了，刷新页面再选一次")
                return
            push_dir, err = clean_remote_dir(preset.get("dir"))
            if err:
                self.log("err", "预设里的推送目录不对：%s" % err)
                return
            files = [{"name": item["name"], "path": item["path"]}
                     for item in preset.get("files") or []]
            self.log("info", "使用预设: %s" % preset["name"])
        else:
            with self.lock:
                files = [dict(item) for item in self.files]
            push_dir, err = clean_remote_dir(cfg.get("pushDir"))
            if err:
                self.log("err", err)
                return

        if not files:
            self.log("err", "没有要推送的文件" + (
                "，先在这个预设里添加文件" if preset is not None
                else "，先把文件拖进虚线框"))
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
        with self.lock:
            self._running_preset = preset["id"] if preset is not None else ""

        try:
            devices, _ = self.refresh_devices()
            serial, err = self._pick_serial(devices)
            if not serial:
                self.log("err", err)
                return

            # 往只读分区推之前先拿权限；失败就不推了，免得每个文件都报只读
            if cfg["remount"]:
                if not self._prepare_remount(cfg, serial):
                    return

            self.log("info", "推送到 %s · 目标设备 %s · 共 %d 个文件"
                     % (push_dir, serial, len(files)))

            for index, item in enumerate(files, 1):
                if self.stop_event.is_set():
                    stopped = True
                    break
                if not os.path.isfile(item["path"]):
                    failed += 1
                    self.log("err", "[%d/%d] 文件不见了，跳过: %s"
                             % (index, len(files), item["path"]))
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
                self._running_preset = ""
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
            if preset is not None:
                self._record_preset(preset["id"], rc, elapsed)
            if summary:
                level = "ok" if outcome == "ok" else (
                    "warn" if outcome == "stopped" else "err")
                self.log(level, "推送结束（用时 %.1f 秒）：%s" % (elapsed, summary))
            self.publish_state()

    def _record_preset(self, preset_id, rc, elapsed):
        """把这一次的结果写回预设，列表里就能看到「上次 退出码 N」。"""
        with self.lock:
            entry = self._find(preset_id)
            if entry is None:
                return
            entry["lastRunAt"] = time.time()
            entry["lastExitCode"] = rc
            entry["lastDuration"] = round(elapsed, 2)
        self._save_presets_quiet()

    def _prepare_remount(self, cfg, serial):
        """按 root → 等设备上线 → remount 的顺序拿写权限。

        成功返回 True；任何一步失败都返回 False，并且已经写清楚原因 ——
        调用方直接结束这次推送，别让每个文件各报一遍只读。
        """
        steps = (
            ("root", 30.0, "已切换到 root"),
            ("wait-for-device", 90.0, "设备已重新上线"),
            ("remount", 60.0, "remount 完成"),
        )
        for args, timeout, ok_text in steps:
            if self.stop_event.is_set():
                return False
            cmd = adbkit.build_command(cfg["adbPath"], serial, [args])
            self.log("cmd", "$ " + shell_join(cmd))
            rc, out = adbkit.run(cfg["adbPath"], serial, [args], timeout)
            text = (out or "").strip()
            if text:
                self.log("info", text)
            low = text.lower()

            if args == "root":
                # production build 上 adb root 常常返回 0，却打印无法 root
                if rc != 0 or "cannot run as root" in low:
                    self.log("err", "adb root 失败，已停止。这台机器不允许 adbd 以 root "
                                    "运行（production build 很常见）。如果目标目录不需要 "
                                    "remount，把「先 root 并 remount」关掉再推。")
                    return False
            elif args == "wait-for-device":
                if rc != 0:
                    self.log("err", "root 之后设备没有回来，已停止")
                    return False
            else:
                failed = (rc != 0
                          or "remount failed" in low
                          or "not running as root" in low
                          or "permission denied" in low)
                if failed:
                    self.log("err", "adb remount 失败，已停止。有些机器要先执行 "
                                    "`adb disable-verity` 再重启一次；也可以确认一下 "
                                    "目标目录是不是本来就不需要 remount。")
                    return False
            self.log("ok", ok_text)
        return True

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
