import os
import tempfile
import unittest

import sshkit
from tests import helpers

SSH = {
    "host": "192.168.100.1", "port": 2222, "user": "root",
    "keyFile": "/k/id_ed25519", "hostKeyPolicy": "accept-new",
    "connectTimeout": 10, "remountCommand": "mount -o remount,rw /",
}


class SshkitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        helpers.fake_ssh(self.tmp)

    def test_base_command_exact(self):
        cmd = sshkit.build_command(SSH, ["echo hi"])
        self.assertEqual(cmd[1:3], ["-T", "-p"])
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("ConnectTimeout=10", cmd)
        self.assertIn("StrictHostKeyChecking=accept-new", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("IdentitiesOnly=yes", cmd)
        self.assertEqual(cmd[-2:], ["root@192.168.100.1", "echo hi"])

    def test_no_key_means_no_identity_flags(self):
        cfg = dict(SSH, keyFile="")
        cmd = sshkit.build_command(cfg, ["true"])
        self.assertNotIn("-i", cmd)
        self.assertNotIn("IdentitiesOnly=yes", cmd)

    def test_synthetic_serial(self):
        self.assertEqual(sshkit.synthetic_serial(SSH), "ssh:root@192.168.100.1:2222")

    def test_probe_success(self):
        with helpers.path_env(self.tmp):
            devices, err = sshkit.probe(SSH)
        self.assertEqual(err, "")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["serial"], "ssh:root@192.168.100.1:2222")
        self.assertEqual(devices[0]["state"], "device")
        self.assertEqual(devices[0]["model"], "RK3576")

    def test_probe_permission_denied(self):
        os.environ["FAKE_SSH_EXIT"] = "255"
        os.environ["FAKE_SSH_ERR"] = "Permission denied (publickey)."
        try:
            with helpers.path_env(self.tmp):
                devices, err = sshkit.probe(SSH)
        finally:
            os.environ.pop("FAKE_SSH_EXIT", None)
            os.environ.pop("FAKE_SSH_ERR", None)
        self.assertEqual(devices, [])
        self.assertIn("认证被拒绝", err)

    def test_explain_known_errors(self):
        self.assertIn("主机指纹", sshkit.explain(255, "Host key verification failed.", SSH))
        self.assertIn("主机名写错", sshkit.explain(255, "Could not resolve hostname x", SSH))
        self.assertIn("超时", sshkit.explain(255, "Connection timed out", SSH))


# Windows 自带的 OpenSSH 在会话收尾时会往 stderr 打这行诊断，和脚本结果、
# 连接状态都没有关系。应用把 stderr 合并进 stdout，不清掉就会混进日志。
NOISE = ("close - IO is still pending on closed socket. "
         "read:1, write:0, io:0000025A4EABCB60")


class TeardownNoiseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        helpers.fake_ssh(self.tmp)
        os.environ["FAKE_SSH_NOISE"] = NOISE

    def tearDown(self):
        os.environ.pop("FAKE_SSH_NOISE", None)

    def test_is_teardown_noise(self):
        self.assertTrue(sshkit.is_teardown_noise(NOISE))
        self.assertTrue(sshkit.is_teardown_noise(
            "close - IO is still pending on closed socket. "
            "read:0, write:1, io:0000018C2F8C9ED0"))
        self.assertFalse(sshkit.is_teardown_noise("MemTotal: 870.00 MB"))
        self.assertFalse(sshkit.is_teardown_noise(""))

    def test_strip_keeps_script_output(self):
        text = "MemTotal: 870.00 MB\n" + NOISE + "\nMemFree: 44.54 MB\n"
        self.assertEqual(sshkit.strip_teardown_noise(text),
                         "MemTotal: 870.00 MB\nMemFree: 44.54 MB")

    def test_probe_model_ignores_noise(self):
        # 收尾诊断排在型号后面；不滤掉的话它就成了「设备型号」
        with helpers.path_env(self.tmp):
            devices, err = sshkit.probe(SSH)
        self.assertEqual(err, "")
        self.assertEqual(devices[0]["model"], "RK3576")
