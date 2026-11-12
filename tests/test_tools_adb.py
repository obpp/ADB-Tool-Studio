import os
import tempfile
import unittest
from unittest import mock

import connection
import transport
from tests import helpers
from tools.push_file import PushFileTool
from tools.replace_image import ReplaceImageTool
from tools.script_runner import ScriptRunnerTool


class RecordingBound(object):
    def __init__(self, inner, calls):
        self.inner = inner
        self.calls = calls
        self.mode = inner.mode

    def preview(self, args):
        self.calls.append(("preview", list(args)))
        return self.inner.preview(args)

    def run(self, args, timeout):
        self.calls.append(("run", list(args)))
        return self.inner.run(args, timeout)

    def popen(self, args):
        self.calls.append(("popen", list(args)))
        return self.inner.popen(args)

    def problem(self, rc, out):
        return self.inner.problem(rc, out)


class RecordingTransport(object):
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def devices(self, timeout=10.0):
        self.calls.append(("devices", timeout))
        return self.inner.devices(timeout)

    def select(self, devices, wanted, error=""):
        self.calls.append(("select", wanted))
        return self.inner.select(devices, wanted, error)

    def build(self, serial):
        self.calls.append(("build", serial))
        return RecordingBound(self.inner.build(serial), self.calls)


class ScriptRunnerTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adb = helpers.fake_adb(self.tmp)
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})
        self.tool = ScriptRunnerTool(self.tmp)
        self.tool.config["adbPath"] = self.adb
        self.tool.config["serial"] = ""

    def test_shell_streams_output(self):
        rc, outcome = self.tool._shell(self.tool.config, "FAKE123", "/data/x.sh", [])
        self.assertEqual((rc, outcome), (0, "ok"))
        messages = [e["msg"] for e in self.tool.history]
        self.assertTrue(any("ok" in m for m in messages))
        self.assertTrue(any("-s FAKE123" in m for m in messages))

    def test_shell_logs_carriage_return_lines(self):
        self.tool.history.clear()
        with mock.patch.dict(os.environ, {"FAKE_SHELL_CR": "progress 50%\\r"}):
            rc, outcome = self.tool._shell(self.tool.config, "FAKE123", "/data/x.sh", [])
        self.assertEqual(outcome, "ok")
        messages = [e["msg"] for e in self.tool.history]
        self.assertIn("progress 50%", messages)
        self.assertTrue(any(m.startswith("$ ") for m in messages))

    def test_shell_goes_through_transport(self):
        rec = RecordingTransport(transport.make(self.tool))
        with mock.patch("tools.script_runner.transport.make", return_value=rec):
            rc, outcome = self.tool._shell(self.tool.config, "FAKE123", "/data/x.sh", [])
        self.assertEqual((rc, outcome), (0, "ok"))
        self.assertIn(("build", "FAKE123"), rec.calls)
        self.assertIn(("preview", ["shell", "sh /data/x.sh"]), rec.calls)
        self.assertIn(("popen", ["shell", "sh /data/x.sh"]), rec.calls)

    def test_run_once_grants_execute_permission(self):
        os.makedirs(self.tool.script_dir(), exist_ok=True)
        with open(os.path.join(self.tool.script_dir(), "hello.sh"), "w") as fp:
            fp.write("echo ok\n")
        self.tool.scripts = [{
            "id": "abcdefgh1", "name": "hello.sh", "file": "hello.sh",
            "args": "", "size": 8, "addedAt": 1,
            "lastRunAt": None, "lastExitCode": None, "lastDuration": None,
        }]
        rec = RecordingTransport(transport.make(self.tool))
        with mock.patch("tools.script_runner.transport.make", return_value=rec):
            outcome = self.tool._run_once(self.tool.config, "abcdefgh1")
        self.assertEqual(outcome, "ok")
        runs = [args for (kind, args) in rec.calls if kind == "run"]
        self.assertIn(["shell", "chmod 755 /data/local/tmp/ats-abcdefgh-hello.sh"], runs)


