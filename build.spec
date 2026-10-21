# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（在项目根目录跑）。

    pyinstaller build.spec --noconfirm --clean

打包有三处特别容易踩，漏一个就是「编译得过、一运行就崩」：

  1. pywebview 的 webview/js/*.js 和 webview/lib/*.dll 是**包数据**，
     不在 .py 里，PyInstaller 的静态分析看不见 —— 必须 collect_data_files 收进来。
     漏了 js 的表现是窗口能开、内容全白。
  2. Windows 后端走 pythonnet（import clr），而且 webview.platforms.* 是运行时
     按平台挑着 import 的，静态分析同样看不到 —— 得写进 hiddenimports。
  3. static/ 和 icon.ico 要跟着打包，运行时从 sys._MEIPASS 里读（见 apppaths.py）。

注意：打包要在**干净的虚拟环境**里做。在一个装了几百个包的环境里跑 PyInstaller，
它会把能扫到的都塞进去，exe 会大得离谱。
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# 单文件还是文件夹。build.bat dir 会把它设成 0。
#
# 开发期间建议用文件夹（ONEFILE=0）：单文件每次都要把整个包重新压一遍，改一行
# 代码也要等很久；文件夹模式只重写变了的那些文件，快得多，启动也快。
# 对外发的时候再换成单文件。
ONEFILE = os.environ.get("MYADB_ONEFILE", "1") != "0"

ROOT = os.path.abspath(SPECPATH)

# ---------------------------------------------------------------- 资源

datas = collect_data_files("webview")       # webview/js/*.js + webview/lib/*.dll

# pythonnet / clr_loader 的运行时 DLL 也是包数据；装了才有，没有就跳过
for _pkg in ("pythonnet", "clr_loader"):
    try:
        datas += collect_data_files(_pkg)
    except Exception:
        pass

datas += [
    (os.path.join(ROOT, "static"), "static"),
    (os.path.join(ROOT, "icon.ico"), "."),
]

hiddenimports = [
    # pythonnet：Windows 上 pywebview 靠它跟 WinForms/WebView2 说话
    "clr",
    "clr_loader",
    # pywebview 的平台后端是运行时挑的，静态分析扫不到
    "webview.platforms.winforms",
    "webview.platforms.win32",
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    # 托盘：pystray 也是按平台挑后端
    "pystray._win32",
]

a = Analysis(
    [os.path.join(ROOT, "desktop", "app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],       # pystray 在 Windows 上用 _win32，用不着 tk
    noarchive=False,
)

pyz = PYZ(a.pure)

ICON = os.path.join(ROOT, "icon.ico")

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="ADB-Tool-Studio",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,              # UPX 压缩会明显拉高杀软误报率，不值得
        runtime_tmpdir=None,
        console=False,          # 不要黑框；日志写 logs/app.log
        icon=ICON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ADB-Tool-Studio",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=ICON,
    )
    COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="ADB-Tool-Studio",
    )
