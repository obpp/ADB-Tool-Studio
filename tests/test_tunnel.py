import tempfile
import unittest

import connection
from tools.ssh_tunnel import SshTunnelTool


class TunnelConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})
        self.tool = SshTunnelTool(self.tmp)

    def test_host_and_user_have_no_personal_default(self):
        cfg = self.tool.default_config()
        self.assertEqual(cfg["host"], "")
        self.assertEqual(cfg["user"], "")

    def test_defaults_split_by_mode(self):
        cfg = self.tool.default_config()
        self.assertEqual(cfg["targets"]["adb"],
                         {"remotePort": 5037, "localHost": "127.0.0.1", "localPort": 5037})
        self.assertEqual(cfg["targets"]["ssh"],
                         {"remotePort": 2222, "localHost": "", "localPort": 22})

    def test_legacy_fields_migrate_to_adb_group(self):
        cfg = self.tool.sanitize_config(
            {"remotePort": 6000, "localHost": "10.0.0.5", "localPort": 6001},
            base=self.tool.default_config())
        self.assertEqual(cfg["targets"]["adb"],
                         {"remotePort": 6000, "localHost": "10.0.0.5", "localPort": 6001})
        self.assertEqual(cfg["targets"]["ssh"]["remotePort"], 2222)

    def test_one_group_does_not_overwrite_other(self):
        cfg = self.tool.sanitize_config(
            {"targets": {"ssh": {"remotePort": 2223}}},
            base=self.tool.default_config())
        self.assertEqual(cfg["targets"]["adb"]["remotePort"], 5037)
        self.assertEqual(cfg["targets"]["ssh"]["remotePort"], 2223)
        self.assertEqual(cfg["targets"]["ssh"]["localPort"], 22)

    def test_adb_command(self):
        cfg = self.tool.sanitize_config(
            {"host": "10.27.12.198", "user": "zhangshijin2"},
            base=self.tool.default_config())
        cmd = self.tool.build_ssh_command(cfg)
        self.assertIn("5037:127.0.0.1:5037", cmd)
        self.assertEqual(cmd[-1], "zhangshijin2@10.27.12.198")

    def test_ssh_command_uses_connection_host(self):
        connection.current().save({"mode": "ssh", "ssh": {
            "host": "192.168.100.1", "user": "root", "port": 22}})
        cfg = self.tool.sanitize_config(self.tool.default_config())
        cmd = self.tool.build_ssh_command(cfg)
        self.assertIn("2222:192.168.100.1:22", cmd)

    def test_ssh_local_host_can_override(self):
        connection.current().save({"mode": "ssh", "ssh": {
            "host": "192.168.100.1", "user": "root"}})
        cfg = self.tool.sanitize_config(
            {"targets": {"ssh": {"localHost": "10.0.0.9"}}},
            base=self.tool.default_config())
        self.assertIn("2222:10.0.0.9:22", self.tool.build_ssh_command(cfg))

    def test_desc_follows_mode(self):
        self.assertIn("adb server", self.tool.desc)
        connection.current().save({"mode": "ssh", "ssh": {
            "host": "192.168.100.1", "user": "root"}})
        self.assertIn("开发板", self.tool.desc)
