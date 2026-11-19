# -*- coding: utf-8 -*-
"""传输适配层：按全局连接方式（ADB / SSH）给出同一套设备、命令、进程接口。

上层功能只认这里的接口，不关心底下是 adb 还是 ssh：
  * devices()  枚举设备
  * select()   从设备列表里挑一台
  * build()    绑定一台设备，得到可以 preview / run / popen 的对象

命令参数沿用 adb 风格的 argv（["shell", cmd] / ["push", src, dst]），
SSH 侧负责把它翻译成 ssh 的远端命令。
"""

import os
import subprocess
import threading
import time

import adbkit
import connection
import spawn
import sshkit
from toolbase import shell_join

PUSH_CHUNK = 65536


class Transport(object):
    """设备枚举 + 选设备 + 绑定设备的公共接口。"""

    mode = connection.MODE_ADB

    def devices(self, timeout=10.0):
        """返回 (设备列表, 错误说明)。列表项与 adb devices -l 同构。"""
        raise NotImplementedError

    def select(self, devices, wanted, error=""):
        """返回 (serial, 错误说明)。"""
        raise NotImplementedError

    def build(self, serial):
        """绑定一台设备，返回 Bound（preview / run / popen / problem）。"""
        raise NotImplementedError


class AdbTransport(Transport):
    mode = connection.MODE_ADB

    def __init__(self, tool_cfg):
        self.tool_cfg = tool_cfg

    def devices(self, timeout=10.0):
        return adbkit.list_devices(self.tool_cfg["adbPath"],
                                   self.tool_cfg.get("serial") or "", timeout)

    def select(self, devices, wanted, error=""):
        return adbkit.pick_serial(devices, wanted, error)

    def build(self, serial):
        return AdbBound(self.tool_cfg, serial)


class AdbBound(object):
    """绑定到某台 adb 设备后的命令执行入口。"""

    mode = connection.MODE_ADB

    def __init__(self, tool_cfg, serial):
        self.tool_cfg = tool_cfg
        self.serial = serial

    def preview(self, args):
        return shell_join(adbkit.build_command(
            self.tool_cfg["adbPath"], self.serial, args))

    def run(self, args, timeout):
        return adbkit.run(self.tool_cfg["adbPath"], self.serial, args, timeout)

    def popen(self, args):
        # stdout 是二进制流且带 fileno()：上层要做长命令流式读取和进度统计
        return subprocess.Popen(
            adbkit.build_command(self.tool_cfg["adbPath"], self.serial, args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
            **spawn.quiet()              # 桌面版没控制台，不加就会闪黑框
        )

    def problem(self, rc, out):
        return adbkit.problem(rc, out)


class SshTransport(Transport):
    """SSH 连接方式：开发板只有一台，序列号是合成的。"""

    mode = connection.MODE_SSH

    def __init__(self, ssh_cfg, tool_cfg, monitor=None):
        self.ssh_cfg = ssh_cfg
        self.tool_cfg = tool_cfg
        self.monitor = monitor

    def devices(self, timeout=10.0):
        if self.monitor is not None:
            return self.monitor.devices()
        return sshkit.probe(self.ssh_cfg)

    def select(self, devices, wanted, error=""):
        # 合成序列号不落盘：tool_cfg["serial"] 里存的是 adb 的选择，别串台
        online = [d for d in devices if d.get("state") == "device"]
        if online:
            return online[0]["serial"], ""
        return "", error or "SSH 连不上开发板"

    def build(self, serial):
        return SshBound(self.ssh_cfg, self.tool_cfg, self.monitor)


class SshBound(object):
    """把 adb 风格 argv 翻译成 ssh 远程命令。"""

    mode = connection.MODE_SSH

    def __init__(self, ssh_cfg, tool_cfg=None, monitor=None):
        self.ssh_cfg = ssh_cfg
        self.tool_cfg = tool_cfg or {}
        self.monitor = monitor

    # ---------------- 翻译 ----------------

    def _remote_args(self, args):
        """adb argv -> ssh 的远端参数列表。"""
        verb = args[0] if args else ""
        if verb == "shell":
            cmd = args[1] if len(args) == 2 else " ".join(args[1:])
            return [cmd]
        if verb == "push":
            return ["cat > " + adbkit.device_quote(args[2])]
        return list(args)

    def preview(self, args):
        verb = args[0] if args else ""
        if verb == "root":
            return "（SSH 模式：已以远程用户身份执行，无需 root）"
        if verb == "wait-for-device":
            return "（SSH 模式：探测开发板是否可达）"
        return sshkit.preview(self.ssh_cfg, self._remote_args(args))

    # ---------------- 执行 ----------------

    def run(self, args, timeout):
        verb = args[0] if args else ""
        if verb == "root":
            return 0, ""
        if verb == "wait-for-device":
            devices, err = self._probe()
            if devices:
                return 0, ""
            return 1, err or "SSH 连不上开发板"
        if verb == "remount":
            command = (self.ssh_cfg.get("remountCommand")
                       or connection.DEFAULT_REMOUNT_COMMAND)
            return self._run_remote([command], timeout)
        if verb == "shell":
            rc, out = self._run_remote(self._remote_args(args), timeout)
            if rc == 255 and self._is_reboot(args):
                return 0, out + "\n[连接断开，按重启成功处理]"
            return rc, out
        if verb == "push":
            return self._run_push(args, timeout)
        raise ValueError("SSH 传输不支持: %r" % (args,))

    def popen(self, args):
        verb = args[0] if args else ""
        if verb == "shell":
            # stdout 是二进制流且带 fileno()，上层按字节切行
            return subprocess.Popen(
                sshkit.build_command(self.ssh_cfg, self._remote_args(args)),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
                **spawn.quiet()          # 桌面版没控制台，不加就会闪黑框
            )
        if verb == "push":
            return _SshPushProcess(args[1], args[2], self.ssh_cfg)
        raise ValueError("SSH 传输不支持 popen: %r" % (args[0] if args else ""))

    def problem(self, rc, out):
        # ssh 返回 0 就是成功；没有 adb 那套「返回 0 但输出里有报错」的坑
        if rc == 0:
            return None
        return (out or "").strip() or ("ssh 返回码 %s" % rc)

    # ---------------- 内部 ----------------

    @staticmethod
    def _is_reboot(args):
        cmd = args[1] if len(args) == 2 else " ".join(args[1:])
        return cmd.strip() == "reboot"

    def _probe(self):
        if self.monitor is not None:
            return self.monitor.devices()
        return sshkit.probe(self.ssh_cfg)

    def _run_remote(self, remote_args, timeout):
        cmd = sshkit.build_command(self.ssh_cfg, remote_args)
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=timeout, **spawn.quiet())
        except subprocess.TimeoutExpired:
            return 124, "命令超时（超过 %.0f 秒）: %s" % (
                timeout, sshkit.preview(self.ssh_cfg, remote_args))
        except FileNotFoundError:
            return 127, "找不到 ssh 可执行文件"
        except OSError as exc:
            return 126, "无法启动 ssh: %s" % exc
        return proc.returncode, sshkit.strip_teardown_noise(
            (proc.stdout or b"").decode("utf-8", "replace"))

    def _run_push(self, args, timeout):
        """run() 形式的推送：边推边读进度，超时就结束。"""
        proc = self.popen(args)
        chunks = []
        reader = threading.Thread(target=_drain, args=(proc.stdout, chunks),
                                  name="ssh-push-run")
        reader.daemon = True
        reader.start()

        deadline = time.time() + timeout
        timed_out = False
        while proc.poll() is None:
            if time.time() > deadline:
                timed_out = True
                try:
                    proc.terminate()
                except OSError:
                    pass
                break
            time.sleep(0.05)

        try:
            rc = proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait(5)
        reader.join(2)
        text = b"".join(chunks).decode("utf-8", "replace")
        if timed_out:
            return 124, text + "\n命令超时（超过 %.0f 秒）" % timeout
        return rc, text


