# -*- coding: utf-8 -*-
"""在 Windows 上安静地起子进程 —— 别让每个 adb 调用都闪一个黑框。

桌面版打包成 console=False，或者用 pythonw.exe 启动时，本进程**没有控制台**。
这时候再 subprocess 起子进程，Windows 会给每个子进程新建一个控制台窗口。
always-adb 每秒重试一次，屏幕上就是不停地闪 cmd —— 「一直弹黑框」就是这么来的。

CREATE_NO_WINDOW 干的正是这件事：子进程照常跑、照常从管道收得到输出，
只是不额外给它开一个窗口。

非 Windows 上这个常量是 0，等于什么都不加，所以调用方不用自己判断平台：

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, **spawn.quiet())
"""

import subprocess

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def quiet():
    """摊进 subprocess 的调用参数里。"""
    if CREATE_NO_WINDOW:
        return {"creationflags": CREATE_NO_WINDOW}
    return {}
