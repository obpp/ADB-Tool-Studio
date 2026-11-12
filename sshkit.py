# -*- coding: utf-8 -*-
"""系统 OpenSSH 的公共拼装：找 ssh、拼非交互参数、探测开发板、翻译报错。

反向隧道和 SSH 连接方式共用这里，避免各写一份。
"""

import os
import re
import shutil
import subprocess

import spawn

PROBE_COMMAND = ("getprop ro.product.model 2>/dev/null || "
                 "cat /proc/device-tree/model 2>/dev/null || true")

# Windows 自带的 OpenSSH 在会话收尾时会往 stderr 打这一行，是它内部 socket
# 兼容层的诊断：既不是脚本输出，也不代表连接失败。应用里 stderr 合并进了
# stdout，不滤掉就会混进日志，还会被 probe 当成设备型号。
_TEARDOWN_NOISE = re.compile(
    r"^close - IO is still pending on closed socket\.\s*"
    r"read:\s*\d+,\s*write:\s*\d+,\s*io:\s*(?:0x)?[0-9A-Fa-f]+$")


def is_teardown_noise(line):
    """这一行是不是 OpenSSH 客户端退出时的诊断噪声。"""
    return bool(_TEARDOWN_NOISE.match((line or "").strip()))


def strip_teardown_noise(text):
    """去掉整行噪声，其余内容原样保留（顺序不变）。"""
    return "\n".join(ln for ln in (text or "").splitlines()
                     if not is_teardown_noise(ln))

# ssh 的常见报错 -> 一句人话提示
HINTS = (
    ("permission denied",
     "认证被拒绝：确认开发板 ~/.ssh/authorized_keys 里有本机公钥，或本机已 ssh-add 加载密钥"),
    ("host key verification failed",
     "主机指纹校验失败：把「主机指纹」改成「自动接受新的」，或先手动 ssh 一次写入 known_hosts"),
    ("address already in use",
     "端口已被占用：远端该端口上已经有别的进程在监听"),
    ("connection refused",
     "连接被拒绝：地址或端口不对，或者开发板的 sshd 没在运行"),
    ("connection timed out",
     "连接超时：网络不通，或被防火墙拦了"),
    ("no route to host", "网络不可达：检查 IP 和路由"),
    ("could not resolve hostname",
     "主机名写错了吧：检查地址拼写"),
    ("connection closed by",
     "连接被开发板关闭：可能是密钥被拒，或 sshd 有限制"),
    ("too many authentication failures",
     "认证失败次数过多：用 -i 指定密钥，并加上 IdentitiesOnly"),
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


def _shell_join(argv):
    """把参数拼成一条可以直接粘进终端的命令（POSIX 风格引号）。"""
    out = []
    for token in argv:
        if token == "" or any(ch in token for ch in " \t\"'\\$&|;<>()*?[]{}`!#~"):
            out.append("'" + token.replace("'", "'\\''") + "'")
        else:
            out.append(token)
    return " ".join(out)


def synthetic_serial(ssh_cfg):
    return "ssh:%s@%s:%d" % (
        ssh_cfg.get("user", ""), ssh_cfg.get("host", ""),
        int(ssh_cfg.get("port", 22)))


def ssh_base(ssh_cfg, ssh_path=None):
    """拼 ssh 的非交互基础参数，最后一项是 user@host。"""
    key = (ssh_cfg.get("keyFile") or "").strip()
    port = int(ssh_cfg.get("port", 22))
    timeout = int(ssh_cfg.get("connectTimeout", 10))
    policy = ssh_cfg.get("hostKeyPolicy") or "accept-new"

    cmd = [
        ssh_path or resolve_ssh(),
        "-T",
        "-p", str(port),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=%d" % timeout,
        "-o", "StrictHostKeyChecking=%s" % policy,
    ]
    if key:
        cmd += ["-i", key, "-o", "IdentitiesOnly=yes"]
    cmd.append("%s@%s" % (ssh_cfg.get("user", ""), ssh_cfg.get("host", "")))
    return cmd


def build_command(ssh_cfg, remote_args, ssh_path=None):
    """在基础参数后按顺序追加远端命令的每个参数。"""
    cmd = ssh_base(ssh_cfg, ssh_path=ssh_path)
    cmd.extend(str(item) for item in remote_args)
    return cmd


def preview(ssh_cfg, remote_args, ssh_path=None):
    return _shell_join(build_command(ssh_cfg, remote_args, ssh_path=ssh_path))


def probe(ssh_cfg, ssh_path=None):
    """探测一次开发板。返回 (设备列表, 错误说明)，列表与 adb devices 同构。"""
    cmd = build_command(ssh_cfg, [PROBE_COMMAND], ssh_path=ssh_path)
    timeout = max(5, int(ssh_cfg.get("connectTimeout", 10)) + 5)
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, **spawn.quiet())
    except subprocess.TimeoutExpired:
        return [], explain(124, "Connection timed out", ssh_cfg)
    except FileNotFoundError:
        return [], "找不到 ssh 可执行文件：%s" % cmd[0]
    except OSError as exc:
        return [], "无法启动 ssh：%s" % exc

    output = strip_teardown_noise(
        (proc.stdout or b"").decode("utf-8", "replace"))
    if proc.returncode != 0:
        return [], explain(proc.returncode, output, ssh_cfg)

    model = ""
    for line in output.splitlines():
        if line.strip():
            model = line.strip()
    return [{
        "serial": synthetic_serial(ssh_cfg),
        "state": "device",
        "model": model,
        "product": "",
        "transport": "",
    }], ""


def explain(rc, output, ssh_cfg):
    """把 ssh 的报错变成一句可操作的提示。"""
    text = (output or "").strip()
    low = text.lower()
    prefix = "SSH 连不上 %s@%s:%s" % (
        ssh_cfg.get("user", ""), ssh_cfg.get("host", ""),
        ssh_cfg.get("port", 22))
    for needle, hint in HINTS:
        if needle in low:
            return "%s：%s" % (prefix, hint)
    last = ""
    if text:
        last = text.splitlines()[-1].strip()[:300]
    return "%s：%s" % (prefix, last or ("ssh 返回码 %s" % rc))
