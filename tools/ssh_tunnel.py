# -*- coding: utf-8 -*-
"""adb 反向隧道：把本机 adb server 反向映射到远程主机。

等价于这条命令：

    ssh -R 5037:127.0.0.1:5037 -N zhangshijin2@10.27.12.198

隧道建立后，在远程主机上直接敲 `adb devices` 就能看到接在本机的设备
（远程的 adb 客户端默认就连 127.0.0.1:5037）。
"""

import os
import shutil
import subprocess
import threading
import time

import spawn
from toolbase import (Tool, STATUS_RUNNING, STATUS_RECONNECTING,
                      as_bool, as_enum, as_int, as_num, as_str, shell_join)

HOST_KEY_POLICIES = ("accept-new", "yes", "no")

# ssh 的常见报错 -> 一句人话提示（每条只提示一次，别刷屏）
HINTS = (
    ("permission denied",
     "认证被拒绝：确认远程 ~/.ssh/authorized_keys 里有本机公钥，或本机已 ssh-add 加载密钥"),
    ("host key verification failed",
     "主机指纹校验失败：把「主机指纹」改成「自动接受新的」，或先手动 ssh 一次写入 known_hosts"),
    ("remote port forwarding failed",
     "远程端口转发被拒：远程该端口可能已被占用；若要绑到 0.0.0.0 还需远程 sshd 开启 GatewayPorts"),
    ("address already in use",
     "端口已被占用：远程该端口上已经有别的进程在监听"),
    ("connection refused",
     "连接被拒绝：主机名或端口不对，或者远程 sshd 没在运行"),
    ("connection timed out",
     "连接超时：网络不通，或被防火墙拦了"),
    ("no route to host", "网络不可达：检查 IP 和路由"),
    ("could not resolve hostname", "域名解析失败：主机名写错了吧"),
    ("connection closed by", "连接被远程关闭：可能是密钥被拒，或远程 sshd 有限制"),
    ("too many authentication failures", "认证失败次数过多：用 -i 指定密钥，并加上 IdentitiesOnly"),
)


def resolve_ssh():
    """找一个能用的 ssh 客户端（Windows 10+ 自带 OpenSSH）。"""
    candidates = ["ssh", "ssh.exe",
                  "C:\\Windows\\System32\\OpenSSH\\ssh.exe",
                  "/usr/bin/ssh", "/usr/local/bin/ssh"]
    for cand in candidates:
        found = shutil.which(cand)
        if found:
            return found
        if ("/" in cand or "\\" in cand) and os.path.isfile(cand):
            return cand
    return "ssh"


def build_ssh_command(cfg):
    """按配置拼出 ssh 命令行（list 形式，直接交给 subprocess）。"""
    host = cfg["host"]
    user = cfg["user"]
    target = ("%s@%s" % (user, host)) if user else host

    if cfg["gatewayPorts"]:
        spec = "0.0.0.0:%d:%s:%d" % (cfg["remotePort"], cfg["localHost"], cfg["localPort"])
    else:
        # 不写绑定地址时，远程只在 loopback 上监听（更安全，也是 ssh 的默认）
        spec = "%d:%s:%d" % (cfg["remotePort"], cfg["localHost"], cfg["localPort"])

    cmd = [cfg["sshPath"], "-N",
           "-p", str(cfg["sshPort"]),
           "-R", spec,
           "-o", "ExitOnForwardFailure=yes",
           "-o", "StrictHostKeyChecking=" + cfg["hostKeyPolicy"]]

    if cfg["keepAlive"]:
        cmd += ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    if cfg["batchMode"]:
        # 没有终端，密码认证没法交互，直接快速失败而不是傻等
        cmd += ["-o", "BatchMode=yes"]
    if cfg["identityFile"]:
        cmd += ["-i", cfg["identityFile"], "-o", "IdentitiesOnly=yes"]

    cmd.append(target)
    return cmd


