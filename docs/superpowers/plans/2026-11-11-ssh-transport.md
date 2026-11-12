# SSH/ADB 全局连接方式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置页用一个开关切换 ADB / SSH，脚本执行、替换镜像、推送文件、反向隧道全部跟随；SSH 使用系统 OpenSSH 和密钥免密。

**Architecture:** 新增 `connection.py`（模式、配置、进程级探测）、`sshkit.py`（拼 ssh 命令、探测、错误提示）、`transport.py`（保留 adb 风格 argv 的适配层）。三个工具只把 `adbkit` / `subprocess` 调用换成 transport，业务逻辑不动。服务端新增 `/api/connection`，前端新增设置卡片、侧边栏连接行，并按模式隐藏 adb 字段。

**Tech Stack:** Python 3 标准库（`http.server`、`subprocess`、`unittest`）、系统 OpenSSH、原生 ES5 前端。

**Spec:** `docs/superpowers/specs/2026-11-11-ssh-transport-design.md`

## Global Constraints

- 后端只用 Python 标准库；SSH 用系统 OpenSSH，不引入 paramiko / scp / sftp。
- SSH 一律带 `BatchMode=yes`；只支持密钥 / ssh-agent，不做密码输入。
- SSH 模式下 `host`、`user` 必填，留空返回 400，不交给 OpenSSH 去猜本地用户名。
- `shell` 后面的命令字符串原样作为一个参数传给远端，传输层不再加任何引号。
- `push` 的远端路径由传输层用 `adbkit.device_quote` 转义后拼成 `cat > '路径'`。
- 合成序列号（`ssh:user@host:port`）只存在于运行时，绝不写回 `config.serial`。
- ADB 模式行为必须与现在完全一致；三工具的原业务逻辑（校验、dd、预设、日志）不动。
- 前端只写 ES5：不用箭头函数、`let` / `const`、模板字符串。
- 不要修改 `/home/ssd9/zhangshijin2/wkspace/my-adb-app/`。
- **不要执行 `git commit`**；本计划里的「提交」步骤只展示 message，改动留在工作区。
- `dev.bat` / `build.bat` 保持 GBK + CRLF；`requirements-desktop.txt` 保持 ASCII。
- 运行时配置 / 数据文件不入库；`config/connection.json` 跟着数据目录走。
- 页面风格与现有 ADB 页一致：名称栏、读数行、参数/预设/日志分栏、按钮都不另起布局。
- 新测试统一放在 `tests/`，用标准库 `unittest`，不引入 pytest。

## File Structure

| 文件 | 责任 |
|------|------|
| `connection.py`（新） | 连接模式与 SSH 参数、校验、持久化、进程级 `ConnectionMonitor` |
| `sshkit.py`（新） | 找 ssh、拼公共参数、探测、错误提示、合成序列号 |
| `transport.py`（新） | `make(tool)`、`AdbTransport`、`SshTransport`、进程适配器 |
| `tools/script_runner.py` | 设备列表 / 推送 / shell 改走 transport |
| `tools/replace_image.py` | 短命令、长命令、重启改走 transport |
| `tools/push_file.py` | 设备列表、目录浏览、remount、推送改走 transport；推送改流式 |
| `tools/ssh_tunnel.py` | 抽走 `resolve_ssh` / `HINTS`；配置分 `targets.adb` / `targets.ssh` |
| `toolbase.py` | SSH 模式下用进程级监听器；快照带 `connection` |
| `server.py` | `/api/connection`、模式切换、`/api/tools` 连接字段、always-adb 拦截 |
| `static/connection.js`（新） | 设置卡片逻辑 + 三个工具页的 SSH 目标切换 |
| `static/settings.html` | 设置页顶部连接卡片 |
| `static/shell.js` | 侧边栏「连接」行；SSH 设备行 |
| `static/{scripts,replace-image,push-file}.{html,js}` | 隐藏 adb 字段，显示只读 SSH 目标 |
| `static/ssh-tunnel.{html,js}` | 标题通用化；三处目标值按模式切换 |
| `static/always-adb.{html,js}` | SSH 模式提示并禁用启动 |
| `static/help.html` | SSH 前提一节 |
| `tests/`（新） | 标准库 unittest + 假 ssh / adb |

## Review Focus

1. **SSH 地址或用户名为空** → 必须 400，不能把空用户名交给 OpenSSH 变成当前系统用户；由 Task 1 的 `test_ssh_requires_host_and_user` 和 Task 9 的 400 测试钉住。
2. **远端命令含空格、`&&`、引号** → `shell` 只能作为一个参数传入，不能被拆分或二次转义；由 Task 7 的 `test_shell_verbatim` 钉住。
3. **推送目标路径含空格或单引号** → 远端必须是 `cat > '...'`，引号按 POSIX 规则闭合；由 Task 7 的 `test_push_quotes_remote_path` 钉住。
4. **SSH 中途断开 / 推送失败** → 必须返回失败，不能把 rc != 0 当成功；进度百分比按本地发送字节计算；由 Task 7 的 `test_push_failure_is_failure` 与 `test_push_quotes_remote_path`（内含 50%/100% 断言）钉住。
5. **开发板重启导致 ssh 返回 255** → 只有显式 `shell reboot` 按成功处理，其它 255 仍是失败；由 Task 7 的 `test_reboot_disconnect_is_success` 和 `test_non_reboot_255_is_failure` 钉住。

---

### Task 1: 连接配置存储与校验

**Files:**
- Create: `connection.py`
- Create: `tests/__init__.py`（空文件）
- Test: `tests/test_connection.py`

**Interfaces:**
- Produces:
  - `MODE_ADB = "adb"`、`MODE_SSH = "ssh"`、`HOST_KEY_POLICIES = ("accept-new", "yes", "no")`
  - `DEFAULT_SSH`：`host=""`、`port=22`、`user=""`、`keyFile=""`、`hostKeyPolicy="accept-new"`、`connectTimeout=10`、`remountCommand="mount -o remount,rw /"`
  - `class ConfigError(ValueError)`
  - `class Connection(app_dir)`：`path`、`load()`、`get() -> dict`、`mode() -> str`、`ssh() -> dict`、`validate(raw, base=None) -> dict`、`save(raw) -> dict`、`reload(app_dir=None)`、`info() -> dict`
  - `info()` 返回 `{mode, modeName, ssh, display, online, error, model, deviceCount}`；ADB 模式 `display == "ADB"`，SSH 模式 `display == "SSH user@host:port"`；本任务里在线字段固定为 `False/""/""/0`
  - 模块级 `current(app_dir=None)`、`reload(app_dir=None)`（本任务只做单例；`monitor()` 在 Task 8 加）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_connection.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_connection -v`
Expected: FAIL / ERROR，`ModuleNotFoundError: No module named 'connection'`。

- [ ] **Step 3: 实现 `connection.py`**

`Connection.__init__`：`self.app_dir`、`self.path = os.path.join(app_dir, "config", "connection.json")`、`self._data = self.load()`；模块级 `_lock = threading.RLock()`、`_connection = None`，`current(app_dir=None)` 首次用 `app_dir or apppaths.data_dir()` 创建，`reload(app_dir=None)` 更新路径后重读。

`validate(raw, base=None)` 的规则：

- `base` 不是 dict 时用内部默认值（不是当前磁盘配置）；`raw["ssh"]` 存在但不是 dict → `ConfigError("SSH 参数格式不对")`。
- `mode` 不在 `MODES` → `ConfigError("连接方式只能是 adb 或 ssh")`。
- `port` 用 `int()` 解析，失败或不在 1–65535 → `ConfigError("SSH 端口必须在 1-65535 之间")`。
- `hostKeyPolicy` 不在 `HOST_KEY_POLICIES` → 用 `"accept-new"`；`connectTimeout` 夹紧到 1–60；`remountCommand` 为空时回默认。
- 合并后的 `mode == "ssh"` 时 `host` / `user` 去掉首尾空白后为空 → 分别抛 `ConfigError("SSH 模式必须填写地址")` / `("SSH 模式必须填写用户名")`。
- 返回 `{"mode": ..., "ssh": {...七个字段...}}`，不保留任何额外键。

`save(raw)` 用 `validate(raw, base=self.get())` 合并当前值，`os.makedirs(dirname, exist_ok=True)`，写 `path + ".tmp"` 后 `os.replace`，更新 `self._data` 并返回。`get()` / `ssh()` 返回 `copy.deepcopy`。

`info()` 按 Interfaces 组装；SSH 模式 `display = "SSH %s@%s:%d" % (ssh["user"], ssh["host"], ssh["port"])`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_connection -v`
Expected: 6 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Add global connection config