class ScriptRunnerSshPumpTest(unittest.TestCase):
    """SSH 模式的日志：脚本输出要留下，ssh 自己的收尾诊断不能留。"""

    NOISE = ("close - IO is still pending on closed socket. "
             "read:1, write:0, io:0000025A4EABCB60")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        helpers.fake_ssh(self.tmp)
        connection.reload(self.tmp)
        connection.current().save({
            "mode": "ssh", "ssh": {"host": "192.168.100.1", "user": "root"}})
        self.tool = ScriptRunnerTool(self.tmp)
        self.tool.config["timeout"] = 10
        os.environ["FAKE_SSH_OUT"] = "MemTotal: 870.00 MB"
        os.environ["FAKE_SSH_NOISE"] = self.NOISE

    def tearDown(self):
        os.environ.pop("FAKE_SSH_OUT", None)
        os.environ.pop("FAKE_SSH_NOISE", None)

    def test_shell_logs_output_but_not_teardown_noise(self):
        with helpers.path_env(self.tmp):
            rc, outcome = self.tool._shell(
                self.tool.config, "ssh:root@192.168.100.1:22", "/data/x.sh", [])
        self.assertEqual((rc, outcome), (0, "ok"))
        messages = [e["msg"] for e in self.tool.history]
        self.assertIn("MemTotal: 870.00 MB", messages)
        self.assertFalse([m for m in messages if "IO is still pending" in m])


class ReplaceImageTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adb = helpers.fake_adb(self.tmp)
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})
        self.tool = ReplaceImageTool(self.tmp)
        self.tool.config["adbPath"] = self.adb

    def _preset(self):
        image = os.path.join(self.tmp, "boot.img")
        with open(image, "wb") as fp:
            fp.write(b"x" * 1024)
        os.environ["FAKE_REMOTE_SIZE"] = "1024"
        return {
            "id": "p1", "pushDir": "/mnt",
            "items": [{"imagePath": image, "targets": ["/dev/block/by-name/boot"]}],
            "reboot": False,
        }

    def test_flash_full_adb_flow(self):
        rc, outcome = self.tool._flash(self.tool.config, self._preset())
        self.assertEqual(outcome, "ok")
        messages = [e["msg"] for e in self.tool.history]
        self.assertTrue(any("烧写完成" in m for m in messages))
        self.assertTrue(any("大小校验一致" in m for m in messages))

    def test_exec_streams_bytes(self):
        rc, outcome, text = self.tool._exec(
            self.tool.config, "FAKE123", ["shell", "dd if=a of=b bs=1"], 5, "写入")
        self.assertEqual((rc, outcome), (0, "ok"))
        self.assertIn("records out", text)

    def test_run_short_logs_command(self):
        # 闸一/二失败时，日志里要有实际执行的命令，才能和手动 ssh 对账
        rc, text = self.tool._run_short(
            self.tool.config, "FAKE123",
            "test -b /dev/block/by-name/boot && echo ATS_BLOCK", 5)
        self.assertEqual(text, "ATS_BLOCK")
        messages = [e["msg"] for e in self.tool.history]
        self.assertTrue(any(m.startswith("$ ") and "test -b" in m for m in messages))

    def test_flash_goes_through_transport(self):
        rec = RecordingTransport(transport.make(self.tool))
        with mock.patch("tools.replace_image.transport.make", return_value=rec):
            rc, outcome = self.tool._flash(self.tool.config, self._preset())
        self.assertEqual(outcome, "ok")
        kinds = [call[0] for call in rec.calls]
        self.assertIn("build", kinds)
        self.assertIn("run", kinds)     # _run_short：test -b / blockdev / wc
        self.assertIn("popen", kinds)   # _exec：push / dd / sync


