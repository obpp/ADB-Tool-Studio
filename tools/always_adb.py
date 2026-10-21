# -*- coding: utf-8 -*-
"""always-adb：force_adb 常驻设置循环（原 _always_adb.bat 的 Web 版）。

流程和 bat 一致：设置 force_adb → 成功了就 root + sync → 等一会儿 → 再来一轮；
设置失败就按固定间隔重试。
"""

import time

import adbkit
from toolbase import (Tool, as_bool, as_num, as_str)


class AlwaysAdbTool(Tool):

    id = "always_adb"
    name = "always-adb"
    desc = "反复把 force_adb 设为 1，成功后 root + sync，隔一段时间再来一轮"
    page = "always-adb"
    icon = "∞"
    # 失败重试时每秒就有几条日志，留 500 条会让打开页面时重放很久；
    # 200 条够看出最近发生了什么，失败次数另有 stats 统计，不会丢。
    history_limit = 200

    def __init__(self, app_dir):
        self.devices = []
        self.devices_error = ""
        Tool.__init__(self, app_dir)

    # ---------------- 配置 ----------------

    def default_config(self):
        return {
            "adbPath": adbkit.resolve_adb(),
            "serial": "",
            "forceAdbValue": "1",
            "retryInterval": 1.0,
            "waitSeconds": 10.0,
            "commandTimeout": 30.0,
            "waitAfterRoot": True,
            "doRoot": True,
            "doSync": True,
        }

    def sanitize_config(self, raw, base=None):
        base = dict(base) if isinstance(base, dict) else self.default_config()
        raw = raw if isinstance(raw, dict) else {}
        return {
            "adbPath": as_str(raw, "adbPath", base["adbPath"], nonempty=True),
            "serial": as_str(raw, "serial", base["serial"]),
            "forceAdbValue": as_str(raw, "forceAdbValue", base["forceAdbValue"], nonempty=True),
            "retryInterval": as_num(raw, "retryInterval", base["retryInterval"], 0.1, 600.0),
            "waitSeconds": as_num(raw, "waitSeconds", base["waitSeconds"], 0.0, 86400.0),
            "commandTimeout": as_num(raw, "commandTimeout", base["commandTimeout"], 1.0, 3600.0),
            "waitAfterRoot": as_bool(raw, "waitAfterRoot", base["waitAfterRoot"]),
            "doRoot": as_bool(raw, "doRoot", base["doRoot"]),
            "doSync": as_bool(raw, "doSync", base["doSync"]),
        }

    def default_stats(self):
        return {
            "rounds": 0,             # 完整跑完 root+sync 的轮数
            "attempts": 0,           # settings put 的总尝试次数
            "retries": 0,            # 其中失败的次数
            "consecutiveFailures": 0,
            "lastSuccessAt": None,
            "lastError": "",
            "deviceCount": 0,
        }

    # ---------------- 设备 ----------------

    def extra(self):
        if self.status == "idle":
            mode = None
        else:
            mode = "once" if self.start_options.get("once") else "loop"
        return {
            "devices": list(self.devices),
            "devicesError": self.devices_error,
            "mode": mode,
        }

    def brief(self):
        stats = self.stats
        line = "已完成 %d 轮 · 重试 %d 次" % (stats.get("rounds", 0), stats.get("retries", 0))
        if stats.get("consecutiveFailures"):
            line += " · 当前连续失败 %d 次" % stats["consecutiveFailures"]
        return line

    def refresh_devices(self):
        cfg = self.config
        devices, err = adbkit.list_devices(
            cfg["adbPath"], cfg["serial"], min(10.0, cfg["commandTimeout"]))
        with self.lock:
            self.devices = devices
            self.devices_error = err
            self.stats["deviceCount"] = len(devices)
        return devices, err

    def target_hint(self):
        if self.config["serial"]:
            return self.config["serial"]
        online = [d for d in self.devices if d["state"] == "device"]
        if len(online) == 1:
            return online[0]["serial"] + "（自动识别）"
        if not online:
            return "（无在线设备）"
        return "（%d 台在线设备，未指定序列号，adb 会报错）" % len(online)

    # ---------------- adb 调用 ----------------

    def adb(self, args, log_cmd=True):
        cfg = self.config
        if log_cmd:
            self.log("cmd", "$ " + " ".join(
                adbkit.build_command(cfg["adbPath"], cfg["serial"], args)))
        return adbkit.run(cfg["adbPath"], cfg["serial"], args, cfg["commandTimeout"])

    # ---------------- 主循环 ----------------

    def run_loop(self):
        cfg = dict(self.config)
        once = bool(self.start_options.get("once"))

        if cfg["doRoot"] or cfg["doSync"]:
            devices, err = self.refresh_devices()
            self.log("info", "目标设备: %s" % self.target_hint())
            if not devices and err:
                self.log("warn", "读取设备列表失败: %s" % err)

        while not self.stop_event.is_set():
            self.log("info", "======================================")
            self.log("info", "等待ADB服务就绪并准备设置 force_adb...")

            if not self._retry_force_adb(cfg):
                return

            self.log("info", "开始执行烧录相关操作...")

            if cfg["doRoot"]:
                self.log("info", "1. 切换到root权限...")
                rc, out = self.adb(["root"])
                text = out.strip()
                if text:
                    self.log("info", text)
                if rc != 0:
                    self.log("warn", "[警告] 获取root权限失败，尝试继续...")
                elif "cannot run as root" in out.lower():
                    # adb 这里返回 0，但 adbd 并没有真的以 root 运行
                    self.log("warn", "[警告] adbd 无法以 root 运行（production build），后续命令可能被拒绝")
                if cfg["waitAfterRoot"]:
                    self.log("info", "2. 等待设备在 root 重启 adbd 后重新上线...")
                    rc, out = self.adb(["wait-for-device"])
                    if rc == 0:
                        self.log("ok", "设备已就绪")
                    elif self.stop_event.is_set():
                        return
                    else:
                        self.log("warn", "[警告] 等待设备就绪失败: %s" % (out.strip() or rc))
            else:
                self.log("info", "1. 已跳过 root 步骤")

            if self.stop_event.is_set():
                return

            if cfg["doSync"]:
                self.log("info", "3. 执行 sync...")
                rc, out = self.adb(["shell", "sync"])
                text = out.strip()
                if text:
                    self.log("info", text)
                if adbkit.problem(rc, out) is None:
                    self.log("ok", "sync 完成")
                else:
                    self.log("warn", "[警告] sync 返回异常: %s" % text)
            else:
                self.log("info", "3. 已跳过 sync 步骤")

            self.bump("rounds")
            self.publish_state()

            self.log("info", "4. 操作完成，等待设备重启...")
            if once:
                self.log("info", "单次执行结束")
                return
            self.log("info", "等待 %.0f 秒后准备下一轮操作..." % cfg["waitSeconds"])
            if self.interruptible_sleep(cfg["waitSeconds"]):
                return

    def _retry_force_adb(self, cfg):
        """对应 bat 的 :RETRY_FORCE_ADB。设置成功返回 True，被停止返回 False。"""
        while not self.stop_event.is_set():
            self.bump("attempts")
            rc, out = self.adb(
                ["shell", "settings", "put", "global", "force_adb", cfg["forceAdbValue"]])
            problem = adbkit.problem(rc, out)

            if problem is None:
                with self.lock:
                    self.stats["consecutiveFailures"] = 0
                    self.stats["lastSuccessAt"] = time.time()
                self.log("ok", "[成功] force_adb 已设置为 %s" % cfg["forceAdbValue"])
                self.publish_state()
                return True

            with self.lock:
                self.stats["retries"] += 1
                self.stats["consecutiveFailures"] += 1
                self.stats["lastError"] = problem
                fails = self.stats["consecutiveFailures"]
            self.log("err", "[失败] 尝试设置失败: %s" % problem)
            # 第 3 次提醒，之后每 30 次再提醒一遍，别把日志刷满
            if fails == 3 or (fails > 3 and fails % 30 == 0):
                self.log("warn", "已连续失败 %d 次，请检查数据线、USB 调试授权（adb devices）" % fails)
            self.log("info", "等待 %.1f 秒后重试..." % cfg["retryInterval"])
            self.publish_state()
            if self.interruptible_sleep(cfg["retryInterval"]):
                return False
        return False