[what]新增 config/connection.json 的读写与校验，支持 adb / ssh 两种模式
[why]四个功能需要一个全局连接开关，且 SSH 参数必须可持久化
[how]新增 connection.Connection，SSH 模式强制校验地址与用户名，端口限定 1-65535
[影响等级]B
[建议测试范围]设置页连接方式切换、空地址、非法端口、保存后重读
```

---

### Task 2: sshkit 与隧道模块去重

**Files:**
- Create: `sshkit.py`
- Create: `tests/helpers.py`
- Test: `tests/test_sshkit.py`
- Modify: `tools/ssh_tunnel.py:31-79`（删掉本地的 `resolve_ssh` / `HINTS`，改成从 `sshkit` 导入）

**Interfaces:**
- Produces:
  - `resolve_ssh() -> str`（候选顺序与现 `ssh_tunnel.resolve_ssh` 一致）
  - `HINTS`（与现 `ssh_tunnel.HINTS` 相同的 9 条）
  - `ssh_base(ssh_cfg, ssh_path=None) -> list`
  - `build_command(ssh_cfg, remote_args, ssh_path=None) -> list`（`remote_args` 是 list，逐项追加）
  - `preview(ssh_cfg, remote_args, ssh_path=None) -> str`（`shell_join(build_command(...))`）
  - `probe(ssh_cfg, ssh_path=None) -> (devices, error)`；成功返回一台 `{"serial": synthetic_serial(cfg), "state": "device", "model": model, "product": "", "transport": ""}`，失败返回 `([], 原因)`
  - `explain(rc, output, cfg) -> str`
  - `synthetic_serial(cfg) -> str`，格式 `ssh:user@host:port`
- `ssh_base` 顺序固定：`[ssh, "-T", "-p", port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=N", "-o", "StrictHostKeyChecking=policy"]`，有 key 时追加 `["-i", key, "-o", "IdentitiesOnly=yes"]`，最后追加 `user@host`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_sshkit.py
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
```

`tests/helpers.py` 提供：

```python
import contextlib
import os
import stat
import sys
from unittest import mock

FAKE_SSH = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
remote = args[-1] if args else ""
log = os.environ.get("FAKE_SSH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fp:
        fp.write(remote + "\\n")
if os.environ.get("FAKE_SSH_EXIT"):
    sys.stderr.write(os.environ.get("FAKE_SSH_ERR", "ssh: failed\\n"))
    sys.exit(int(os.environ["FAKE_SSH_EXIT"]))
if remote.startswith("cat > "):
    data = sys.stdin.buffer.read()
    with open(os.environ["FAKE_SSH_SINK"], "wb") as fp:
        fp.write(data)
    sys.exit(0)
print(os.environ.get("FAKE_SSH_OUT", "RK3576"))
'''

def _install(dirpath, name, source):
    """把假可执行文件放进 dirpath；Windows 再加一个 .bat 转发给当前解释器。"""
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(source)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    if os.name == "nt":
        with open(path + ".bat", "w", encoding="ascii") as fp:
            fp.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, path))
    return path

def fake_ssh(dirpath, name="ssh"):
    return _install(dirpath, name, FAKE_SSH)

@contextlib.contextmanager
def path_env(dirpath):
    env = {"PATH": dirpath + os.pathsep + os.environ.get("PATH", "")}
    with mock.patch.dict(os.environ, env):
        yield
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_sshkit -v`
Expected: FAIL / ERROR，`No module named 'sshkit'`。

- [ ] **Step 3: 实现 `sshkit.py` 并改 `ssh_tunnel.py`**

- `resolve_ssh` 与 `HINTS`：从 `tools/ssh_tunnel.py` 原样搬过来；`ssh_tunnel.py` 删除这两个对象及不再使用的 `shutil` 导入，改成 `from sshkit import HINTS, resolve_ssh`。
- `probe` 的远端命令固定为：
  ```python
  PROBE_COMMAND = ("getprop ro.product.model 2>/dev/null || "
                   "cat /proc/device-tree/model 2>/dev/null || true")
  ```
  用 `subprocess.run(..., stdout=PIPE, stderr=STDOUT, stdin=DEVNULL, timeout=max(5, connectTimeout + 5), **spawn.quiet())`。
  `model` 取输出的最后一个非空行并 `strip()`。
- `explain` 按 `HINTS` 顺序匹配小写输出，返回 `"SSH 连不上 user@host:port：<提示>"`；没有命中时用输出最后一行（截 300 字符）或 `"ssh 返回码 N"`。
- `ssh_base` 里 `ssh_path or resolve_ssh()`；`port` / `connectTimeout` 直接 `str()`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_sshkit -v`
Expected: 6 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Extract shared ssh helpers

[what]新增 sshkit，统一拼 ssh 命令、探测开发板、把常见错误翻成人话
[why]连接方式和反向隧道都要用系统 ssh，不能各写一份
[how]从 ssh_tunnel 抽出 resolve_ssh / HINTS，新增 build_command / probe / explain
[影响等级]B
[建议测试范围]隧道启动、SSH 认证失败、主机指纹变化、地址解析失败
```

---

### Task 3: transport 骨架与 ADB 实现

**Files:**
- Create: `transport.py`
- Test: `tests/test_transport.py`（本任务只写 ADB 部分）
- Modify: `tests/helpers.py`（追加假 adb）

**Interfaces:**
- Produces:
  - `make(tool) -> Transport`
  - `class Transport`：`mode`、`devices(timeout=10.0) -> (list, str)`、`select(devices, wanted, error="") -> (str, str)`、`build(serial) -> Bound`
  - `class AdbTransport(Transport)`、`class AdbBound`：`preview(args) -> str`、`run(args, timeout) -> (rc, text)`、`popen(args) -> Popen`、`problem(rc, out) -> str|None`
  - `class SshTransport(Transport)`（本任务只占位：`__init__(ssh_cfg, tool_cfg, monitor=None)`，四个方法抛 `NotImplementedError`）
- `AdbBound.popen`：`subprocess.Popen(build_command(...), stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, bufsize=0, **spawn.quiet())`，即 `stdout` 是二进制、有 `fileno()`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_transport.py
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
        rc = proc.wait(5)
        self.assertEqual(rc, 0)
        self.assertIn(b"ok", data)

    def test_make_dispatches_ssh(self):
        connection.current().save({
            "mode": "ssh", "ssh": {"host": "h", "user": "u"}})
        self.assertIsInstance(transport.make(self.tool), transport.SshTransport)
```

`tests/helpers.py` 追加：

```python
FAKE_ADB = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_ADB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fp:
        fp.write("\\x1f".join(args) + "\\n")
if args[:1] == ["-s"]:
    args = args[2:]
verb = args[0] if args else ""
if verb == "devices":
    print("List of devices attached")
    print("FAKE123 device product:board model:TestBoard transport_id:1")
elif verb == "push":
    print("[100%] " + (args[2] if len(args) > 2 else ""))
elif verb == "shell":
    cmd = args[1] if len(args) > 1 else ""
    if "test -b" in cmd:
        print("ATS_BLOCK")
    elif "blockdev --getsize64" in cmd:
        print(os.environ.get("FAKE_BLOCK_SIZE", "104857600"))
    elif cmd.startswith("wc -c "):
        print(os.environ.get("FAKE_REMOTE_SIZE", "1"))
    elif cmd.startswith("dd ") or " dd " in cmd:
        print("records in")
        print("records out")
    elif cmd.startswith("ls "):
        print("app/")
        print("config/")
    else:
        print(os.environ.get("FAKE_SHELL_OUT", "ok"))
else:
    print("ok")
'''

def fake_adb(dirpath, name="adb"):
    return _install(dirpath, name, FAKE_ADB)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_transport -v`
Expected: FAIL / ERROR，`No module named 'transport'`。

- [ ] **Step 3: 实现 `transport.py` 的 ADB 部分**

```python
def make(tool):
    conn = connection.current(tool.app_dir)
    if conn.mode() == connection.MODE_SSH:
        return SshTransport(conn.ssh(), tool.config)
    return AdbTransport(tool.config)

class AdbTransport(Transport):
    mode = connection.MODE_ADB
    def __init__(self, tool_cfg): self.tool_cfg = tool_cfg
    # devices -> adbkit.list_devices(tool_cfg["adbPath"], tool_cfg.get("serial") or "", timeout)
    # select  -> adbkit.pick_serial(devices, wanted, error)
    # build   -> AdbBound(tool_cfg, serial)

class AdbBound(object):
    def preview(self, args):
        return shell_join(adbkit.build_command(self.tool_cfg["adbPath"], self.serial, args))
    def run(self, args, timeout):
        return adbkit.run(self.tool_cfg["adbPath"], self.serial, args, timeout)
    def popen(self, args):
        return subprocess.Popen(
            adbkit.build_command(self.tool_cfg["adbPath"], self.serial, args),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, **spawn.quiet())
    def problem(self, rc, out):
        return adbkit.problem(rc, out)
```

`Transport` 基类只放 `mode` 和四个抛 `NotImplementedError` 的方法；`SshTransport` 继承它，保存 `ssh_cfg`、`tool_cfg`、`monitor`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_transport -v`
Expected: 6 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Add transport layer with adb backend

[what]新增 transport 适配层，按全局连接模式返回 ADB 或 SSH 实现
[why]三个功能要共用同一套设备/命令/进程接口，避免各改各的
[how]保留 adb 风格 argv，ADB 侧包装 adbkit 与 subprocess，SSH 侧先留接口
[影响等级]B
[建议测试范围]设备枚举、自动选设备、命令预览、长命令流式输出
```

---

### Task 4: 脚本执行接入 transport

**Files:**
- Modify: `tools/script_runner.py:30-32`（导入）、`:399-411`（设备）、`:650-705`（推送）、`:727-825`（shell 与输出泵）
- Test: `tests/test_tools_adb.py`

**Interfaces:**
- Consumes: `transport.make(tool)`、`Transport.devices/select/build`、`Bound.preview/run/popen/problem`
- Produces: `ScriptRunnerTool.refresh_devices()` / `_pick_serial()` / `_shell()` 行为不变，仅底层通道可切换。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tools_adb.py
import tempfile
import unittest

import connection
from tests import helpers
from tools.script_runner import ScriptRunnerTool


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
        rc, outcome = self.tool._shell(self.tool.config, "FAKE123", "/data/x.sh", [])
        self.assertEqual(outcome, "ok")
        self.assertTrue(any(m.startswith("$ ") for m in [e["msg"] for e in self.tool.history]))
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_tools_adb -v`
Expected: FAIL，`_shell` 仍用 `adbkit.build_command`，日志里没有 SSH/transport 预览（或未导入 transport）。

- [ ] **Step 3: 改造 `script_runner.py`**

- 导入 `transport`；删除 `spawn`（不再直接 `Popen`）。
- `refresh_devices`：
  ```python
  devices, err = transport.make(self).devices(min(10.0, cfg["timeout"]))
  ```
  其余写状态不变。
- `_pick_serial`：`return transport.make(self).select(devices, self.config["serial"], self.devices_error)`。
- `_run_once`：选完 `serial` 后 `bound = transport.make(self).build(serial)`；推送日志用 `bound.preview(["push", script_path, remote])`；执行 `bound.run(...)`；失败判断 `bound.problem(push_rc, push_out)`。
- `_shell`：
  ```python
  bound = transport.make(self).build(serial)
  cmd_preview = bound.preview(["shell", device_cmd])
  self.log("cmd", "$ " + cmd_preview)
  proc = bound.popen(["shell", device_cmd])
  ```
  去掉原来的“按参数分开显示”那条日志（它拼的是另一条命令，容易和实际执行的对不上）。
- `_pump` 改成二进制读法（与 `replace_image._pump` 一致）：`os.read(proc.stdout.fileno(), 4096)`，按 `_EOL = re.compile(b"\r\n|\r|\n")` 切行，`decode("utf-8", "replace")`；`\r` 行最多每 0.4 秒记一条。模块级加 `_EOL`。
- `FileNotFoundError` 的提示文案改成“找不到命令可执行文件”（ADB / SSH 都适用）；`OSError` 文案同理。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_tools_adb -v`
Expected: 2 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Route script runner through transport

[what]脚本执行的设备枚举、脚本推送和远程 shell 改走 transport
[why]切换 SSH 时这个功能要跟着走，同时保持 ADB 行为不变
[how]用 transport 的 preview/run/popen 替换 adbkit 调用，输出泵改为按字节切行
[影响等级]B
[建议测试范围]ADB 下跑脚本、带参数脚本、停止执行、\r 进度输出
```

---

### Task 5: 替换镜像接入 transport

**Files:**
- Modify: `tools/replace_image.py:36-38`（导入）、`:352-371`（设备）、`:833-844`（重启）、`:864-960`（子进程）
- Test: `tests/test_tools_adb.py`（追加）

**Interfaces:**
- Consumes: `transport.make(tool)`、`Bound.preview/run/popen`
- Produces: `_flash()` 的既有流程、日志、返回值不变。

- [ ] **Step 1: 写失败测试**

```python
import os
from tools.replace_image import ReplaceImageTool


class ReplaceImageTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adb = helpers.fake_adb(self.tmp)
        connection.reload(self.tmp)
        connection.current().save({"mode": "adb"})
        self.tool = ReplaceImageTool(self.tmp)
        self.tool.config["adbPath"] = self.adb

    def test_flash_full_adb_flow(self):
        image = os.path.join(self.tmp, "boot.img")
        with open(image, "wb") as fp:
            fp.write(b"x" * 1024)
        os.environ["FAKE_REMOTE_SIZE"] = "1024"
        preset = {
            "id": "p1", "pushDir": "/mnt",
            "items": [{"imagePath": image, "targets": ["/dev/block/by-name/boot"]}],
            "reboot": False,
        }
        rc, outcome = self.tool._flash(self.tool.config, preset)
        self.assertEqual(outcome, "ok")
        messages = [e["msg"] for e in self.tool.history]
        self.assertTrue(any("烧写完成" in m for m in messages))
        self.assertTrue(any("大小校验一致" in m for m in messages))

    def test_exec_streams_bytes(self):
        rc, outcome, text = self.tool._exec(
            self.tool.config, "FAKE123", ["shell", "dd if=a of=b bs=1"], 5, "写入")
        self.assertEqual((rc, outcome), (0, "ok"))
        self.assertIn("records out", text)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_tools_adb -v`
Expected: FAIL，`_run_short` / `_exec` 仍直接调 `adbkit`。

- [ ] **Step 3: 改造 `replace_image.py`**

- 导入 `transport`；删除 `spawn`。
- `refresh_devices`：`transport.make(self).devices(10.0)`。
- `_resolve_serial`：先 `transport.make(self).select(devices, self.config["serial"], self.devices_error)`；保留“显式指定但 unauthorized/offline”的现有检查（SSH 合成设备始终 `device`）。
- `_run_short`：`bound = transport.make(self).build(serial)`；预览用 `bound.preview(["shell", command])`（只用于日志）；执行 `bound.run(["shell", command], timeout)`。
- `_exec`：`bound = transport.make(self).build(serial)`；日志用 `bound.preview(args)`；`proc = bound.popen(args)`。其余读取/停止/超时逻辑不变。
- 重启：`bound.preview(["shell", "reboot"])` 与 `bound.run(["shell", "reboot"], REBOOT_TIMEOUT)`；SSH 下 255 的成功映射由 Task 7 的 `SshBound.run` 负责。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_tools_adb -v`
Expected: 4 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Route image flashing through transport

[what]替换镜像的设备选择、短命令、推送/dd/sync 和重启改走 transport
[why]SSH 模式没有 adb 可调，但写分区流程必须完全保留
[how]把 adbkit.build_command/run/Popen 换成 bound 的 preview/run/popen
[影响等级]A
[建议测试范围]ADB 全流程烧写、分区大小校验、dd 输出解析、重启
```

---

### Task 6: 推送文件接入 transport 并改流式

**Files:**
- Modify: `tools/push_file.py:31-33`（导入）、`:335-342`（设备）、`:551-583`（目录浏览）、`:775-795`（推送结果日志）、`:849-956`（remount 与推送）
- Test: `tests/test_tools_adb.py`（追加）

**Interfaces:**
- Consumes: `transport.make(tool)`、`Bound.preview/run/popen/problem`
- Produces: `_push_one()` 现在边推边记日志并返回 `(rc, out, state)`；`adbkit.problem` 的判断保留在调用方。

- [ ] **Step 1: 写失败测试**

```python
from tools.push_file import PushFileTool


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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_tools_adb -v`
Expected: FAIL，`_push_one` 仍用 `communicate()`，日志里没有逐行进度。

- [ ] **Step 3: 改造 `push_file.py`**

- 导入 `transport`；删除 `spawn`。
- `refresh_devices`：`transport.make(self).devices(10.0)` 后 `apply_devices`。
- `_pick_serial`：`transport.make(self).select(...)`。
- `_api_list`：`bound = transport.make(self).build(serial)`；`command` 仍是 `"ls -a -p -L " + adbkit.device_quote(path)`；`rc, out = bound.run(["shell", command], LIST_TIMEOUT)`；失败判断保持“rc != 0 且没有列出目录才报错”。
- `_prepare_remount`：`bound` 一次性建好；循环里 `bound.preview([args])` / `bound.run([args], timeout)`。ADB 专属的文本判断（`cannot run as root`、`remount failed` 等）只在 `transport.make(self).mode == "adb"` 时执行；SSH 只按 `rc != 0` 判定，失败提示改成“remount 失败：<输出>；有些系统要先 disable-verity 再重启”。
- `_push_one`：
  - `bound = transport.make(self).build(serial)`；`cmd = bound.preview(["push", src, dest])`。
  - `proc = bound.popen(["push", src, dest])`（`FileNotFoundError` / `OSError` 仍返回 fail）。
  - 起一个读取线程按 `_EOL` 切行（同 Task 4），每行 `self.log("info", line)`，同时把原始行拼进 `out`；
  - 主线程按 `push_timeout(size)` 轮询 `proc.poll()`，`stop_event` 置位就 `terminate()`；超时同样 `terminate()`；
  - 结束 `try: rc = proc.wait(5) except subprocess.TimeoutExpired: proc.kill(); rc = proc.wait()`（SSH 适配器的 `wait` 会抛 `TimeoutExpired`），`reader.join(1)`，返回 `(rc, out, state)`。
  - 模块级加 `_EOL`。
- 调用方（`_run_preset` 的 `for index, item ...` 内）：删除 `text = _clean_output(out)` 和随后那条 `self.log(...)`，因为 `_push_one` 已经逐行记过；`adbkit.problem(rc, out)` 判断保留。`_clean_output` 若不再被使用则删除。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_tools_adb -v`
Expected: 7 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Stream file push through transport

[what]推送文件的设备枚举、目录浏览、remount 和推送改走 transport，推送改为流式输出
[why]SSH 下没有 adb push，需要按字节计数生成进度；ADB 也要保持同样的日志体验
[how]用 bound.popen 替换 communicate，读取线程按 \r/\n 切行，逐行记日志
[影响等级]A
[建议测试范围]ADB 推送大文件、进度行、remount、停止推送、目录浏览
```

---

### Task 7: SSH 传输实现

**Files:**
- Modify: `transport.py`（替换 `SshTransport` 占位，新增 `SshBound`、`_SshPushProcess`）
- Test: `tests/test_transport.py`（追加 SSH 部分）

**Interfaces:**
- Consumes: Task 1 的 `connection`、Task 2 的 `sshkit`、Task 3 的 `Transport`
- Produces:
  - `SshTransport(ssh_cfg, tool_cfg, monitor=None)`：`mode == "ssh"`；`devices()` 优先读 `monitor.devices()`，没有 monitor 时直接 `sshkit.probe`；`select()` 返回唯一合成序列号或 `("", 原因)`；`build(serial)` 返回 `SshBound`。
  - `SshBound.preview(args) -> str`、`run(args, timeout) -> (rc, text)`、`popen(args) -> 进程适配器`、`problem(rc, out) -> str|None`（SSH 只看 `rc != 0`）。
  - 进程适配器：`stdout`（二进制可读、有 `fileno()`）、`stdin`、`poll()`、`wait(timeout)`、`terminate()`、`kill()`、`returncode`、`pid`。
- 翻译规则：
  | argv | SSH 实际行为 |
  |------|--------------|
  | `["shell", CMD]` | `ssh ... CMD`（CMD 原样一个参数） |
  | `["push", SRC, DST]` | `ssh ... "cat > <device_quote(DST)>"`，本地 64 KiB 分块写 stdin |
  | `["root"]` | `(0, "")`，不启动 ssh |
  | `["remount"]` | `ssh ... <sshCfg.remountCommand>` |
  | `["wait-for-device"]` | 探测一次；通 `(0, "")`，不通 `(1, 原因)` |
  | `["shell", "reboot"]` | `ssh ... reboot`；rc 255 映射为 `(0, 输出 + 说明)` |
  | 其它 | `ValueError` |

- [ ] **Step 1: 写失败测试**

```python
import re
import time
import unittest

import sshkit
from tests import helpers

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
                    "FAKE_SSH_SINK", "FAKE_SSH_OUT"):
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
            rc = proc.wait(10)
        self.assertNotEqual(rc, 0)
        self.assertIn("permission denied", out.lower())
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
        tr = transport.SshTransport(dict(SSH_CFG), dict(self.tool_cfg), monitor=Offline())
        self.assertEqual(tr.select([], "", "连不上"), ("", "连不上"))

    def test_select_online_returns_synthetic(self):
        class Online(object):
            def devices(self):
                return [{"serial": "ssh:root@192.168.100.1:22",
                         "state": "device", "model": "RK3576"}], ""
        tr = transport.SshTransport(dict(SSH_CFG), dict(self.tool_cfg), monitor=Online())
        devices, err = tr.devices()
        self.assertEqual(tr.select(devices, "", err),
                         ("ssh:root@192.168.100.1:22", ""))

    def test_synthetic_serial_not_written_back(self):
        self.bound.run(["root"], 5)
        self.assertEqual(self.tool_cfg["serial"], "OLD-SERIAL")
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_transport.SshBoundTest -v`
Expected: FAIL，`SshTransport` 方法抛 `NotImplementedError`。

- [ ] **Step 3: 实现 SSH 传输**

`SshTransport.devices()`：

```python
if self.monitor is not None:
    return self.monitor.devices()
return sshkit.probe(self.ssh_cfg)
```

`select(devices, wanted, error="")`：`devices` 为空 → `("", error or "SSH 连不上开发板")`；过滤 `state == "device"`，只有一台就返回它的 `serial`；多于一台返回第一台（合成序列号本来就唯一）。**不读写 `tool_cfg["serial"]`。**

`SshBound._remote_args(args)`：

```python
if args[0] == "shell":
    return [args[1] if len(args) == 2 else " ".join(args[1:])]
if args[0] == "push":
    return ["cat > " + adbkit.device_quote(args[2])]
return list(args)
```

`run(args, timeout)` 按翻译表分派；`root` / `remount` / `wait-for-device` 先处理。`shell` 用 `subprocess.run(sshkit.build_command(...), stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, timeout=timeout, **spawn.quiet())`，超时返回 `(124, "命令超时…")`，`FileNotFoundError` 返回 `(127, ...)`。`shell reboot` 的 255 → `(0, out + "\n[连接断开，按重启成功处理]")`。`push` 走 `popen` + 后台读输出 + 截止时间轮询。

`popen`：

- `shell`：直接 `Popen(sshkit.build_command(...), stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, bufsize=0, **spawn.quiet())`。
- `push`：返回 `_SshPushProcess(src, dst_quoted, ssh_cfg)`。
- 其它 verb：`ValueError("SSH 传输不支持 popen: %r" % args[0])`。

`_SshPushProcess` 的实现要点：

```python
read_fd, write_fd = os.pipe()
self.stdout = os.fdopen(read_fd, "rb", buffering=0)
self._proc = subprocess.Popen(
    sshkit.build_command(ssh_cfg, ["cat > " + adbkit.device_quote(dest)]),
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE, bufsize=0, **spawn.quiet())
self.stdin = self._proc.stdin
self.pid = self._proc.pid
```

喂数据线程按 65536 字节读 src、写 `stdin`、累计 `sent`；`pct = 100 if total == 0 else int(sent * 100 / total)`；百分比变化时 `os.write(write_fd, ("[%3d%%] %s\n" % (pct, dest)).encode("utf-8"))`；0% 与 100% 一定各写一次。写失败（`BrokenPipeError` / `OSError`）时把 `"\n[推送中断] %s\n" % exc` 写入进度管道并停止。线程收尾 `stdin.close()`、`rc = proc.wait()`、读取 `stderr` 并在 `rc != 0` 时追加到进度管道、设置 `self.returncode = rc`、`os.close(write_fd)`。

`wait(timeout)`：`thread.join(timeout)`；还活着抛 `subprocess.TimeoutExpired(self._cmd, timeout)`，否则返回 `returncode`。`terminate()` / `kill()` 转给 `self._proc`。`poll()` 返回 `returncode`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_transport -v`
Expected: 17 tests PASS（ADB 6 + SSH 11）。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Implement ssh transport

[what]SSH 侧实现设备探测、原样命令、流式推送与进度、root/remount/reboot 映射
[why]ADB 的四个功能要能在 SSH 模式下等价工作
[how]用系统 ssh 包装 adb 风格 argv，push 走 cat > 并本地分块计数
[影响等级]A
[建议测试范围]真实开发板 SSH 执行脚本、推镜像、推文件、重启、断线重连
```

---

### Task 8: 进程级 SSH 监听器与 toolbase 接入

**Files:**
- Modify: `connection.py`（追加 `ConnectionMonitor`、`monitor()`、`peek_monitor()`；`info()` 合并在线状态）
- Modify: `toolbase.py:20`（导入）、`:148-185`（设备监听）
- Modify: `toolbase.py:269-302`（`snapshot` / `summary` 加 `connection`）
- Test: `tests/test_monitor.py`

**Interfaces:**
- Produces:
  - `class ConnectionMonitor(connection, interval=10.0)`：`register(tool)`、`unregister(tool)`、`start()`、`stop()`、`kick()`、`reset()`、`is_running()`、`devices() -> (list, str)`、`status() -> {online, error, model, deviceCount, checkedAt}`、`check() -> status`
  - `monitor() -> ConnectionMonitor`（单例）、`peek_monitor() -> ConnectionMonitor|None`
  - `reload(app_dir=None)` 先 `_monitor.reset()` 再丢弃 `_monitor`，避免旧监听器绑着旧配置
  - `Tool.reset_devices()`
  - `Tool.snapshot()` / `Tool.summary()` 增加 `"connection"` 键

- [ ] **Step 1: 写失败测试**

```python
# tests/test_monitor.py
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
        mon.register(tool)
        with helpers.path_env(self.tmp):
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
        mon.register(tool)
        with helpers.path_env(self.tmp):
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_monitor -v`
Expected: FAIL，`connection.ConnectionMonitor` 不存在。

- [ ] **Step 3: 实现监听器与 toolbase 接入**

`ConnectionMonitor` 用 `threading.RLock`、`threading.Event`：

- `register`：去重后加入；`self.start()`（ADB 模式里 `start()` 直接返回）。
- `start`：`mode != ssh` 直接返回；线程已活也返回；清 `_stop`、起 daemon 线程。
- `_run`：循环 `check()`，然后 `_wake.wait(interval)`；`stop()` 置 `_stop` 和 `_wake` 后 `join(2)`。
- `check()`：`sshkit.probe(self.connection.ssh())`；算 `online = bool(devices) and not error`、`model = devices[0]["model"] if devices else ""`；缓存；只有 `(devices 的 serial/state/model, error)` 变化时才 `_push`（第一次一定推）。
- `_push`：在锁外遍历工具副本，`apply_devices(devices, error)` + `publish_state()`，单个工具异常只吞掉该工具。
- `reset()`：`stop()`、清空工具列表与缓存、`_last_key = None`。
- `devices()` 返回缓存副本。
- `status()` 返回 `{online, error, model, deviceCount, checkedAt}`。

`connection.info()`：SSH 模式下 `mon = peek_monitor()` 存在时用 `mon.status()` 更新 `online/error/model/deviceCount`。

`connection.reload(app_dir=None)`：先 `mon = peek_monitor()`；有就 `mon.reset()`，再 `_monitor = None`；然后按 Task 1 的路径重读配置。这样切数据目录、测试里换临时目录都不会留着旧监听器。

`toolbase`：

- `ensure_device_watch()`：先 `connection.current(self.app_dir).mode()`；SSH 时 `connection.monitor().register(self)` 后返回，不创建 `DeviceWatcher`；ADB 时保持现状。
- `kick_device_watch()`：SSH 时取 `peek_monitor()` 并 `kick()`；ADB 时 `watcher.kick()`。
- `stop_device_watch()`：先 `peek_monitor()` 存在就 `unregister(self)`，再停 `_device_watch`。
- 新增 `reset_devices()`：`self._last_online = None`；`apply_devices([], "")`；`publish_state()`。
- `snapshot()` / `summary()` 增加 `"connection": connection.current(self.app_dir).info()`。注意 `publish_state` 在监听器线程里调用，`info()` 只读监听器状态，不能再取工具锁。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_monitor -v`
Expected: 6 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Add process-wide ssh connection monitor

[what]新增进程级 SSH 探测监听器，每 10 秒探测一次并把合成设备推给工具
[why]侧边栏和各工具页需要同一份连接状态，不能每个工具各探一次
[how]ConnectionMonitor 缓存探测结果并广播，toolbase 在 SSH 模式下改注册到它
[影响等级]B
[建议测试范围]SSH 在线/离线切换、设备状态推送、切回 ADB 后的行为
```

---

### Task 9: 服务端连接 API 与模式切换

**Files:**
- Modify: `server.py:32-43`（导入）、`:373-375`（`/api/tools`）、`:377` 附近（GET 路由）、`:483` 附近（POST 路由）、`:528`（start 分支）、`:180-188`（数据目录切换后重载）、`:613-662`（`AppServer`）
- Test: `tests/test_server.py`

**Interfaces:**
- Produces:
  - `GET /api/tools` → `{"tools": [...], "connection": {...}}`；SSH 模式 `tools` 不含 `always_adb`
  - `GET /api/connection` → `{"ok": true, "connection": {...}, "config": {...}, "running": [名称...]}`
  - `POST /api/connection`：`{mode, ssh}`；校验失败 400，有任务运行 409，成功 200
  - `POST /api/connection/test`：`{ssh}`；返回 `{"ok": bool, "online": bool, "model": str, "error": str}`，HTTP 200
  - `apply_connection_change()`、`connection_state()`
  - SSH 模式 `POST /api/always_adb/start` → 400

- [ ] **Step 1: 写失败测试**

```python
# tests/test_server.py
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

    def test_switch_data_dir_reloads_connection(self):
        other = tempfile.mkdtemp()
        os.makedirs(os.path.join(other, "config"))
        with mock.patch.object(server.datadir, "adopt", return_value={"ok": True}), \
             mock.patch.object(server.apppaths, "data_dir", return_value=other), \
             mock.patch.object(server.connection, "reload") as reload_mock:
            payload, status = server.switch_data_dir(other, adopt=True)
        self.assertEqual(status, 200)
        reload_mock.assert_called_once_with(other)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_server -v`
Expected: FAIL，`/api/tools` 没有 `connection`，`/api/connection` 404。

- [ ] **Step 3: 实现服务端改动**

- 导入 `connection`、`sshkit`。
- `connection_state()`：先 `connection.current().info()`；ADB 模式下统计 `registry.all()` 中每个工具 `getattr(tool, "devices", [])` 里 `state == "device"` 的数量，取最大值写入 `deviceCount` / `online`。
- `tool_summaries()`：SSH 模式过滤掉 `always_adb`。
- `GET /api/tools` → `{"tools": tool_summaries(), "connection": connection_state()}`。
- GET `/api/connection` → `{"ok": True, "connection": connection_state(), "config": connection.current().get(), "running": running_tool_names()}`。
- POST 路由在 `parts[:2] == ["api", "settings"]` 之后、工具分发之前接走 `parts[1] == "connection"`：
  - `len(parts) == 3 and parts[2] == "test"`：读 JSON，`ssh_raw = data.get("ssh") or {}`，`merged = connection.current().validate({"mode": "ssh", "ssh": ssh_raw})`；`ConfigError` → 400 `{"ok": False, "error": str(exc)}`；否则 `sshkit.probe(merged["ssh"])` 返回 200 `{"ok": bool(devices), "online": bool(devices), "model": ..., "error": err}`。
  - 无 `test`：有任务运行 → 409 `{"ok": False, "error": "%s 正在运行，先全部停止再切换" % "、".join(running)}`；`connection.current().save(data)`；`ConfigError` → 400；`OSError` → 500；成功后 `apply_connection_change()`，返回 200 `{"ok": True, "connection": connection_state(), "config": saved}`。
- `apply_connection_change()`：
  ```python
  mon = connection.monitor()
  mon.reset()
  for tool in registry.all():
      tool.stop_device_watch()
      tool.reset_devices()
  if connection.current().mode() == connection.MODE_SSH:
      for tool in registry.all():
          if tool.id != "always_adb" and hasattr(tool, "refresh_devices"):
              mon.register(tool)
      mon.start()
  ```
- `start` 分支：`if tool.id == "always_adb" and connection.current().mode() == connection.MODE_SSH: return self._send_json({"ok": False, "error": "本功能仅支持 ADB，切回 ADB 后可用"}, 400)`（放在 `tool.start(data)` 之前）。
- `_switch_data_dir`：`registry = new`、`DATA_DIR = ...` 之后调用 `connection.reload(DATA_DIR)` 和 `apply_connection_change()`。
- `AppServer.start()`：线程启动后调用 `apply_connection_change()`。
- `AppServer.stop()`：先 `mon = connection.peek_monitor(); if mon: mon.reset()`，再停工具。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_server -v`
Expected: 7 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Add connection api and mode switching

[what]新增 /api/connection 的读取、保存、测试接口，/api/tools 带连接状态
[why]前端需要在设置页切换并让所有功能立即跟随
[how]模式切换时停止设备监听、清空设备状态、重建 SSH 监听器，并拦截 always-adb
[影响等级]A
[建议测试范围]设置页切换、运行中切换被拒、SSH 测试连接、数据目录切换后重载
```

---

### Task 10: 设置页连接卡片

**Files:**
- Create: `static/connection.js`
- Modify: `static/settings.html:34`（`settings-scroll` 开头插入卡片）、`:150-155`（脚本标签）
- Modify: `static/style.css`（追加连接卡片需要的少量样式）
- Test: `tests/test_frontend_contract.py`

**Interfaces:**
- Produces `window.CONNECTION`：`info()`、`load() -> Promise`、`update(info)`、`isSsh()`、`targetText()`、`applyToolPage(info)`；设置卡片自己的逻辑也在这个 IIFE 里，只在 `#conn-card` 存在时执行。
- 设置卡片 DOM id：`conn-card`、`conn-mode-adb`、`conn-mode-ssh`、`conn-panel-adb`、`conn-panel-ssh`、`conn-host`、`conn-port`、`conn-user`、`conn-key`、`btn-conn-key`、`conn-policy`、`conn-timeout`、`conn-remount`、`conn-advanced`、`conn-status`、`btn-conn-test`、`btn-conn-save`、`conn-running`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_frontend_contract.py
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fp:
        return fp.read()


class SettingsConnectionCardTest(unittest.TestCase):
    def test_card_ids_present(self):
        html = read("static/settings.html")
        for dom_id in ("conn-card", "conn-mode-adb", "conn-mode-ssh",
                       "conn-panel-adb", "conn-panel-ssh", "conn-host",
                       "conn-port", "conn-user", "conn-key", "btn-conn-key",
                       "conn-policy", "conn-timeout", "conn-remount",
                       "conn-status", "btn-conn-test", "btn-conn-save",
                       "conn-running"):
            self.assertIn('id="%s"' % dom_id, html)

    def test_connection_js_loaded(self):
        self.assertIn("/static/connection.js", read("static/settings.html"))
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: FAIL，卡片 id 不存在。

- [ ] **Step 3: 实现卡片与 `connection.js`**

`settings.html` 在 `<div class="settings-scroll">` 后插入：

```html
<section class="settings-card" id="conn-card" aria-labelledby="conn-title">
  <h2 class="help-h2" id="conn-title">连接方式</h2>
  <p class="hint">所有功能共用这里的选择。切到 SSH 后，脚本执行、替换镜像、推送文件和反向隧道都走 SSH；切回 ADB 后行为与以前一致。</p>
  <div class="mode-tabs" role="tablist" aria-label="连接方式">
    <button class="mode-tab" id="conn-mode-adb" type="button" role="tab" aria-selected="true">ADB</button>
    <button class="mode-tab" id="conn-mode-ssh" type="button" role="tab" aria-selected="false">SSH</button>
  </div>
  <div class="mode-panel" id="conn-panel-adb">
    <p class="hint">设备的序列号和 adb 路径仍在各功能页选择。这里的检测只表示后端能不能列出设备。</p>
  </div>
  <div class="mode-panel" id="conn-panel-ssh" hidden>
    <div class="config-grid">
      <label class="field span-2"><span class="field-label">地址</span>
        <input id="conn-host" type="text" autocomplete="off" spellcheck="false" placeholder="192.168.100.1"></label>
      <label class="field"><span class="field-label">端口</span>
        <input id="conn-port" type="number" min="1" max="65535" step="1"></label>
      <label class="field"><span class="field-label">用户名</span>
        <input id="conn-user" type="text" autocomplete="off" spellcheck="false" placeholder="root"></label>
      <label class="field span-2"><span class="field-label">密钥文件</span>
        <span class="field-inline"><input id="conn-key" type="text" autocomplete="off" spellcheck="false" placeholder="留空则用 ssh-agent 和默认密钥">
          <button id="btn-conn-key" class="btn ghost" type="button">浏览…</button></span></label>
      <label class="field"><span class="field-label">主机密钥策略</span>
        <select id="conn-policy">
          <option value="accept-new">自动接受新的</option>
          <option value="yes">必须已存在</option>
          <option value="no">不校验</option>
        </select></label>
    </div>
    <details id="conn-advanced"><summary>高级</summary>
      <div class="config-grid">
        <label class="field"><span class="field-label">连接超时（秒）</span>
          <input id="conn-timeout" type="number" min="1" max="60" step="1"></label>
        <label class="field span-2"><span class="field-label">remount 命令</span>
          <input id="conn-remount" type="text" autocomplete="off" spellcheck="false"></label>
      </div>
    </details>
  </div>
  <p id="conn-status" class="form-status" role="status"></p>
  <div class="form-actions">
    <button id="btn-conn-test" class="btn ghost" type="button">测试连接</button>
    <button id="btn-conn-save" class="btn primary" type="button">保存并应用</button>
  </div>
  <p id="conn-running" class="hint" data-tone="error" hidden></p>
</section>
```

`connection.js`：

- 模块状态：`cached = null`、`dirtyMode = null`。
- `load()`：GET `/api/connection` → `data.connection`；填 `cached`；若设置卡片存在则渲染；返回 Promise。
- `update(info)`：`cached = info`；`paintPage()`。
- `info()`：返回 `cached`（没有就返回 `null`）。
- `isSsh()`：`cached && cached.mode === 'ssh'`。
- `targetText()`：SSH 时 `user@host`，否则 `''`。
- `applyToolPage(info)`：`info = info || cached`；只操作存在的节点：`#adb-path-field` / `#device-field` 的 `hidden = ssh`，`#ssh-target-field` 的 `hidden = !ssh`；`#ssh-target-text` 填 `"SSH · user@host:port"`；`#ssh-target-field` 的 `data-tone` 按 `online ? "ok" : "warn"`。
- 卡片渲染：按 `data.config.mode` 选按钮（`is-active` / `aria-selected`）和面板 `hidden`；填 SSH 字段；状态行：ADB `"ADB · N 台设备在线"`（0 台时 `"ADB · 没有检测到在线设备"`），SSH 在线 `"root@host · RK3576"` 绿色，离线红色显示 `connection.error || "还没测试连接"`；有 `running` 时显示 `conn-running` 并禁用保存。
- 模式按钮：只切本地 `dirtyMode`、面板和按钮状态，不发请求；ADB 面板下「测试连接」禁用。
- 浏览密钥：`window.pywebview.api.pick_files('')`，取第一个；网页版把焦点放到输入框，提示手动填写。
- 测试：POST `/api/connection/test`，body `{"ssh": collectSsh()}`；成功/失败都写到 `#conn-status`；不保存。
- 保存：POST `/api/connection`，body `{"mode": dirtyMode || cached.mode, "ssh": collectSsh()}`；400/409 把 `error` 写到 `#conn-status`；成功用返回的 `connection` 调 `update()`，再 `window.SHELL && SHELL.refresh()`。
- `load()` 在脚本加载后立即调用；失败静默（设置页其他部分还能用）。
- `style.css` 只补：`#conn-card .mode-tabs { margin-top: 4px; }`、`#conn-status { min-height: 18px; }`、`#conn-status[data-tone="ok"] { color: var(--good-ink); }`、`#conn-status[data-tone="error"] { color: var(--crit-ink); }`、`#conn-running { margin: 10px 0 0; }`、`#ssh-target-field .conn-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warning); }`、`#ssh-target-field[data-tone="ok"] .conn-dot { background: var(--good); }`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: 2 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Add connection card to settings

[what]设置页顶部新增 ADB / SSH 连接卡片，含测试连接与保存并应用
[why]需要一个地方切一次就让所有功能跟着走
[how]新增 connection.js 管理卡片状态，保存后调用 /api/connection 并刷新侧边栏
[影响等级]B
[建议测试范围]两种模式切换、空地址提示、测试连接、运行中禁用保存、密钥浏览
```

---

### Task 11: 侧边栏连接行与 SSH 设备行

**Files:**
- Modify: `static/shell.js:13-22`（状态变量）、`:73-76`（底部行）、`:163-224`（设备行）、`:336-350`（轮询）
- Modify: `static/style.css`（连接行可点击样式）
- Test: `tests/test_frontend_contract.py`（追加）

**Interfaces:**
- Consumes: `GET /api/tools` 的 `connection` 字段
- Produces: 侧边栏「连接」行 id `connection-status` / `connection-text`；SSH 设备行显示在线/离线；连接行点击进 `/settings#conn-card`

- [ ] **Step 1: 写失败测试**

```python
class SidebarConnectionTest(unittest.TestCase):
    def test_sidebar_has_connection_row(self):
        js = read("static/shell.js")
        self.assertIn("'连接'", js)
        self.assertIn("connectionInfo", js)
        self.assertIn("connection-text", js)
        self.assertIn("/settings#conn-card", js)

    def test_ssh_device_branch(self):
        js = read("static/shell.js")
        self.assertIn("SSH 在线", js)
        self.assertIn("连不上", js)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

- 顶部状态加 `var connectionInfo = null;`
- `buildSidebar()` 在「服务」行之前插入：
  ```js
  var connRow = buildStatusRow('connection-status', '连接', 'connection-text', '读取中');
  connRow.classList.add('is-link');
  connRow.tabIndex = 0;
  connRow.setAttribute('role', 'link');
  connRow.setAttribute('aria-label', '连接方式，点击到设置页切换');
  connRow.addEventListener('click', function () { location.href = '/settings#conn-card'; });
  connRow.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); location.href = '/settings#conn-card'; }
  });
  foot.insertBefore(connRow, foot.firstChild);
  ```
- `renderConnection()`：
  - 无 `connectionInfo`：tone `connecting`，文字 `读取中`。
  - ADB：tone `idle`，文字 `ADB`，title 说明在设置页切换。
  - SSH：文字 `SSH ` + `user@host:port`；在线 tone `ok` + title 带型号，离线 tone `warn` + title 带 `error`。
- `renderDevice()` 开头（拿到 row 之后）加 SSH 分支：`connectionInfo && connectionInfo.mode === 'ssh'` 时 —— 在线 `SSH 在线 · 型号` tone `ok`；离线 `连不上 · 简短原因`（取 `error` 第一行、截 40 字符）tone `warn`；然后 `return`。
- `refresh()`：`connectionInfo = data.connection || null; renderConnection(); renderDevice();`，然后再 `paint(tools)`。
- `paint(tools)` 里如果当前工具有 `connection` 字段（Task 8 加进 summary），先同步到 `connectionInfo` 并重画连接行/设备行 —— 这样 SSE 状态比 3 秒轮询更早把 SSH 在线状态显示出来。
- 导出对象不变（`refresh` / `setPush` / `setDevices`）。
- `style.css` 追加 `.conn.is-link { cursor: pointer; }`、`.conn.is-link:hover .conn-text { color: var(--ink); }`、`.conn.is-link:focus-visible { outline: 1px solid var(--accent); outline-offset: 2px; }`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: 4 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Show connection state in sidebar

[what]侧边栏新增「连接」行，SSH 模式下设备行显示在线/连不上
[why]切到 SSH 后没有 adb 设备列表，仍要能一眼看出通道状态
[how]读取 /api/tools 的 connection 字段，点击连接行跳到设置页卡片
[影响等级]C
[建议测试范围]ADB/SSH 两种侧边栏、SSH 在线与离线、点击跳转
```

