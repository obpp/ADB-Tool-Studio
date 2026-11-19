import os
import tempfile
import unittest

import connection
import transport
from tests import helpers


class FakeTool(object):
    def __init__(self, app_dir, config):
        self.app_dir = app_dir
        self.config = config


class AdbTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adb = helpers.fake_adb(self.tmp)
        self.cfg = {"adbPath": self.adb, "serial": ""}
        self.tool = FakeTool(self.tmp, self.cfg)
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})

    def test_devices_parsed(self):
        devices, err = transport.make(self.tool).devices()
        self.assertEqual(err, "")
        self.assertEqual(devices[0]["serial"], "FAKE123")
        self.assertEqual(devices[0]["model"], "TestBoard")

    def test_select_returns_only_online(self):
        tr = transport.make(self.tool)
        devices, _ = tr.devices()
        serial, err = tr.select(devices, "")
        self.assertEqual((serial, err), ("FAKE123", ""))

    def test_bound_preview_has_serial(self):
        bound = transport.make(self.tool).build("FAKE123")
        self.assertIn("-s FAKE123", bound.preview(["shell", "echo hi"]))

    def test_bound_run(self):
        bound = transport.make(self.tool).build("FAKE123")
        rc, out = bound.run(["shell", "echo hi"], 5)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ok")

    def test_bound_popen_is_binary_pipe(self):
        bound = transport.make(self.tool).build("FAKE123")
        proc = bound.popen(["shell", "echo hi"])
        data = proc.stdout.read()
        proc.stdout.close()
        rc = proc.wait(5)
        self.assertEqual(rc, 0)
        self.assertIn(b"ok", data)

    def test_make_dispatches_ssh(self):
        connection.current().save({
            "mode": "ssh", "ssh": {"host": "h", "user": "u"}})
        self.assertIsInstance(transport.make(self.tool), transport.SshTransport)


SSH_CFG = {
    "host": "192.168.100.1", "port": 22, "user": "root",
    "keyFile": "", "hostKeyPolicy": "accept-new",
    "connectTimeout": 10, "remountCommand": "mount -o remount,rw /system",
}


class SshBoundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        helpers.fake_ssh(self.tmp)
        self.log = os.path.join(self.tmp, "ssh.log")
        os.environ["FAKE_SSH_LOG"] = self.log
        self.tool_cfg = {"adbPath": "unused", "serial": "OLD-SERIAL"}
        self.tr = transport.SshTransport(dict(SSH_CFG), self.tool_cfg)
        self.bound = self.tr.build("ssh:root@192.168.100.1:22")

    def tearDown(self):
        for key in ("FAKE_SSH_LOG", "FAKE_SSH_EXIT", "FAKE_SSH_ERR",
                    "FAKE_SSH_SINK", "FAKE_SSH_OUT", "FAKE_SSH_NOISE"):
            os.environ.pop(key, None)

    def _last_remote(self):
        with open(self.log, "r", encoding="utf-8") as fp:
            return fp.read().splitlines()[-1]

    def test_shell_verbatim(self):
        cmd = "test -b /dev/block/by-name/boot && echo ATS_BLOCK"
        with helpers.path_env(self.tmp):
            rc, out = self.bound.run(["shell", cmd], 5)
        self.assertEqual(rc, 0)
        self.assertEqual(self._last_remote(), cmd)

    def test_run_strips_teardown_noise(self):
        # Windows OpenSSH 退出时打的诊断行不能混进命令输出
        os.environ["FAKE_SSH_OUT"] = "MemTotal: 870.00 MB"
        os.environ["FAKE_SSH_NOISE"] = (
            "close - IO is still pending on closed socket. "
            "read:1, write:0, io:0000025A4EABCB60")
        with helpers.path_env(self.tmp):
            rc, out = self.bound.run(["shell", "cat /proc/meminfo"], 5)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "MemTotal: 870.00 MB")

    def test_push_quotes_remote_path(self):
        src = os.path.join(self.tmp, "image.bin")
        data = b"x" * 131072
        with open(src, "wb") as fp:
            fp.write(data)
        sink = os.path.join(self.tmp, "remote.bin")
        os.environ["FAKE_SSH_SINK"] = sink
        with helpers.path_env(self.tmp):
            proc = self.bound.popen(["push", src, "/tmp/a b'c.img"])
            out = proc.stdout.read().decode("utf-8", "replace")
            proc.stdout.close()
            rc = proc.wait(10)
        self.assertEqual(rc, 0)
        with open(sink, "rb") as fp:
            self.assertEqual(fp.read(), data)
        self.assertEqual(self._last_remote(), "cat > '/tmp/a b'\\''c.img'")
        self.assertIn("[ 50%]", out)
        self.assertIn("[100%]", out)

    def test_push_failure_is_failure(self):
        src = os.path.join(self.tmp, "x.bin")
        with open(src, "wb") as fp:
            fp.write(b"x")
        os.environ["FAKE_SSH_EXIT"] = "255"
        os.environ["FAKE_SSH_ERR"] = "ssh: permission denied"
        with helpers.path_env(self.tmp):
            proc = self.bound.popen(["push", src, "/tmp/x.bin"])
            out = proc.stdout.read().decode("utf-8", "replace")
            proc.stdout.close()
            rc = proc.wait(10)
        self.assertNotEqual(rc, 0)
        self.assertIn("permission denied", out.lower())
        self.assertIsNotNone(self.bound.problem(rc, out))

    def test_push_local_read_error_is_failure(self):
        # 本地源在 isfile 检查和实际读取之间出错（TOCTOU / 读 I/O 错误）：
        # 远端 cat 只会收到 EOF，建出一个截断文件并以 0 退出。必须让返回码
        # 非零，否则上层会把「推送中断」当成功。
        src = os.path.join(self.tmp, "vanished.bin")
        os.makedirs(src)                    # getsize 成功、open 报错
        os.environ["FAKE_SSH_SINK"] = os.path.join(self.tmp, "remote.bin")
        with helpers.path_env(self.tmp):
            proc = self.bound.popen(["push", src, "/tmp/x.bin"])
            out = proc.stdout.read().decode("utf-8", "replace")
            proc.stdout.close()
            rc = proc.wait(10)
        self.assertNotEqual(rc, 0)
        self.assertIn("推送中断", out)
        self.assertIsNotNone(self.bound.problem(rc, out))

    def test_reboot_disconnect_is_success(self):
        os.environ["FAKE_SSH_EXIT"] = "255"
        with helpers.path_env(self.tmp):
            rc, out = self.bound.run(["shell", "reboot"], 5)
        self.assertEqual(rc, 0)

    def test_non_reboot_255_is_failure(self):
        os.environ["FAKE_SSH_EXIT"] = "255"
        with helpers.path_env(self.tmp):
            rc, out = self.bound.run(["shell", "false"], 5)
        self.assertEqual(rc, 255)

    def test_root_is_noop(self):
        rc, out = self.bound.run(["root"], 5)
        self.assertEqual((rc, out), (0, ""))
        self.assertFalse(os.path.exists(self.log))

    def test_remount_uses_configured_command(self):
        with helpers.path_env(self.tmp):
            rc, out = self.bound.run(["remount"], 5)
        self.assertEqual(rc, 0)
        self.assertEqual(self._last_remote(), SSH_CFG["remountCommand"])

    def test_wait_for_device(self):
        with helpers.path_env(self.tmp):
            self.assertEqual(self.bound.run(["wait-for-device"], 5)[0], 0)

    def test_select_offline_returns_empty(self):
        class Offline(object):
            def devices(self):
                return [], "连不上"
        tr = transport.SshTransport(dict(SSH_CFG), dict(self.tool_cfg),
                                    monitor=Offline())
        self.assertEqual(tr.select([], "", "连不上"), ("", "连不上"))

    def test_select_online_returns_synthetic(self):
        class Online(object):
            def devices(self):
                return [{"serial": "ssh:root@192.168.100.1:22",
                         "state": "device", "model": "RK3576"}], ""
        tr = transport.SshTransport(dict(SSH_CFG), dict(self.tool_cfg),
                                    monitor=Online())
        devices, err = tr.devices()
        self.assertEqual(tr.select(devices, "", err),
                         ("ssh:root@192.168.100.1:22", ""))

    def test_synthetic_serial_not_written_back(self):
        self.bound.run(["root"], 5)
        self.assertEqual(self.tool_cfg["serial"], "OLD-SERIAL")