class SshTunnelTool(Tool):

    id = "ssh_tunnel"
    name = "adb 反向隧道"
    desc = "把本机 adb server 反向映射到远程主机，远程即可直接操作接在本机的设备"
    page = "ssh-tunnel"
    icon = "⇄"

    # ---------------- 配置 ----------------

    def default_config(self):
        return {
            "sshPath": resolve_ssh(),
            "host": "10.27.12.198",
            "user": "zhangshijin2",
            "sshPort": 22,
            "identityFile": "",
            "hostKeyPolicy": "accept-new",
            "keepAlive": True,
            "batchMode": True,
            "remotePort": 5037,
            "localHost": "127.0.0.1",
            "localPort": 5037,
            "gatewayPorts": False,
            "autoReconnect": True,
            "reconnectInterval": 3.0,
        }

    def sanitize_config(self, raw, base=None):
        base = dict(base) if isinstance(base, dict) else self.default_config()
        raw = raw if isinstance(raw, dict) else {}
        return {
            "sshPath": as_str(raw, "sshPath", base["sshPath"], nonempty=True),
            "host": as_str(raw, "host", base["host"]),
            "user": as_str(raw, "user", base["user"]),
            "sshPort": as_int(raw, "sshPort", base["sshPort"], 1, 65535),
            "identityFile": as_str(raw, "identityFile", base["identityFile"]),
            "hostKeyPolicy": as_enum(raw, "hostKeyPolicy", base["hostKeyPolicy"], HOST_KEY_POLICIES),
            "keepAlive": as_bool(raw, "keepAlive", base["keepAlive"]),
            "batchMode": as_bool(raw, "batchMode", base["batchMode"]),
            "remotePort": as_int(raw, "remotePort", base["remotePort"], 1, 65535),
            "localHost": as_str(raw, "localHost", base["localHost"], nonempty=True),
            "localPort": as_int(raw, "localPort", base["localPort"], 1, 65535),
            "gatewayPorts": as_bool(raw, "gatewayPorts", base["gatewayPorts"]),
            "autoReconnect": as_bool(raw, "autoReconnect", base["autoReconnect"]),
            "reconnectInterval": as_num(raw, "reconnectInterval", base["reconnectInterval"], 0.5, 3600.0),
        }

    def default_stats(self):
        return {
            "connects": 0,           # ssh 进程被拉起的次数
            "failures": 0,           # 非 0 退出的次数
            "lastConnectedAt": None,
            "lastExitCode": None,
            "lastError": "",
        }

    # ---------------- 状态 ----------------

    def remote_spec(self, cfg=None):
        cfg = cfg or self.config
        return "%s:%d" % (cfg["host"], cfg["remotePort"])

    def local_spec(self, cfg=None):
        cfg = cfg or self.config
        return "%s:%d" % (cfg["localHost"], cfg["localPort"])

    def connected(self):
        proc = self.process
        return bool(self.status == STATUS_RUNNING and proc is not None
                    and proc.poll() is None)

    def extra(self):
        cfg = self.config
        return {
            "remoteSpec": self.remote_spec(cfg),
            "localSpec": self.local_spec(cfg),
            "target": ("%s@%s" % (cfg["user"], cfg["host"])) if cfg["user"] else cfg["host"],
            "connected": self.connected(),
            "command": shell_join(build_ssh_command(cfg)),
        }

    def brief(self):
        cfg = self.config
        if not cfg["host"]:
            return "还没填远程主机"
        line = "%s → %s" % (self.remote_spec(cfg), self.local_spec(cfg))
        if self.connected():
            line += " · 已连通"
        return line

    def preview(self, params):
        """POST /api/ssh_tunnel/preview：把页面上的当前参数变成一条命令行。"""
        merged = self.sanitize_config(params, base=self.config)
        cmd = build_ssh_command(merged)
        warn = ""
        if not merged["host"]:
            warn = "还没有填远程主机"
        return {"argv": cmd, "shell": shell_join(cmd), "warning": warn}

    # ---------------- 启停 ----------------

    def on_stop(self):
        proc = self.process
        if proc is not None and proc.poll() is None:
            self.log("info", "正在断开 ssh（pid %s）..." % proc.pid)
            try:
                proc.terminate()
            except OSError as exc:
                self.log("warn", "结束 ssh 进程失败: %s" % exc)

    # ---------------- 主循环 ----------------

    def run_loop(self):
        cfg = dict(self.config)
        delay = 0.0

        while not self.stop_event.is_set():
            if delay:
                self.log("info", "%.1f 秒后重连..." % delay)
                self.set_status(STATUS_RECONNECTING)
                if self.interruptible_sleep(delay):
                    return
                delay = 0.0

            cmd = build_ssh_command(cfg)
            self.log("cmd", "$ " + shell_join(cmd))

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    universal_newlines=True,
                    **spawn.quiet()          # 桌面版没控制台，不加就会闪黑框
                )
            except FileNotFoundError:
                self.log("err", "找不到 ssh 可执行文件: %s" % cfg["sshPath"])
                self.set_stat("lastError", "找不到 ssh 可执行文件")
                return
            except OSError as exc:
                self.log("err", "无法启动 ssh: %s" % exc)
                self.set_stat("lastError", str(exc))
                return

            with self.lock:
                self.process = proc
                self.stats["connects"] += 1
                self.stats["lastConnectedAt"] = time.time()
            self.set_status(STATUS_RUNNING)
            self.log("ok", "ssh 已启动（pid %s）：%s → %s"
                     % (proc.pid, self.remote_spec(cfg), self.local_spec(cfg)))
            self.log("info", "现在在 %s 上直接敲 `adb devices`，就能看到接在这台机器上的设备"
                     % cfg["host"])

            reader = threading.Thread(target=self._pump, args=(proc,), name="ssh-out")
            reader.daemon = True
            reader.start()

            rc = proc.wait()
            reader.join(timeout=2)

            with self.lock:
                self.process = None

            if self.stop_event.is_set():
                self.log("info", "隧道已断开")
                return

            with self.lock:
                self.stats["lastExitCode"] = rc
                if rc != 0:
                    self.stats["failures"] += 1
                    self.stats["lastError"] = "ssh 退出码 %s" % rc
            if rc == 0:
                self.log("warn", "ssh 已退出（返回码 0）")
            else:
                self.log("err", "ssh 已退出，返回码 %s" % rc)

            if not cfg["autoReconnect"]:
                self.log("info", "未开启自动重连，任务结束")
                return
            delay = cfg["reconnectInterval"]

    def _pump(self, proc):
        """把 ssh 的输出实时推进日志流，顺便把常见错误翻译成人话。"""
        seen = set()
        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip("\r\n")
                if not line:
                    continue
                self.log("info", line)
                low = line.lower()
                for key, hint in HINTS:
                    if key in low and key not in seen:
                        seen.add(key)
                        self.log("warn", hint)
        except (ValueError, OSError):
            pass