---

### Task 12: 三个功能页按模式切换字段

**Files:**
- Modify: `static/scripts.html:56-66`、`static/scripts.js`（`updateTargetLabel`、`collectConfig`、`applyConfig`、`refreshDevices`、启动入口）
- Modify: `static/replace-image.html:68-78`、`static/replace-image.js`（同结构）
- Modify: `static/push-file.html:57-67`、`static/push-file.js`（同结构）
- Test: `tests/test_frontend_contract.py`（追加）

**Interfaces:**
- Consumes: `CONNECTION.load/update/isSsh/applyToolPage/targetText`
- Produces: 三个页面都有 `adb-path-field`、`device-field`、`ssh-target-field`；SSH 模式提交配置时不带 `adbPath` / `serial`

- [ ] **Step 1: 写失败测试**

```python
class ToolPagesTest(unittest.TestCase):
    def test_pages_have_mode_blocks(self):
        for page in ("scripts.html", "replace-image.html", "push-file.html"):
            html = read("static/" + page)
            self.assertIn('id="adb-path-field"', html)
            self.assertIn('id="device-field"', html)
            self.assertIn('id="ssh-target-field"', html)
            self.assertIn("/static/connection.js", html)

    def test_tool_js_skips_serial_in_ssh(self):
        for js in ("scripts.js", "replace-image.js", "push-file.js"):
            text = read("static/" + js)
            self.assertIn("CONNECTION", text)
            self.assertIn("isSsh", text)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

三个 HTML：

- 把现有「adb 路径」`label` 包上 `id="adb-path-field"`。
- 把现有「目标设备」`div.field` 包上 `id="device-field"`。
- 紧跟其后加：
  ```html
  <div class="field span-2" id="ssh-target-field" hidden>
    <span class="field-label">SSH 目标</span>
    <span class="field-inline ro">
      <span class="conn-dot" aria-hidden="true"></span>
      <span id="ssh-target-text">SSH · —</span>
      <span class="ro-note">只读</span>
    </span>
  </div>
  ```
- 三个页面在 `common.js` / `shell.js` 之后、页面 JS 之前加 `<script src="/static/connection.js"></script>`。

三个 JS：

- 启动时 `CONNECTION.load().then(function (info) { CONNECTION.applyToolPage(info); updateTargetLabel(); });`
- `onState` 回调里如果 `s.connection`，先 `CONNECTION.update(s.connection)` 再 `CONNECTION.applyToolPage(s.connection)`。
- `collectConfig()`：保留原对象，但在末尾判断 `if (!CONNECTION.isSsh()) { cfg.adbPath = ...; cfg.serial = ...; }`；SSH 时不带这两个键（服务端会保留旧值）。
- `applyConfig()`：SSH 时跳过 `fillSerialOptions` / 不覆盖隐藏字段；ADB 时保持现状。
- 目标读数：`updateTargetLabel()` / `updateDeviceLabel()` / 推送页对应函数里，SSH 优先返回 `CONNECTION.targetText()`（`user@host`），否则走原来的 `cfg.serial` / 在线设备逻辑。
- `refreshDevices()` 在 SSH 时仍可调用（后端返回合成设备），但页面上没有 select，不用填；保留调用不影响。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: 6 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Adapt tool pages to ssh mode

[what]脚本执行、替换镜像、推送文件在 SSH 模式下隐藏 adb 路径和目标设备，显示只读 SSH 目标
[why]SSH 没有 adb 序列号可选，但页面布局要和 ADB 模式保持一致
[how]新增 connection.js 公共切换逻辑，三个页面用同一组 field id 包裹原字段
[影响等级]B
[建议测试范围]三页在 ADB/SSH 下的显示、保存参数不覆盖已存序列号、目标读数
```

