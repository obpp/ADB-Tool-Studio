# -*- coding: utf-8 -*-
"""替换镜像：把本地镜像推到设备上，dd 写进分区，然后重启。

一个预设可以有多行「镜像 → 目标」，同一个镜像也可以写多个分区（A/B 槽）。
流程等价于：

    adb -s <序列号> shell "test -b /dev/block/by-name/boot_a && echo ATS_BLOCK"
    adb -s <序列号> push ~/rk3576_r8/kernel-6.1/boot.img /mnt/ats-xxxxxxxx-1-boot.img
    adb -s <序列号> shell "wc -c /mnt/ats-xxxxxxxx-1-boot.img"
    adb -s <序列号> shell "dd if=/mnt/ats-xxxxxxxx-1-boot.img of=/dev/block/by-name/boot_a bs=1048576"
    adb -s <序列号> shell "dd if=/mnt/ats-xxxxxxxx-1-boot.img of=/dev/block/by-name/boot_b bs=1048576"
    adb -s <序列号> shell sync
    adb -s <序列号> shell reboot

和脚本执行的区别在「错了会变砖」，所以这里多了几道闸，任何一道不过都直接
放弃，而且**只有全部 dd 成功才重启**：

  * 写入目标只允许 /dev/ 下的路径（或者裸分区名，自动补成 by-name）；同一分区不能分给两个镜像
  * 所有目标先 test -b 验一遍，再开始写 —— 别写到一半才发现 boot_b 不存在
  * 设备上有 blockdev 的话，镜像比分区大就不写
  * 推完用 wc -c 核对大小，和设备上不一致就不写
  * dd 输出里出现 No space left / I/O error 之类也按失败处理
  * 中途失败或按停止不会重启（已经写进去的分区不会回滚，日志里会明说写了几个）

镜像本身不进程序目录，预设里只记绝对路径 —— boot.img 动辄几十上百 MB，
复制一份既费磁盘，改了源码目录里的文件还得再导一次。
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

# 短命令（test / wc / blockdev）的超时。这些命令要么秒回，要么卡住了。
CHECK_TIMEOUT = 15.0
REBOOT_TIMEOUT = 20.0
SYNC_TIMEOUT = 60.0

# dd 的块大小。用数字不用 1M：不是所有 toybox/busybox 都认识 M 后缀。
DD_BS = 1048576

# 写入目标只允许 /dev/ 下的路径。别的路径轻则写坏文件，重则覆盖数据分区。
# 这个正则同时把空格、引号、分号挡掉了 —— 命令是拼成一条字符串交给设备上
# 的 sh 执行的，任何元字符都可能被执行。
_TARGET_RE = re.compile(r"^/dev/[A-Za-z0-9._/-]+$")
_REMOTE_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")

# 只写分区名（boot_a）时补成这个前缀。大多数 Android 设备都有 by-name 目录。
TARGET_PREFIX = "/dev/block/by-name/"
_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_target(path):
    """写入目标：/dev/ 下、只含安全字符、没有 .. 段。"""
    return bool(_TARGET_RE.match(path or "")) and ".." not in path.split("/")


def expand_target(value):
    """目标可以写完整路径，也可以只写分区名（boot_a）。"""
    value = (value or "").strip()
    if value.startswith("/"):
        return value
    if _SHORTHAND_RE.match(value):
        return TARGET_PREFIX + value
    return value


def split_targets(value):
    """页面传字符串（空格/逗号分隔）或列表，统一成去重后的目标列表。"""
    if isinstance(value, str):
        parts = re.split(r"[\s,，;；]+", value.strip())
    elif isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value]
    else:
        parts = []
    out = []
    for part in parts:
        if not part:
            continue
        target = expand_target(part)
        if target and target not in out:
            out.append(target)
    return out


def parse_items(raw, legacy_image="", legacy_target=""):
    """把请求里的镜像列表整理成 [{imagePath, targets}]。

    返回 (items, error)。空行（还没填的新行）直接跳过；填了一半就报错，
    免得用户以为保存了、执行时才发现少东西。
    """
    if raw is None:
        raw = [{"imagePath": legacy_image, "targets": legacy_target}]
    if not isinstance(raw, list):
        return [], "镜像列表格式不对"

    items = []
    seen = {}          # 目标 -> 镜像，用来挡住两个镜像抢同一个分区
    for idx, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            continue
        image = str(row.get("imagePath") or "").strip()
        targets = split_targets(row.get("targets", row.get("target")))
        if not image and not targets:
            continue
        if not image:
            return [], "第 %d 行还没选镜像" % idx
        if not targets:
            return [], "第 %d 行没有写写入目标" % idx
        for target in targets:
            if not valid_target(target):
                return [], "第 %d 行的写入目标不合法: %s" % (idx, target)
            other = seen.get(target)
            if other is not None and other != image:
                return [], "写入目标 %s 同时给了两个镜像" % target
            seen[target] = image
        items.append({"imagePath": image, "targets": targets})

    if not items:
        return [], "至少要有一个镜像和写入目标"
    return items, None


def _clean_items(raw, legacy_image="", legacy_target=""):
    """清单文件用的宽松版：保留用户填的内容，合法性交给执行前检查。"""
    rows = raw if isinstance(raw, list) else [{
        "imagePath": legacy_image, "targets": legacy_target}]
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        image = str(row.get("imagePath") or "").strip()
        targets = split_targets(row.get("targets", row.get("target")))
        if image or targets:
            items.append({"imagePath": image, "targets": targets})
    return items


def valid_remote_dir(path):
    """设备上的目录：绝对路径、只含安全字符、没有 .. 段。"""
    return bool(_REMOTE_DIR_RE.match(path or "")) and ".." not in path.split("/")


# 远程文件名只留 ASCII，理由同上
_SAFE_REMOTE = re.compile(r"[^A-Za-z0-9._-]")

# 镜像浏览器里默认只看这些后缀。看别的可以勾「显示全部」。
IMAGE_EXTS = (".img", ".bin", ".raw", ".iso")

# dd 明明返回 0 但输出里出现这些，也算写失败了
_DD_BAD = (
    "no space left",
    "i/o error",
    "input/output error",
    "permission denied",
    "read-only file system",
    "cannot open",
)


def _new_id():
    return os.urandom(4).hex()


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    if n < 1024 * 1024 * 1024:
        return "%.1f MB" % (n / (1024.0 * 1024.0))
    return "%.2f GB" % (n / (1024.0 * 1024.0 * 1024.0))


def remote_name(local_path, preset_id, index=0):
    """设备上的临时文件名。带预设 id 前缀，两个预设的 boot.img 不会互相覆盖。

    一个预设里有多个镜像时再带上序号：两个不同目录下的 boot.img 也不会撞名。
    """
    base = os.path.basename(local_path or "")
    stem, ext = os.path.splitext(base)
    stem = _SAFE_REMOTE.sub("_", stem).strip("._-")
    if not re.search(r"[A-Za-z0-9]", stem):
        stem = "image"
    ext = _SAFE_REMOTE.sub("", ext)[:10]
    prefix = "ats-%s" % preset_id[:8]
    if index:
        prefix += "-%d" % index
    return "%s-%s%s" % (prefix, stem[:40], ext)


class ReplaceImageTool(Tool):

    id = "replace_image"
    name = "替换镜像"
    desc = "推送镜像并写入分区，完成后重启"
    page = "replace-image"
    icon = "⤓"

    def __init__(self, app_dir):
        self.presets = []            # 清单，见 _load_presets
        self.devices = []
        self.devices_error = ""
        self._running_id = ""
        self._stage = "idle"         # push / dd / sync / reboot，停止时用来说明后果
        Tool.__init__(self, app_dir)
        self._load_presets()

    # ---------------- 路径 ----------------

    def manifest_path(self):
        return os.path.join(self.app_dir, "config", self.id + ".presets.json")

    # ---------------- 配置 ----------------

    def default_config(self):
        return {
            "adbPath": adbkit.resolve_adb(),
            "serial": "",
            # 一次 push 或 dd 允许跑多久。默认 10 分钟：100 MB 的镜像
            # 走 USB 也就几十秒，留足余量又不至于卡死。
            "timeout": 600.0,
            "selected": "",
            "lastBrowseDir": "",
        }

    def sanitize_config(self, raw, base=None):
        base = dict(base) if isinstance(base, dict) else self.default_config()
        raw = raw if isinstance(raw, dict) else {}
        return {
            "adbPath": as_str(raw, "adbPath", base["adbPath"], nonempty=True),
            "serial": as_str(raw, "serial", base["serial"]),
            "timeout": as_num(raw, "timeout", base["timeout"], 30.0, 7200.0),
            "selected": as_str(raw, "selected", base["selected"]),
            "lastBrowseDir": as_str(raw, "lastBrowseDir", base["lastBrowseDir"]),
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

    # ---------------- 预设清单 ----------------

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
        # 老清单是单个 imagePath/target，读进来就折成一条 items
        items = _clean_items(raw.get("items"),
                             raw.get("imagePath", ""), raw.get("target", ""))
        return {
            "id": pid,
            "name": str(raw.get("name") or "").strip()[:60] or "未命名",
            "items": items,
            "pushDir": str(raw.get("pushDir") or "/mnt").strip() or "/mnt",
            "reboot": bool(raw.get("reboot", True)),
            "addedAt": raw.get("addedAt"),
            "lastRunAt": raw.get("lastRunAt"),
            "lastExitCode": raw.get("lastExitCode"),
            "lastDuration": raw.get("lastDuration"),
        }

    def _save_presets(self):
        """原子写：先写临时文件再 os.replace，中途断电不会留下半个 JSON。"""
        path = self.manifest_path()
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
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

    # ---------------- 给前端的状态 ----------------

    def extra(self):
        with self.lock:
            presets = [dict(p) for p in self.presets]
            selected = self.config.get("selected", "")
            running_id = self._running_id
            devices = list(self.devices)
            devices_error = self.devices_error
            last_dir = self.config.get("lastBrowseDir", "")
        if selected and not any(p["id"] == selected for p in presets):
            selected = presets[0]["id"] if presets else ""
        return {
            "presets": presets,
            "selected": selected,
            "runningId": running_id,
            "devices": devices,
            "devicesError": devices_error,
            "lastBrowseDir": last_dir,
        }

    def brief(self):
        stats = self.stats
        line = "%d 个预设 · 已烧写 %d 次" % (len(self.presets), stats.get("runs", 0))
        if stats.get("failures"):
            line += " · 失败 %d 次" % stats["failures"]
        return line

    # ---------------- 设备 ----------------

    def refresh_devices(self):
        cfg = self.config
        devices, err = adbkit.list_devices(cfg["adbPath"], cfg["serial"], 10.0)
        with self.lock:
            self.devices = devices
            self.devices_error = err
            self.stats["deviceCount"] = len(devices)
        return devices, err

    def _resolve_serial(self, devices):
        """确定这次写哪台设备。返回 (serial, 错误说明)。"""
        serial, err = adbkit.pick_serial(
            devices, self.config["serial"], self.devices_error)
        if not serial:
            return "", err
        # 显式指定了但设备处于 unauthorized/offline 时，别硬来
        for dev in devices:
            if dev["serial"] == serial and dev["state"] != "device":
                return "", "设备 %s 当前是 %s，不能写入" % (serial, dev["state"])
        return serial, ""

    # ---------------- 本工具自己的 POST 动作 ----------------

    def api(self, action, raw, data, query):
        if action == "presets/save":
            return self._api_save(data)
        if action == "presets/delete":
            return self._api_delete(data)
        if action == "fs/list":
            return self._api_list(data)
        if action == "fs/stat":
            return self._api_stat(data)
        return None

    def _api_save(self, data):
        data = data if isinstance(data, dict) else {}
        pid = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()[:60]
        push_dir = str(data.get("pushDir") or "").strip() or "/mnt"
        reboot = bool(data.get("reboot", True))

        # 老格式（单个 imagePath/target）也认，方便已有调用方
        items, error = parse_items(data.get("items"),
                                   data.get("imagePath", ""), data.get("target", ""))
        if error:
            return {"ok": False, "error": error}, 400
        for item in items:
            item["imagePath"] = os.path.abspath(
                os.path.expanduser(item["imagePath"]))

        first_image = items[0]["imagePath"]
        if not name:
            # 名称只是列表里的标签：没填就借第一个镜像的文件名
            name = os.path.splitext(os.path.basename(first_image))[0].strip()[:60]
        if not name:
            return {"ok": False, "error": "这个镜像没有文件名，给预设起个名字吧"}, 400
        if not valid_remote_dir(push_dir):
            return {"ok": False,
                    "error": "推送目录必须是 / 开头的绝对路径，比如 /mnt"}, 400
        push_dir = push_dir.rstrip("/") or "/"
        if push_dir == "/":
            return {"ok": False, "error": "推送目录不能是根目录"}, 400

        with self.lock:
            entry = self._find(pid) if pid else None
            if pid and entry is None:
                return {"ok": False, "error": "这个预设已经不在了，刷新一下"}, 404
            if entry is None:
                entry = {
                    "id": _new_id(),
                    "addedAt": time.time(),
                    "lastRunAt": None,
                    "lastExitCode": None,
                    "lastDuration": None,
                }
                self.presets.append(entry)
            entry.update({
                "name": name,
                "items": items,
                "pushDir": push_dir,
                "reboot": reboot,
            })
            # 老字段留着只会让两边各读各的，保存一次就清掉
            entry.pop("imagePath", None)
            entry.pop("target", None)

            try:
                self._save_presets()
                saved = True
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

        total_targets = sum(len(item["targets"]) for item in items)
        self.log("info", "预设已保存: %s（%d 个镜像 / %d 个分区）"
                 % (name, len(items), total_targets))
        self.publish_state()
        return {
            "ok": True,
            "saved": saved,
            "preset": result,
            "presets": presets,
            "exists": all(os.path.isfile(item["imagePath"]) for item in items),
        }, 200

    def _api_delete(self, data):
        data = data if isinstance(data, dict) else {}
        pid = str(data.get("id") or "")

        with self.lock:
            entry = self._find(pid)
            if entry is None:
                return {"ok": False, "error": "没有这个预设"}, 404
            if self._running_id == pid:
                return {"ok": False, "error": "这个预设正在烧写，先停止再删除"}, 409

            self.presets.remove(entry)
            try:
                self._save_presets()
            except (IOError, OSError) as exc:
                self.log("warn", "预设清单写入失败: %s" % exc)

            if self.config.get("selected") == pid:
                self.config["selected"] = self.presets[0]["id"] if self.presets else ""
                try:
                    self.save_config()
                except (IOError, OSError):
                    pass

        self.log("info", "已删除预设 %s" % entry["name"])
        self.publish_state()
        return {"ok": True}, 200

    # ---------------- 镜像浏览器 ----------------

    def _api_list(self, data):
        """列目录给页面上的文件浏览器用。

        它能把本机任意目录列出来 —— 这是「只记录路径、不复制镜像」的代价。
        服务默认只监听 127.0.0.1；用 --host 0.0.0.0 时整站本来就无鉴权，
        启动时已经提示过风险。
        """
        data = data if isinstance(data, dict) else {}
        raw_path = str(data.get("path") or "").strip()
        # 空路径 = 主目录。页面上的「主目录」按钮就靠这个，
        # 不拿 lastBrowseDir 兜底，否则「主目录」会跳回上次的目录。
        if not raw_path:
            raw_path = os.path.expanduser("~")
        path = os.path.abspath(os.path.expanduser(raw_path))

        if not os.path.isdir(path):
            # 传了个文件或已不存在的路径：退回它的父目录，别让界面卡在空列表
            parent = os.path.dirname(path)
            if os.path.isdir(parent):
                path = parent
            else:
                return {"ok": False, "error": "目录不存在: %s" % path, "path": path}, 200

        try:
            names = sorted(os.listdir(path), key=lambda s: s.lower())
        except OSError as exc:
            return {"ok": False, "error": "读不了这个目录: %s" % exc, "path": path}, 200

        show_all = bool(data.get("all"))
        dirs, files = [], []
        truncated = len(names) > 2000
        for name in names[:2000]:
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full})
                elif os.path.isfile(full):
                    if not show_all and os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                        continue
                    files.append({
                        "name": name,
                        "path": full,
                        "size": os.path.getsize(full),
                    })
            except OSError:
                continue

        parent = os.path.dirname(path)
        if parent == path:              # 已经到根目录（Windows 的盘符也是这种）
            parent = ""

        with self.lock:
            self.config["lastBrowseDir"] = path

        return {
            "ok": True,
            "path": path,
            "parent": parent,
            "dirs": dirs,
            "files": files,
            "truncated": truncated,
        }, 200

    def _api_stat(self, data):
        data = data if isinstance(data, dict) else {}
        raw_path = str(data.get("path") or "").strip()
        if not raw_path:
            return {"ok": True, "exists": False}, 200
        path = os.path.abspath(os.path.expanduser(raw_path))
        if not os.path.exists(path):
            return {"ok": True, "exists": False, "path": path}, 200
        is_file = os.path.isfile(path)
        size = 0
        if is_file:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        return {
            "ok": True,
            "exists": True,
            "isFile": is_file,
            "size": size,
            "path": path,
        }, 200

    # ---------------- 停止 ----------------

    def on_stop(self):
        with self.lock:
            proc = self.process
            stage = self._stage
        if proc is not None and proc.poll() is None:
            self.log("info", "正在结束 adb（pid %s）..." % proc.pid)
            try:
                proc.terminate()
            except OSError as exc:
                self.log("warn", "结束 adb 进程失败: %s" % exc)

        # 说清楚停下来意味着什么，别让人以为分区一定完好
        if stage == "dd":
            self.log("warn", "停止时正在 dd：分区可能只写了一部分，不会自动重启")
        elif stage == "push":
            self.log("warn", "停止时正在推送，分区没有被改动")
        elif stage == "sync":
            self.log("warn", "停止时正在同步缓存，不会执行重启")
        elif stage == "reboot":
            self.log("warn", "重启指令已经发出，设备仍然会重启")
        else:
            self.log("warn", "已停止，没有写入分区")

    # ---------------- 主流程 ----------------

    def run_loop(self):
        cfg = dict(self.config)
        pid = str(self.start_options.get("presetId") or cfg.get("selected") or "")

        with self.lock:
            entry = self._find(pid)
            if entry is None:
                self.log("err", "没有可执行的预设，先在左边选一个")
                return
            preset = dict(entry)
            self._running_id = preset["id"]
        # 让列表马上把这一行标成「烧写中」，不用等任务结束
        self.publish_state()

        started = time.time()
        rc = None
        outcome = "fail"
        try:
            rc, outcome = self._flash(cfg, preset)
        finally:
            elapsed = time.time() - started
            with self.lock:
                entry = self._find(preset["id"])
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
                self._stage = "idle"
                self._save_presets_quiet()
            self.publish_state()

    def _flash(self, cfg, preset):
        """按顺序推送、校验、dd、sync、重启。返回 (最后一条命令的返回码, 结果)。"""
        push_dir = preset.get("pushDir") or ""
        items = preset.get("items") or []

        # ---- 先校验参数，一条命令都别发 ----
        if not valid_remote_dir(push_dir) or push_dir.rstrip("/") in ("", "/"):
            self.log("err", "推送目录不合法: %r" % push_dir)
            return None, "fail"
        if not items:
            self.log("err", "这个预设里没有镜像")
            return None, "fail"

        plans = []          # [(image, size, [target, ...])]
        sizes = {}
        seen = {}           # target -> image，挡住两个镜像抢同一个分区
        total = 0
        for idx, item in enumerate(items, 1):
            raw_image = str(item.get("imagePath") or "").strip()
            if not raw_image:
                self.log("err", "第 %d 个镜像没有选文件" % idx)
                return None, "fail"
            image = os.path.abspath(os.path.expanduser(raw_image))
            if image not in sizes:
                if not os.path.isfile(image):
                    self.log("err", "镜像文件不存在: %s" % image)
                    return None, "fail"
                try:
                    size = os.path.getsize(image)
                except OSError as exc:
                    self.log("err", "读不了镜像文件: %s（%s）" % (image, exc))
                    return None, "fail"
                if size <= 0:
                    self.log("err", "镜像是空文件: %s" % image)
                    return None, "fail"
                sizes[image] = size
            targets = list(item.get("targets") or [])
            if not targets:
                self.log("err", "第 %d 个镜像没有写写入目标: %s"
                         % (idx, os.path.basename(image)))
                return None, "fail"
            for target in targets:
                if not valid_target(target):
                    self.log("err", "写入目标不合法: %r（必须是 /dev/ 下的路径）" % target)
                    return None, "fail"
                other = seen.get(target)
                if other is not None and other != image:
                    self.log("err", "写入目标 %s 同时给了 %s 和 %s"
                             % (target, os.path.basename(other), os.path.basename(image)))
                    return None, "fail"
                seen[target] = image
            plans.append((image, sizes[image], targets))
            total += len(targets)

        self.log("info", "准备写入 %d 个镜像到 %d 个分区：" % (len(plans), total))
        for image, size, targets in plans:
            self.log("info", "  %s（%s）→ %s" % (
                os.path.basename(image), human_size(size), "、".join(targets)))

        # ---- 选设备 ----
        devices, _ = self.refresh_devices()
        serial, err = self._resolve_serial(devices)
        if not serial:
            self.log("err", err)
            return None, "fail"
        self.log("info", "目标设备 %s，推送目录 %s" % (serial, push_dir))

        # ---- 闸一/二：所有目标先验一遍，任何一项不过就一条 dd 都不发 ----
        # A/B 分区尤其要先验全：写了一半才发现 boot_b 不存在，比不写更糟。
        pair_no = 0
        for image, size, targets in plans:
            for target in targets:
                pair_no += 1
                check = "test -b %s && echo ATS_BLOCK || echo ATS_NOBLOCK" % target
                rc, text = self._run_short(cfg, serial, check, CHECK_TIMEOUT)
                if "ATS_BLOCK" not in (text or ""):
                    self.log("err", "目标 %s 不是块设备或不可访问，没有写入任何分区" % target)
                    return rc, "fail"

                rc, text = self._run_short(
                    cfg, serial, "blockdev --getsize64 %s" % target, CHECK_TIMEOUT)
                match = re.match(r"\s*(\d+)\s*$", text or "")
                if match:
                    block_size = int(match.group(1))
                    if block_size > 0 and size > block_size:
                        self.log("err", "镜像 %s 大于分区 %s（%s > %s），没有写入任何分区"
                                 % (os.path.basename(image), target,
                                    human_size(size), human_size(block_size)))
                        return rc, "fail"
                    self.log("info", "目标 %d/%d 可用：%s（分区 %s）"
                             % (pair_no, total, target, human_size(block_size)))
                else:
                    self.log("warn", "目标 %d/%d 拿不到分区大小（设备上没有 blockdev？），"
                             "跳过大小检查" % (pair_no, total))

                if self.stop_event.is_set():
                    self.log("warn", "已停止，还没有写入任何分区")
                    return None, "stopped"

        # ---- 逐个镜像推送，再逐个分区写入 ----
        written = 0
        multi = len(plans) > 1
        for n, (image, size, targets) in enumerate(plans, 1):
            remote = push_dir.rstrip("/") + "/" + remote_name(
                image, preset["id"], n if multi else 0)
            self._set_stage("push")
            self.log("info", "推送 %d/%d：%s" % (n, len(plans), os.path.basename(image)))
            rc, outcome, _ = self._exec(
                cfg, serial, ["push", image, remote], cfg["timeout"], "推送镜像")
            if outcome == "stopped":
                self.log("warn", "已停止：已写入 %d/%d 个分区，没有重启" % (written, total))
                return rc, "stopped"
            if outcome != "ok":
                self.log("err", "推送失败，%s" % self._after_failure(written, total))
                return rc, "fail"

            # ---- 闸三：设备上的文件大小必须和本地一致 ----
            rc, text = self._run_short(cfg, serial, "wc -c %s" % remote, CHECK_TIMEOUT)
            match = re.search(r"(\d+)", text or "")
            if match:
                remote_size = int(match.group(1))
                if remote_size != size:
                    self.log("err", "推送后大小不一致（本地 %d 字节 / 设备 %d 字节），%s"
                             % (size, remote_size, self._after_failure(written, total)))
                    return rc, "fail"
                self.log("ok", "已推送 %s，大小校验一致" % remote)
            else:
                low = (text or "").lower()
                for bad in ("no such file", "not found", "permission denied"):
                    if bad in low:
                        self.log("err", "设备上读不到推送的文件，%s: %s"
                                 % (self._after_failure(written, total),
                                    (text or "").strip()[:120]))
                        return rc, "fail"
                self.log("warn", "读不到设备上文件的大小，跳过校验: %s"
                         % (text or "").strip()[:120])

            for target in targets:
                if self.stop_event.is_set():
                    self.log("warn", "已停止：已写入 %d/%d 个分区，没有重启"
                             % (written, total))
                    return None, "stopped"
                self._set_stage("dd")
                self.log("info", "写入 %d/%d：%s → %s"
                         % (written + 1, total, os.path.basename(image), target))
                dd_cmd = "dd if=%s of=%s bs=%d" % (remote, target, DD_BS)
                rc, outcome, text = self._exec(
                    cfg, serial, ["shell", dd_cmd], cfg["timeout"], "写入分区")
                if outcome == "stopped":
                    self.log("warn", "dd 被中断：%s 可能只写了一部分；已写入 %d/%d 个分区，"
                             "没有重启" % (target, written, total))
                    return rc, "stopped"
                if outcome != "ok":
                    self.log("err", "dd 失败（退出码 %s），%s"
                             % (rc, self._after_failure(written, total)))
                    return rc, "fail"
                low = (text or "").lower()
                hit = next((bad for bad in _DD_BAD if bad in low), None)
                if hit:
                    self.log("err", "dd 输出里出现「%s」，按失败处理，%s"
                             % (hit, self._after_failure(written, total)))
                    return rc, "fail"
                written += 1
                self.log("ok", "已写入 %d/%d：%s" % (written, total, target))

        # 这里停下来的话分区已经写完，但用户明确点了停止，就别替他重启
        if self.stop_event.is_set():
            self.log("warn", "已停止：%d 个分区已经写完，没有执行 sync 和重启" % written)
            return None, "stopped"

        # ---- 落盘 ----
        self._set_stage("sync")
        rc, outcome, _ = self._exec(cfg, serial, ["shell", "sync"], SYNC_TIMEOUT, "同步缓存")
        if outcome != "ok":
            # dd 已经写完了，数据在页缓存里；reboot 前内核自己也会 sync 一次，
            # 所以这里不拦重启，只提醒一句。
            self.log("warn", "sync 没有正常结束，继续重启（内核重启前还会再同步一次）")

        if self.stop_event.is_set():
            self.log("warn", "已停止：%d 个分区已经写完，没有重启" % written)
            return rc, "stopped"

        # ---- 重启 ----
        if preset.get("reboot", True):
            self._set_stage("reboot")
            reboot_cmd = adbkit.build_command(cfg["adbPath"], serial, ["shell", "reboot"])
            self.log("cmd", "$ " + shell_join(reboot_cmd))
            rc, text = adbkit.run(cfg["adbPath"], serial, ["shell", "reboot"], REBOOT_TIMEOUT)
            text = (text or "").strip()
            if text:
                self.log("info", text)
            if rc != 0 and text:
                self.log("warn", "重启命令退出码 %s，确认设备是不是真的重启了" % rc)
            else:
                self.log("ok", "重启指令已发出，设备随即断开")
        else:
            self.log("ok", "写入完成（预设里没勾选重启）")

        self.log("ok", "烧写完成：%d 个镜像 → %d 个分区" % (len(plans), total))
        return rc, "ok"

    @staticmethod
    def _after_failure(written, total):
        """失败时把「已经写进去几个」说清楚，别让人以为分区一定完好。"""
        if not written:
            return "分区没有改动，不会重启"
        return "已写入的 %d/%d 个分区不会回滚，不会重启" % (written, total)

    def _set_stage(self, stage):
        with self.lock:
            self._stage = stage

    # ---------------- 子进程 ----------------

    def _run_short(self, cfg, serial, command, timeout):
        """跑一条短命令，输出直接进日志。返回 (返回码, 输出)。"""
        cmd = adbkit.build_command(cfg["adbPath"], serial, ["shell", command])
        rc, out = adbkit.run(cfg["adbPath"], serial, ["shell", command], timeout)
        text = (out or "").strip()
        if text:
            self.log("info", text)
        return rc, text

    def _exec(self, cfg, serial, args, timeout, label):
        """跑一条长命令（push / dd / sync），输出边跑边进日志。

        不用 adbkit.run 是因为它一阻塞就没法响应停止，而且拿不到中间输出。
        返回 (返回码, ok | fail | stopped, 累计输出)。
        """
        t0 = time.time()
        cmd = adbkit.build_command(cfg["adbPath"], serial, args)
        self.log("cmd", "$ " + shell_join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                **spawn.quiet()          # 桌面版没控制台，不加就会闪黑框
            )
        except FileNotFoundError:
            self.log("err", "找不到 adb 可执行文件: %s" % cfg["adbPath"])
            return None, "fail", ""
        except OSError as exc:
            self.log("err", "无法启动 adb: %s" % exc)
            return None, "fail", ""

        with self.lock:
            self.process = proc

        chunks = []
        reader = threading.Thread(target=self._pump, args=(proc, chunks), name="image-out")
        reader.daemon = True
        reader.start()

        deadline = t0 + timeout
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
                self.log("err", "%s超时（%.0f 秒），已结束 adb" % (label, timeout))
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
        text = "".join(chunks)

        if timed_out:
            return rc, "fail", text
        if self.stop_event.is_set():
            self.log("warn", "%s已停止（用时 %.1f 秒）" % (label, elapsed))
            return rc, "stopped", text
        if rc == 0:
            self.log("ok", "%s完成，用时 %.1f 秒" % (label, elapsed))
            return rc, "ok", text
        self.log("err", "%s失败，退出码 %s（用时 %.1f 秒）" % (label, rc, elapsed))
        return rc, "fail", text

    def _pump(self, proc, chunks):
        """把子进程输出按行推进日志。

        不能直接用 readline：adb push 和某些 dd 会用 \\r 原地刷新进度，
        没有 \\n 时 readline 会一直等到命令结束，进度就变成一条巨型字符串。
        所以 \\r 和 \\n 都当换行，\\r 那种最多每 0.4 秒打一行。
        """
        pending = b""
        last_emit = 0.0
        while True:
            try:
                chunk = os.read(proc.stdout.fileno(), 4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            pending += chunk
            while True:
                match = _EOL.search(pending)
                if match is None:
                    break
                raw = pending[:match.start()]
                is_cr = match.group(0) == b"\r"
                pending = pending[match.end():]
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if len(chunks) < 4000:
                    chunks.append(line + "\n")
                now = time.time()
                if not is_cr or now - last_emit > 0.4:
                    self.log("info", line)
                    last_emit = now
        line = pending.decode("utf-8", "replace").strip()
        if line:
            if len(chunks) < 4000:
                chunks.append(line + "\n")
            self.log("info", line)
        try:
            proc.stdout.close()
        except (OSError, AttributeError):
            pass


_EOL = re.compile(b"\r\n|\r|\n")
