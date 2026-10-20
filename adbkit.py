# -*- coding: utf-8 -*-
"""adb 相关的公共代码：找 adb、执行命令、解析 devices 输出、判断命令是否真的失败。

always-adb 工具在用；将来别的工具要调 adb 也走这里。
"""

import os
import shutil
import subprocess

import apppaths
import spawn

# 这些关键字出现在输出里，即使返回码是 0 也算失败
BAD_PATTERNS = (
    "exception",
    "error:",
    "no devices/emulators",
    "device offline",
    "device unauthorized",
    "not found",
)


def resolve_adb():
    """找一个能用的 adb。

    顺序：跟程序放在一起的（绿色版，exe 旁边或 exe\\platform-tools\\）
    → PATH → 几个常见的安装位置。
    自带的那份排最前 —— 用户既然特意放了一份，就该用他那份。
    """
    candidates = list(apppaths.bundled_adb())
    candidates += ["adb", "adb.exe"]
    home = os.path.expanduser("~")
    for sub in ("workspace/platform-tools",
                "platform-tools",
                "Android/Sdk/platform-tools",
                "AppData/Local/Android/Sdk/platform-tools",
                "zhangshijin/platform-tools-latest-windows/platform-tools"):
        candidates.append(os.path.join(home, sub, "adb"))
        candidates.append(os.path.join(home, sub, "adb.exe"))
    for cand in candidates:
        found = shutil.which(cand)
        if found:
            return found
        if os.sep in cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "adb"


def build_command(adb_path, serial, args):
    cmd = [adb_path]
    if serial:
        cmd += ["-s", serial]
    return cmd + list(args)


def run(adb_path, serial, args, timeout):
    """跑一条 adb 命令，返回 (returncode, 合并后的输出)。"""
    cmd = build_command(adb_path, serial, args)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            **spawn.quiet()              # 桌面版没控制台，不加就会闪黑框
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "找不到 adb 可执行文件: " + adb_path
    except subprocess.TimeoutExpired:
        return 124, "命令超时（超过 %.0f 秒）: %s" % (timeout, " ".join(cmd))
    except OSError as exc:
        return 126, "无法执行 adb: %s" % (exc,)


def problem(rc, out):
    """这条命令算失败吗？失败返回原因字符串，成功返回 None。

    比原始 bat 只看 errorlevel 严格：有些 adb 版本在 shell 命令被系统拒绝时
    仍然返回 0，只在输出里留一句 SecurityException。
    """
    text = (out or "").strip()
    if rc != 0:
        return text or ("adb 返回码 %s" % rc)
    low = text.lower()
    for bad in BAD_PATTERNS:
        if bad in low:
            return text
    return None


def parse_devices(output):
    """解析 `adb devices -l` 的输出。"""
    devices = []
    for line in (output or "").splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                info[key] = value
        devices.append({
            "serial": parts[0],
            "state": parts[1],
            "model": info.get("model", ""),
            "product": info.get("product", ""),
            "transport": info.get("transport_id", ""),
        })
    return devices


def list_devices(adb_path, serial, timeout=10.0):
    """返回 (设备列表, 错误信息)。"""
    rc, out = run(adb_path, serial, ["devices", "-l"], timeout)
    if rc != 0:
        return [], (out.strip() or ("adb 返回码 %s" % rc))
    return parse_devices(out), ""