---

### Task 13: 反向隧道后端双模式

**Files:**
- Modify: `tools/ssh_tunnel.py:66-122`（命令拼装）、`:142-185`（配置）、`:198-235`（读数/预览）、`:254-340`（主循环里的端口使用）
- Test: `tests/test_tunnel.py`

**Interfaces:**
- Consumes: `connection.current(tool.app_dir)`
- Produces:
  - 配置新增 `targets = {"adb": {...}, "ssh": {...}}`，每组 `{remotePort, localHost, localPort}`；ADB 默认 `5037 / 127.0.0.1 / 5037`，SSH 默认 `2222 / "" / 22`
  - `SshTunnelTool.active_target(cfg=None) -> dict`；SSH 组 `localHost` 为空时用 `connection.ssh()["host"]`
  - `SshTunnelTool.build_ssh_command(cfg)` / `display_command(cfg)` 为实例方法（替换模块级同名函数）
  - `name = "反向隧道"`；`desc` 是属性，SSH 下 `"把开发板的 SSH 反向映射到远程主机"`，ADB 下保留原文案
  - `extra()` 增加 `mode`、`modeName`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tunnel.py
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_tunnel -v`
Expected: FAIL，没有 `targets`。

- [ ] **Step 3: 实现**

- `default_config` 去掉顶层 `remotePort/localHost/localPort`，加 `targets`（两组）。`sanitize_config`：
  - `base_targets = base.get("targets") or 默认 targets`；
  - `raw_targets = raw.get("targets") if dict else {}`；
  - 若 `raw` 有旧顶层三字段且没有 `targets`，把它们并入 `adb` 组；
  - 两组分别用 `as_int` / `as_str` 清洗；ADB 的 `localHost` `nonempty=True`，SSH 的允许空；
  - 返回只含 `targets`（不再保留顶层三字段）。
- `active_target(cfg=None)` 按 `connection.current(self.app_dir).mode()` 选组；SSH 且 `localHost` 为空时填 `connection.ssh()["host"]`。
- `build_ssh_command(cfg)` / `display_command(cfg)` 改成实例方法，内部用 `active_target`；`display_command` 的空主机占位逻辑保留。删除模块级同名函数，更新所有调用点。
- `remote_spec` / `local_spec` 用 `active_target`。
- `name = "反向隧道"`；`desc` 改成 `@property`。
- `extra()` 加 `"mode"`、`"modeName"`。
- `run_loop` 里所有 `cfg["remotePort"]` 改成 `target = self.active_target(cfg)` 后的 `target["remotePort"]`；`FORWARD_FAIL_RE` 的端口解析仍从 ssh 输出取。
- `env_command` / `ANDROID_ADB_SERVER_PORT` 提示只在 ADB 模式发；SSH 模式跳过 `maybePromptEnv`（前端 Task 14 同时处理）。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_tunnel -v`
Expected: 7 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Support dual targets in reverse tunnel

