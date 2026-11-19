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
noise = os.environ.get("FAKE_SSH_NOISE")
if noise:
    sys.stdout.flush()
    sys.stderr.write(noise + "\\n")
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
    elif os.environ.get("FAKE_SHELL_CR"):
        sys.stdout.write(os.environ["FAKE_SHELL_CR"].replace("\\\\r", "\\r"))
        sys.stdout.write("ok\\n")
    else:
        print(os.environ.get("FAKE_SHELL_OUT", "ok"))
else:
    print("ok")
'''


def fake_ssh(dirpath, name="ssh"):
    return _install(dirpath, name, FAKE_SSH)


def fake_adb(dirpath, name="adb"):
    return _install(dirpath, name, FAKE_ADB)


@contextlib.contextmanager
def path_env(dirpath):
    env = {"PATH": dirpath + os.pathsep + os.environ.get("PATH", "")}
    with mock.patch.dict(os.environ, env):
        yield
