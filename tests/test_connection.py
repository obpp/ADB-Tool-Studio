import tempfile
import unittest

import connection


class ConnectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = connection.Connection(self.tmp)

    def test_defaults_are_adb(self):
        self.assertEqual(self.conn.mode(), connection.MODE_ADB)
        self.assertEqual(self.conn.ssh()["port"], 22)
        self.assertEqual(self.conn.ssh()["hostKeyPolicy"], "accept-new")
        self.assertEqual(self.conn.info()["display"], "ADB")

    def test_ssh_requires_host_and_user(self):
        with self.assertRaises(connection.ConfigError) as ctx:
            self.conn.validate({"mode": "ssh", "ssh": {"user": "root"}})
        self.assertIn("地址", str(ctx.exception))
        with self.assertRaises(connection.ConfigError):
            self.conn.validate({"mode": "ssh", "ssh": {"host": "192.168.100.1"}})

    def test_port_must_be_in_range(self):
        for bad in (0, 65536, "abc"):
            with self.assertRaises(connection.ConfigError):
                self.conn.validate({"mode": "adb", "ssh": {"port": bad}})

    def test_invalid_mode_rejected(self):
        with self.assertRaises(connection.ConfigError):
            self.conn.validate({"mode": "ftp"})

    def test_save_then_reload_from_disk(self):
        saved = self.conn.save({
            "mode": "ssh",
            "ssh": {"host": "192.168.100.1", "user": "root", "keyFile": "/k/id"},
        })
        self.assertEqual(saved["mode"], "ssh")
        again = connection.Connection(self.tmp)
        self.assertEqual(again.mode(), connection.MODE_SSH)
        self.assertEqual(again.ssh()["host"], "192.168.100.1")
        self.assertEqual(again.ssh()["keyFile"], "/k/id")

    def test_save_preserves_unspecified_ssh_fields(self):
        self.conn.save({"mode": "ssh", "ssh": {"host": "h", "user": "u"}})
        self.conn.save({"mode": connection.MODE_ADB})
        self.assertEqual(self.conn.ssh()["host"], "h")
        self.assertEqual(self.conn.ssh()["user"], "u")


class ReloadRaceTest(unittest.TestCase):
    def setUp(self):
        self.a = tempfile.mkdtemp()
        self.b = tempfile.mkdtemp()
        connection.reload(self.a)
        connection.current().save({"mode": "adb"})

    def tearDown(self):
        mon = connection.peek_monitor()
        if mon is not None:
            mon.reset()

    def test_monitor_created_while_old_resets_binds_new_connection(self):
        # reload() 摘掉旧监听器后要 reset（join 线程，可能耗时）。如果这时候
        # 先把 _connection 换成新的，并发 monitor() 新建的监听器才绑新配置；
        # 反过来就会缓存一个绑着旧 Connection 的监听器，reset() 不会改绑。
        old = connection.monitor()
        seen = {}
        real_reset = old.reset

        def reset_with_concurrent_monitor():
            seen["mon"] = connection.monitor()
            real_reset()

        old.reset = reset_with_concurrent_monitor
        connection.reload(self.b)
        mon = seen["mon"]
        self.assertIsNotNone(mon)
        self.assertIs(mon.connection, connection.current())
        self.assertEqual(mon.connection.app_dir, self.b)