[what]反向隧道按当前连接模式使用两套本地目标，标题改为通用「反向隧道」
[why]SSH 模式要把开发板的 22 转发到远端，而不是本机 adb server
[how]配置改为 targets.adb / targets.ssh，旧配置自动迁到 adb 组，命令按模式拼装
[影响等级]A
[建议测试范围]ADB 隧道、SSH 隧道、旧配置迁移、端口占用提示、GatewayPorts
```

---

### Task 14: 反向隧道页面双模式

**Files:**
- Modify: `static/ssh-tunnel.html:6`（title）、`:22`（h1）
- Modify: `static/ssh-tunnel.js`（`collectConfig` / `applyConfig` / `maybePromptEnv` / `updatePortHint` / 标题与说明）
- Test: `tests/test_frontend_contract.py`（追加）

**Interfaces:**
- Consumes: `CONNECTION.load/isSsh`、后端 config 的 `targets`
- Produces: 页面不加新字段；ADB / SSH 各显示自己那组 `remotePort/localHost/localPort`，远端服务器字段共用

- [ ] **Step 1: 写失败测试**

```python
class TunnelPageTest(unittest.TestCase):
    def test_title_and_targets(self):
        html = read("static/ssh-tunnel.html")
        self.assertIn("<h1>反向隧道</h1>", html)
        js = read("static/ssh-tunnel.js")
        self.assertIn("targets", js)
        self.assertIn("CONNECTION", js)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

