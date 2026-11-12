import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import connection
import server
from tests import helpers


class ServerConnectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        helpers.fake_ssh(self.tmp)
        self._path = helpers.path_env(self.tmp)
        self._path.__enter__()          # 整个测试期间都让 fake ssh 排在最前
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})
        self._old_registry = server.registry
        self._old_data = server.DATA_DIR
        server.DATA_DIR = self.tmp
        server.registry = server.Registry(self.tmp, server.TOOL_CLASSES)
        self.app = server.AppServer("127.0.0.1", 0).start()
        self.base = self.app.url.rstrip("/")

    def tearDown(self):
        self.app.stop()
        mon = connection.peek_monitor()
        if mon:
            mon.reset()
        server.registry = self._old_registry
        server.DATA_DIR = self._old_data
        self._path.__exit__(None, None, None)

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get_text(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")

    def test_tools_carry_connection(self):
        status, payload = self.call("GET", "/api/tools")
        self.assertEqual(status, 200)
        self.assertEqual(payload["connection"]["mode"], "adb")
        self.assertIn("always_adb", [t["id"] for t in payload["tools"]])

    def test_save_ssh_requires_host(self):
        status, payload = self.call("POST", "/api/connection", {"mode": "ssh", "ssh": {}})
        self.assertEqual(status, 400)
        self.assertIn("地址", payload["error"])

    def test_save_ssh_hides_always_adb(self):
        status, payload = self.call("POST", "/api/connection", {
            "mode": "ssh",
            "ssh": {"host": "192.168.100.1", "user": "root"}})
        self.assertEqual(status, 200)
        status, payload = self.call("GET", "/api/tools")
        ids = [t["id"] for t in payload["tools"]]
        self.assertEqual(payload["connection"]["mode"], "ssh")
        self.assertNotIn("always_adb", ids)

    def test_running_tools_block_switch(self):
        with mock.patch.object(server, "running_tool_names",
                               return_value=["脚本执行"]):
            status, payload = self.call("POST", "/api/connection", {"mode": "adb"})
        self.assertEqual(status, 409)
        self.assertIn("脚本执行", payload["error"])

    def test_connection_test_endpoint(self):
        status, payload = self.call("POST", "/api/connection/test", {
            "ssh": {"host": "192.168.100.1", "user": "root"}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["model"], "RK3576")

    def test_always_adb_start_blocked_in_ssh(self):
        self.call("POST", "/api/connection", {
            "mode": "ssh", "ssh": {"host": "192.168.100.1", "user": "root"}})
        status, payload = self.call("POST", "/api/always_adb/start", {})
        self.assertEqual(status, 400)
        self.assertIn("仅支持 ADB", payload["error"])

    def test_root_page_follows_mode(self):
        # 首页 = 当前模式下侧边栏的第一项：ADB 是 always-adb，
        # SSH 下 always-adb 被藏掉，落点跟着 /api/tools 的第一项走。
        status, text = self.get_text("/")
        self.assertEqual(status, 200)
        self.assertIn("<h1>always-adb</h1>", text)
        self.call("POST", "/api/connection", {
            "mode": "ssh", "ssh": {"host": "192.168.100.1", "user": "root"}})
        _, payload = self.call("GET", "/api/tools")
        first = payload["tools"][0]
        self.assertEqual(first["id"], "ssh_tunnel")
        status, text = self.get_text("/")
        self.assertEqual(status, 200)
        self.assertIn('<body data-tool="%s">' % first["id"], text)

    def test_switch_data_dir_reloads_connection(self):
        other = tempfile.mkdtemp()
        os.makedirs(os.path.join(other, "config"))
        # adopt 分支先调 inspect_existing 做只读检查；data_dir 被 mock 成
        # other 后它会误判「已经是当前目录」，所以两个都要打桩
        with mock.patch.object(server.datadir, "inspect_existing",
                               return_value={"ok": True}), \
             mock.patch.object(server.datadir, "adopt", return_value={"ok": True}), \
             mock.patch.object(server.apppaths, "data_dir", return_value=other), \
             mock.patch.object(server.connection, "reload") as reload_mock:
            payload, status = server.switch_data_dir(other, adopt=True)
        self.assertEqual(status, 200)
        reload_mock.assert_called_once_with(other)
