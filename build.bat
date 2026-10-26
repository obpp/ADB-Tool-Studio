@echo off
chcp 936 >nul
REM ===========================================================================
REM  把 ADB-Tool-Studio 打包成 Windows 桌面 exe。
REM
REM    build.bat          普通打包，单文件 exe
REM    build.bat dir      出文件夹而不是单文件：改代码后重打快得多，
REM                       启动也快、杀软误报少。**开发期间建议一直用这个**
REM    build.bat clean    清掉 PyInstaller 缓存重打。只在改了 build.spec
REM                       或者怀疑缓存脏了的时候才需要 —— 平时加它纯属浪费时间
REM
REM  日常改代码想快速验证，别用本文件，用 dev.bat（源码模式，秒级）。
REM
REM  注意：本文件必须存成 GBK 编码、CRLF 换行。cmd 是按系统 ANSI 代码页
REM  逐字节读批处理的，存成 UTF-8 的话中文注释会把解析器带跑偏。
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist "build.spec" goto :wrongdir
if not exist "desktop\app.py" goto :wrongdir

set "CLEAN="
set "MYADB_ONEFILE=1"
if /i "%~1"=="clean" set "CLEAN=--clean"
if /i "%~1"=="dir"   set "MYADB_ONEFILE=0"

echo.
echo [1/6] 找个能用的 Python ...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython
%PY% --version
if errorlevel 1 goto :fail
echo     OK: %PY%

echo.
echo [2/6] 准备虚拟环境 .venv-build ...
if not exist ".venv-build\Scripts\python.exe" %PY% -m venv .venv-build
if errorlevel 1 goto :fail
set "VPY=.venv-build\Scripts\python.exe"
if not exist "%VPY%" goto :fail
echo     OK

echo.
echo [3/6] 检查依赖 ...
"%VPY%" -c "import webview, pystray, PIL, PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo     缺东西，装一下。第一次跑大概一分钟 ...
    "%VPY%" -m pip install -r requirements-desktop.txt pyinstaller
    if errorlevel 1 goto :pipfail
) else (
    echo     都在，跳过。
)

echo.
echo [4/6] 生成图标 ...
"%VPY%" desktop\make_icon.py
if errorlevel 1 goto :fail

echo.
echo [5/6] 关掉正在跑的实例 ...
call :freetarget
if errorlevel 1 goto :locked

echo.
echo [6/6] 打包（输出 dist\）...
"%VPY%" -m PyInstaller build.spec --noconfirm %CLEAN%
if errorlevel 1 goto :fail

echo.
echo ===========================================================================
if "%MYADB_ONEFILE%"=="1" (
    echo  完成： dist\ADB-Tool-Studio.exe
) else (
    echo  完成： dist\ADB-Tool-Studio\ADB-Tool-Studio.exe
)
echo.
echo  下一步：
echo    1. 双击它，试试窗口能不能开
echo    2. 能开就拷到你想放的地方（配置和日志生成在 exe 旁边）
echo    3. 想开机自启：右键托盘图标，勾上「开机自启」
echo.
echo  双击没反应就看 exe 旁边的 logs\app.log
echo.
echo  改代码后想快速验证，用 dev.bat 跑源码模式，别每次都重新打包。
echo ===========================================================================
echo.
pause
exit /b 0


REM ---------------------------------------------------------------- 子过程

:freetarget
REM 正在跑的 exe 锁着 dist 里的同名文件。PyInstaller 最后一步要删掉它重建，
REM 删不掉就报 PermissionError [WinError 5] 拒绝访问 —— 而且那时候分析已经
REM 全跑完了，等于白等一场。所以这里先把进程关掉、再把旧 exe 删一次，
REM 有问题当场就说，别拖到最后。
set "KILLED=0"
set "LOCKED=0"
taskkill /f /im ADB-Tool-Studio.exe >nul 2>nul
if not errorlevel 1 set "KILLED=1"
if "%KILLED%"=="1" (
    echo     发现 ADB-Tool-Studio.exe 还在跑，已经关掉它。
    echo     多半是之前双击开的那个缩在托盘里没退。
    ping -n 3 127.0.0.1 >nul
) else (
    echo     没有实例在跑。
)
call :delstale "dist\ADB-Tool-Studio.exe"
call :delstale "dist\ADB-Tool-Studio\ADB-Tool-Studio.exe"
if "%LOCKED%"=="1" exit /b 1
exit /b 0

:delstale
REM 单文件和文件夹两种模式的产物路径不一样，两个都试一下。
if not exist "%~1" exit /b 0
del /f /q "%~1" >nul 2>nul
if exist "%~1" (
    echo     [占用] %~1
    set "LOCKED=1"
) else (
    echo     清掉了旧的 %~1
)
exit /b 0


:wrongdir
echo.
echo   [错误] 当前目录不对，没找到 build.spec。
echo   本脚本要在 ADB-Tool-Studio 根目录下跑，当前是：%CD%
echo.
pause
exit /b 1

:nopython
echo.
echo   [错误] 没找到 Python。
echo.
echo   如果敲 python 弹的是微软商店，那是商店的占位符，不是真的 Python。
echo   去 python.org 装一个 3.8 以上版本，安装时勾上 Add python.exe to PATH。
echo   注意：网页版 server.py 本身零依赖，只有打包 exe 才需要 Python。
echo.
pause
exit /b 1

:pipfail
echo.
echo   [错误] pip 装依赖失败。
echo.
echo   多半是网络不通，或者公司网络要走代理。国内可以换镜像重试：
echo     .venv-build\Scripts\python.exe -m pip install -r requirements-desktop.txt pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:locked
echo.
echo   [错误] dist\ADB-Tool-Studio.exe 删不掉，有东西占着它。
echo.
echo   最常见的就是它自己还在跑：双击开的那个 exe 缩在托盘里没退。
echo   Windows 不允许删掉正在运行的 exe，PyInstaller 最后一步就会报
echo   PermissionError [WinError 5] 拒绝访问。
echo.
echo   解决：
echo     1. 看屏幕右下角的托盘图标，右键 - 退出
echo     2. 还不行就 Ctrl+Shift+Esc 打开任务管理器，结束 ADB-Tool-Studio.exe
echo     3. 都不行就重启一次，或者把项目拷到本地盘再打
echo.
echo   然后重跑本脚本就行 —— PyInstaller 的分析缓存还在，很快。
echo.
pause
exit /b 1

:fail
echo.
echo   [失败] 上一步出错了，往上翻看具体报错。
echo.
pause
exit /b 1