- `ssh-tunnel.html` 的 `<title>` 改成 `反向隧道 · ADB Tool Studio`，`<h1>` 改成 `反向隧道`；`/static/connection.js` 加在其他脚本之后。
- `ssh-tunnel.js`：
  - `var targets = null;`（保存两组值）。
  - 启动 `CONNECTION.load().then(function () { if (lastState) applyConfig(lastState.config); document.title = '反向隧道 · ADB Tool Studio'; });`，其中 `lastState` 是现有 `onState` 缓存的最近一次快照。
  - `applyConfig(cfg)`：`targets = cfg.targets || null`；`var sshMode = CONNECTION.isSsh();`，取 `targets[sshMode ? 'ssh' : 'adb']` 填 `#cfg-remoteport/#cfg-localhost/#cfg-localport`；SSH 组 `localHost` 为空时 placeholder 用 `CONNECTION.info().ssh.host`。
  - `collectConfig()`：复制 `targets`（深拷贝），把当前三个输入写进当前模式组，再返回 `{..., targets: copy}`；不再返回顶层三字段。
  - `maybePromptEnv(s)`：`if (CONNECTION.isSsh()) return;`（SSH 隧道没有 `ANDROID_ADB_SERVER_PORT`）。
  - `updatePortHint(extra)`：SSH 模式隐藏端口提示（`el.hidden = true`），ADB 模式保持现状。
  - 说明文字：`#tool-desc` 由后端 `desc` 自动变；名称栏若用静态文案不动。