class ReplaceImageSshGateTest(unittest.TestCase):
    """SSH 模式：test -b 成功但 stdout 一个字节都取不到，也要靠退出码放行。

    Windows 自带的 OpenSSH 经管道取 stdout 时，偶尔只有 stderr 的收尾诊断
    能拿到（应用日志里就只剩一行 close - IO ...）。ssh 会把远端命令的退出码
    原样带回来，所以 rc == 0 本身就是「test -b 成功」的证明。
    """

    FAKE_SSH = '''#!/usr/bin/env python3
import os, sys
remote = sys.argv[-1] if len(sys.argv) > 1 else ""
if remote.startswith("cat > "):
    with open(os.environ["FAKE_SSH_SINK"], "wb") as fp:
        fp.write(sys.stdin.buffer.read())
elif "test -b" in remote:
    sys.exit(0)          # 是块设备，但一个字节都不打印
elif "blockdev --getsize64" in remote:
    print("104857600")
elif remote.startswith("wc -c"):
    print("1024")
elif remote.startswith("dd "):
    print("records out")
else:
    print("ok")
'''

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sink = os.path.join(self.tmp, "remote.img")
        os.environ["FAKE_SSH_SINK"] = self.sink
        os.environ["FAKE_REMOTE_SIZE"] = "1024"
        self.adb = helpers.fake_adb(self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        helpers._install(self.bin, "ssh", self.FAKE_SSH)
        connection.reload(self.tmp)
        connection.current().save({
            "mode": "ssh", "ssh": {"host": "192.168.100.1", "user": "root"}})
        self.tool = ReplaceImageTool(self.tmp)

    def tearDown(self):
        os.environ.pop("FAKE_SSH_SINK", None)
        os.environ.pop("FAKE_REMOTE_SIZE", None)

    def test_flash_passes_when_stdout_is_lost(self):
        image = os.path.join(self.tmp, "boot.img")
        with open(image, "wb") as fp:
            fp.write(b"x" * 1024)
        preset = {
            "id": "p1", "pushDir": "/mnt",
            "items": [{"imagePath": image,
                       "targets": ["/dev/disk/by-partlabel/boot_a"]}],
            "reboot": False,
        }
        with helpers.path_env(self.bin):
            rc, outcome = self.tool._flash(self.tool.config, preset)
        self.assertEqual(outcome, "ok")
        self.assertFalse([e["msg"] for e in self.tool.history
                          if "不是块设备" in e["msg"]])

    def test_ssh_zero_exit_counts_as_block_device(self):
        self.assertTrue(ReplaceImageTool._block_ok(0, "", ssh_mode=True))

    def test_ssh_nonzero_exit_is_not_block_device(self):
        self.assertFalse(ReplaceImageTool._block_ok(1, "ATS_NOBLOCK", ssh_mode=True))

    def test_adb_still_needs_the_marker(self):
        # adb shell 历史上总是返回 0，ADB 模式只能认 ATS_BLOCK
        self.assertFalse(ReplaceImageTool._block_ok(0, "", ssh_mode=False))
        self.assertTrue(ReplaceImageTool._block_ok(0, "ATS_BLOCK", ssh_mode=False))


class PushFileTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adb = helpers.fake_adb(self.tmp)
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})
        self.tool = PushFileTool(self.tmp)
        self.tool.config["adbPath"] = self.adb

    def test_push_one_streams_progress(self):
        src = os.path.join(self.tmp, "a.txt")
        with open(src, "w", encoding="utf-8") as fp:
            fp.write("hello")
        rc, out, state = self.tool._push_one(
            self.tool.config, "FAKE123", src, "/system/app/a.txt")
        self.assertEqual((rc, state), (0, "ok"))
        self.assertIn("[100%]", out)
        messages = [e["msg"] for e in self.tool.history]
        self.assertTrue(any("[100%]" in m for m in messages))

    def test_prepare_remount_adb(self):
        self.assertTrue(self.tool._prepare_remount(self.tool.config, "FAKE123"))
        messages = [e["msg"] for e in self.tool.history]
        self.assertTrue(any("remount 完成" in m for m in messages))

    def test_api_list_returns_dirs(self):
        payload, status = self.tool._api_list({"path": "/system"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["dirs"], ["app", "config"])

    def test_remount_and_list_go_through_transport(self):
        rec = RecordingTransport(transport.make(self.tool))
        with mock.patch("tools.push_file.transport.make", return_value=rec):
            self.assertTrue(self.tool._prepare_remount(self.tool.config, "FAKE123"))
            payload, status = self.tool._api_list({"path": "/system"})
        self.assertEqual(status, 200)
        kinds = [call[0] for call in rec.calls]
        self.assertIn("devices", kinds)
        self.assertIn("select", kinds)
        self.assertIn("build", kinds)
        self.assertIn("run", kinds)