def _drain(stream, chunks):
    """把二进制 stdout 读干净（push 的进度行）。"""
    try:
        while True:
            data = stream.read(4096)
            if not data:
                break
            chunks.append(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, AttributeError):
            pass


class _SshPushProcess(object):
    """用 `ssh 'cat > 目标'` 推文件。

    ssh 本身不报进度，所以本地按已写入字节数算百分比，写进一个管道当作
    stdout —— 和 adb push 的输出格式对齐（`[ 37%] 目标`），上层不用改。
    """

    def __init__(self, src, dest, ssh_cfg):
        self._src = src
        self._dest = dest
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self._write_fd = write_fd
        self._cmd = sshkit.build_command(
            ssh_cfg, ["cat > " + adbkit.device_quote(dest)])
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=0,
            **spawn.quiet()          # 桌面版没控制台，不加就会闪黑框
        )
        self.stdin = self._proc.stdin
        self.pid = self._proc.pid
        self.returncode = None
        self._thread = threading.Thread(target=self._feed, name="ssh-push")
        self._thread.daemon = True
        self._thread.start()

    def _emit(self, text):
        try:
            os.write(self._write_fd, text.encode("utf-8"))
        except OSError:
            pass

    def _feed(self):
        last_pct = 0
        try:
            total = os.path.getsize(self._src)
            self._emit("[  0%%] %s\n" % self._dest)
            sent = 0
            with open(self._src, "rb") as fp:
                while True:
                    chunk = fp.read(PUSH_CHUNK)
                    if not chunk:
                        break
                    self.stdin.write(chunk)
                    sent += len(chunk)
                    pct = 100 if total == 0 else int(sent * 100 / total)
                    if pct != last_pct:
                        self._emit("[%3d%%] %s\n" % (pct, self._dest))
                        last_pct = pct
            if last_pct != 100:
                self._emit("[100%%] %s\n" % self._dest)
        except BrokenPipeError as exc:
            # 远端先退出：ssh 的返回码本身就会非零，不需要再杀进程
            self._emit("\n[推送中断] %s\n" % exc)
        except (OSError, IOError) as exc:
            # 本地读源文件失败（检查后文件没了 / 读 I/O 错误）：远端 cat 只会
            # 收到 EOF，建出截断文件并以 0 退出。杀掉 ssh 让返回码非零，
            # 交给上层统一的失败处理，别把「推送中断」当成功。
            self._emit("\n[推送中断] %s\n" % exc)
            try:
                self._proc.kill()
            except OSError:
                pass
        finally:
            try:
                self.stdin.close()
            except (OSError, ValueError):
                pass
            rc = self._proc.wait()
            err = b""
            try:
                err = self._proc.stderr.read() or b""
            except (OSError, ValueError):
                pass
            if rc != 0:
                text = sshkit.strip_teardown_noise(
                    err.decode("utf-8", "replace")).strip()
                if text:
                    self._emit(text + "\n")
            self.returncode = rc
            try:
                os.close(self._write_fd)
            except OSError:
                pass

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired(self._cmd, timeout)
        return self.returncode

    def terminate(self):
        self._proc.terminate()

    def kill(self):
        self._proc.kill()


def make(tool):
    """按当前全局连接方式给这个工具挑一个 Transport。"""
    conn = connection.current(tool.app_dir)
    if conn.mode() == connection.MODE_SSH:
        return SshTransport(conn.ssh(), tool.config)
    return AdbTransport(tool.config)
