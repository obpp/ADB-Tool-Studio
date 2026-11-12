import os
import tempfile
import time
import unittest

import connection
from tests import helpers
from tools.script_runner import ScriptRunnerTool


class FakeTool(object):
    def __init__(self):
        self.devices = []
        self.error = ""
        self.pushes = 0

    def apply_devices(self, devices, error=""):
        self.devices = devices
        self.error = error

    def publish_state(self):
        self.pushes += 1


def wait_until(fn, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        helpers.fake_ssh(self.tmp)
        self.conn = connection.Connection(self.tmp)

    def tearDown(self):
        for key in ("FAKE_SSH_EXIT", "FAKE_SSH_ERR", "FAKE_SSH_OUT"):
            os.environ.pop(key, None)

    def _ssh(self):
        self.conn.save({"mode": "ssh", "ssh": {
            "host": "192.168.100.1", "user": "root", "port": 22}})

    def test_probe_pushes_synthetic_device(self):
        self._ssh()
        tool = FakeTool()
        mon = connection.ConnectionMonitor(self.conn, interval=0.05)
        # register 会立刻 start，PATH 必须在它之前就位，否则第一次探测会
        # 落到真实的 ssh 上（连接超时，测试白等）
        with helpers.path_env(self.tmp):
            mon.register(tool)
            mon.start()
            self.assertTrue(wait_until(lambda: tool.pushes > 0))
            mon.stop()
        self.assertEqual(tool.devices[0]["serial"], "ssh:root@192.168.100.1:22")
        self.assertEqual(tool.devices[0]["model"], "RK3576")
        self.assertTrue(mon.status()["online"])

    def test_probe_failure_pushes_empty(self):
        self._ssh()
        os.environ["FAKE_SSH_EXIT"] = "255"
        os.environ["FAKE_SSH_ERR"] = "Permission denied (publickey)."
        tool = FakeTool()
        mon = connection.ConnectionMonitor(self.conn, interval=0.05)
        # register 会立刻 start，PATH 必须在它之前就位，否则第一次探测会
        # 落到真实的 ssh 上（连接超时，测试白等）
        with helpers.path_env(self.tmp):
            mon.register(tool)
            mon.start()
            self.assertTrue(wait_until(lambda: tool.pushes > 0))
            mon.stop()
        self.assertEqual(tool.devices, [])
        self.assertIn("认证被拒绝", tool.error)

    def test_adb_mode_does_not_start(self):
        mon = connection.ConnectionMonitor(self.conn, interval=0.05)
        mon.start()
        self.assertFalse(mon.is_running())

    def test_tool_watch_uses_monitor_in_ssh_mode(self):
        self._ssh()
        connection._connection = self.conn
        connection._monitor = None
        tool = ScriptRunnerTool(self.tmp)
        with helpers.path_env(self.tmp):
            tool.ensure_device_watch()
            mon = connection.monitor()
            self.assertTrue(mon.is_running())
            self.assertIsNone(tool._device_watch)
            mon.stop()

    def test_snapshot_has_connection_and_keeps_serial(self):
        self._ssh()
        connection._connection = self.conn
        connection._monitor = None
        tool = ScriptRunnerTool(self.tmp)
        tool.config["serial"] = "OLD-SERIAL"
        snap = tool.snapshot()
        self.assertEqual(snap["connection"]["mode"], "ssh")
        self.assertEqual(tool.config["serial"], "OLD-SERIAL")

    def test_reload_drops_old_monitor(self):
        self._ssh()
        connection.reload(self.tmp)
        mon = connection.monitor()
        self.assertIs(connection.peek_monitor(), mon)
        connection.reload(self.tmp)
        self.assertIsNone(connection.peek_monitor())