- 保存/启动仍走 `client.saveConfig(collectConfig())`；后端 `sanitize_config` 会保留另一组。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: 7 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Switch tunnel page between adb and ssh targets

[what]反向隧道页在两种模式下显示各自的远程绑定端口和本地目标，标题通用化
[why]SSH 模式转发的是开发板 22，页面不能还写着本机 5037
[how]前端按 connection 模式读写 targets.adb / targets.ssh，另一组原样提交
[影响等级]B
[建议测试范围]两模式页面显示、切换后保存、命令预览、环境变量提示
```

---

### Task 15: always-adb 拦截与帮助页

**Files:**
- Modify: `static/always-adb.html`（名称栏下加提示）
- Modify: `static/always-adb.js:24-30`（启动时读模式）、`:250-258`（禁用启动）
- Modify: `static/help.html:26-31`（目录）、`:140` 前（新增 SSH 一节）
- Test: `tests/test_frontend_contract.py`（追加）

**Interfaces:**
- Consumes: `CONNECTION.load/isSsh`
- Produces: SSH 模式直接访问 `/always-adb` 时显示 `本功能仅支持 ADB，切回 ADB 后可用`，启动/单次按钮禁用

- [ ] **Step 1: 写失败测试**

```python
class AlwaysAdbAndHelpTest(unittest.TestCase):
    def test_notice_and_guard(self):
        html = read("static/always-adb.html")
        self.assertIn('id="adb-only-notice"', html)
        self.assertIn("本功能仅支持 ADB", html)
        js = read("static/always-adb.js")
        self.assertIn("CONNECTION", js)
        self.assertIn("adbOnly", js)

    def test_help_has_ssh_section(self):
        html = read("static/help.html")
        self.assertIn('id="ssh"', html)
        self.assertIn("BatchMode", html)
        self.assertIn("loopback", html)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: FAIL。

