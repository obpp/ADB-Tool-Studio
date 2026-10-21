# -*- coding: utf-8 -*-
"""测「桌面版起子进程时会不会弹黑框」。

用 `dev.bat popup` 跑（它会用 pythonw 启动本脚本）。

分两组各起 20 个子进程，中间停 2 秒：

  第一组 **对照组**：故意不带 CREATE_NO_WINDOW。你应该看到黑框不停闪 ——
          这一组是用来证明这个测试确实看得出来，不是你在疑神疑鬼。
  第二组 **被测组**：走 adbkit.run()，也就是真正执行 adb 的那条代码路径。

第一组闪、第二组不闪 = 修复生效。两组都闪 = spawn.quiet() 没起作用。

**必须用 pythonw 跑。** 黑框只在「当前进程没有控制台」时才会出现；
用普通 python.exe 跑，子进程直接继承控制台，两组都不闪，什么也测不出来。
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import adbkit                                    # noqa: E402
import apppaths                                  # noqa: E402

BURST = 20
GAP = 0.25

SHELL = os.environ.get("COMSPEC") or "cmd.exe"


def has_console():
    """当前进程有没有控制台？没控制台才可能出现黑框。"""
    if sys.platform != "win32":
        return True
    import ctypes
    return bool(ctypes.windll.kernel32.GetConsoleWindow())


def run_control():
    """对照组：不带任何 flag 直接起子进程，用来把「黑框长什么样」演一遍。"""
    for _ in range(BURST):
        subprocess.run([SHELL, "/c", "echo", "hi"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
        time.sleep(GAP)


def run_real():
    """被测组：adbkit.run() 才是真正调 adb 走的那条路径。"""
    for _ in range(BURST):
        adbkit.run(SHELL, "", ["/c", "echo", "hi"], 10)
        time.sleep(GAP)


def message_box(title, text):
    """pythonw 下没有控制台，只能弹框告诉人结论。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def write_log(lines):
    try:
        os.makedirs(apppaths.logs_dir(), exist_ok=True)
        path = os.path.join(apppaths.logs_dir(), "devcheck.log")
        with open(path, "a", encoding="utf-8") as fp:
            fp.write("%s\n%s\n\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "\n".join(lines)))
        return path
    except OSError:
        return ""


def main():
    if sys.platform != "win32":
        print("这个检查只在 Windows 上有意义。")
        return 0

    if has_console():
        message_box(
            "检查没意义",
            "当前进程有控制台，子进程会直接继承它，两组都不会闪黑框。\n\n"
            "请用 pythonw 跑：dev.bat popup")
        return 1

    run_control()
    time.sleep(2.0)
    run_real()
    time.sleep(0.5)

    lines = [
        "黑框检查",
        "",
        "刚才屏幕上应该发生了两件事：",
        "  前 %d 次（对照组）：不停闪黑框  ← 这是错误做法长什么样" % BURST,
        "  后 %d 次（被测组）：不该有任何黑框" % BURST,
        "",
        "如果你确实看到「前一半闪、后一半干净」，说明修复生效了。",
        "如果两段都在闪，说明 spawn.quiet() 没起作用，把日志发给开发者。",
        "",
        "（必须用 pythonw 跑才有意义 —— 普通 python.exe 有控制台，两组都不会闪）",
    ]
    path = write_log(lines)
    message_box("ADB Tool Studio 黑框检查",
                "\n".join(lines[3:]) + ("\n\n日志: %s" % path if path else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