- [ ] **Step 3: 实现**

- `always-adb.html` 在 `#tool-desc` 后加：
  ```html
  <p id="adb-only-notice" class="hint" data-tone="error" hidden>本功能仅支持 ADB，切回 ADB 后可用。</p>
  ```
- `always-adb.js` 加 `var adbOnly = false;`；启动时：
  ```js
  CONNECTION.load().then(function (info) {
    adbOnly = !!info && info.mode === 'ssh';
    var notice = $('#adb-only-notice');
    if (notice) notice.hidden = !adbOnly;
    if (adbOnly) {
      $('#btn-start').disabled = true;
      $('#btn-once').disabled = true;
    }
  });
  ```
  `renderState` 里 `$('#btn-start').disabled = running || adbOnly;`、`$('#btn-once').disabled = running || adbOnly;`。
- `help.html` 目录加 `<a href="#ssh">SSH 连接</a>`，在 `#faq` 前加一节：
  ```html
  <section class="help-section" id="ssh">
    <h2 class="help-h2">SSH 连接</h2>
    <p class="help-note">设置页顶部的连接方式切到 SSH 后，脚本执行、替换镜像、推送文件和反向隧道都改走 SSH。</p>
    <div class="help-grid">
      <article class="help-card">
        <h3>前提</h3>
        <ul>
          <li>开发板能用密钥免密登录，本机已把公钥放进开发板的 authorized_keys，或已 ssh-add</li>
          <li>登录用户是 root；SSH 模式不再执行 adb root</li>
          <li>软件固定带 BatchMode=yes，不会停在密码提示上</li>
        </ul>
      </article>
      <article class="help-card">
        <h3>隧道与主机密钥</h3>
        <ul>
          <li>反向隧道默认只绑远端 loopback，远端 sshd 的 GatewayPorts 保持关闭</li>
          <li>主机密钥默认 accept-new：第一次记录，之后变化会报错</li>
          <li>SSH 登录是 root，开发板的 SSH 暴露面要自己控制</li>
        </ul>
      </article>
    </div>
  </section>
  ```
- 页面脚本标签加 `/static/connection.js`。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_frontend_contract -v`
Expected: 9 tests PASS。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Guard always-adb and document ssh mode

[what]SSH 模式下 always-adb 显示提示并禁用启动，帮助页补 SSH 前提
[why]这个功能只依赖 adb，切到 SSH 后不能假装可用
[how]前端读取连接模式禁用按钮，后端启动接口同时返回 400
[影响等级]C
[建议测试范围]SSH 模式访问 always-adb、切回 ADB 后恢复、帮助页链接
```

---

### Task 16: 全量测试、README 与收尾

**Files:**
- Modify: `README.md`（用法一节后加「连接方式」）
- Test: `tests/` 全部

**Interfaces:**
- Consumes: 前 15 个任务
- Produces: 一条可重复执行的测试命令；README 说明两种模式和 SSH 前提

- [ ] **Step 1: 跑全量测试**

Run: `python3 -m unittest discover -s tests -v`
Expected: 全部 PASS，无 traceback。

- [ ] **Step 2: 语法检查**

Run: `python3 -m py_compile connection.py sshkit.py transport.py toolbase.py server.py tools/*.py`
Expected: 无输出、退出码 0。

- [ ] **Step 3: 更新 README**

在「## 用法」之前或之后加：

```markdown
## 连接方式

设置页顶部的「连接方式」在 ADB 和 SSH 之间切换，切一次，脚本执行、替换镜像、
推送文件和反向隧道全部跟随。SSH 使用系统 OpenSSH（Windows 10+ 自带），只支持
密钥 / ssh-agent 免密，不提供密码输入：

- 地址、端口、用户名、密钥文件分开填，不用 `~/.ssh/config` 别名
- 登录用户是 root；SSH 模式下没有 adb root / remount 步骤
- 反向隧道在 SSH 模式下默认把开发板的 22 转发到远端，页面不加新字段
- 切回 ADB 后行为与以前一致，原来的序列号不会被 SSH 的合成序列号覆盖

跑测试（标准库 unittest，不装 pytest）：

```bash
python3 -m unittest discover -s tests -v
```
```

- [ ] **Step 4: 手工检查清单（在真实开发板上）**

1. 设置页切到 SSH，填 `192.168.100.1 / 22 / root`，测试连接显示型号。
2. 脚本执行：导入一个 `echo hello` 脚本，执行成功、日志有 ssh 命令行。
3. 替换镜像：用小镜像写一个测试分区，确认推送进度、`dd`、`sync`、重启。
4. 推送文件：推一个大文件，日志有百分比进度；勾「先 remount」能成功。
5. 反向隧道：SSH 模式建立后，在 `10.27.12.198` 上 `ssh dev` 能进开发板。
6. 直接访问 `/always-adb`，确认提示与按钮禁用。
7. 切回 ADB，确认三个页面恢复 adb 路径/目标设备，原序列号还在。
8. 三个页面在 SSH 模式下布局与 ADB 一致，没有多出 adb 字段。

- [ ] **Step 5: 提交（只展示，不执行）**

```
[feature][JIRA#][项目名][N]Document ssh mode and add test suite

[what]README 补充连接方式说明，测试目录覆盖 ADB/SSH 两条通道
[why]切换模式后行为和前提需要写清楚，回归也需要一条命令
[how]新增 tests/ 标准库用例与假 ssh/adb，README 增加连接方式和测试命令
[影响等级]C
[建议测试范围]全量 unittest、真实开发板四功能、切回 ADB 回归
```

---

## Self-Review

- **Spec coverage:** §5 配置 → Task 1；§6 传输层 → Task 3/7；§7.1–7.3 → Task 4/5/6/7；§7.4 → Task 13/14；§8.1 → Task 10；§8.2 → Task 11；§8.3 → Task 12；§8.4 → Task 9/15；§8.5 → Task 15；§9 → Task 9；§10 → Task 1/7/9；§12 → 各任务测试 + Task 16；§13 顺序一致。
- **类型一致性:** `transport.make(tool)`、`Transport.devices/select/build`、`Bound.preview/run/popen/problem`、`ConnectionMonitor.devices/status/register/reset` 在后续任务中保持同名同参。
- **未覆盖项:** 无。SSH 真实设备行为只能在 Task 16 手工验证，计划已列出清单。
