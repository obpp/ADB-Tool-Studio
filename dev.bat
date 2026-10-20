@echo off
chcp 936 >nul
REM ===========================================================================
REM  开发用：源码模式跑桌面版。改完代码重跑本文件就行，**不用打包**。
REM
REM    dev.bat          有控制台，日志和报错直接打在屏幕上（日常用这个）
REM    dev.bat pyw      用 pythonw 跑，没有控制台 —— 复现「闪黑框」这类问题
REM    dev.bat web      只跑网页版，不套窗口，用浏览器看
REM    dev.bat popup    专门测「子进程会不会弹黑框」，十几秒看完
REM
REM  第一次跑会自动建 .venv-build 并装桌面依赖（约一分钟），之后就是秒开。
REM  本文件必须保持 GBK 编码 + CRLF 换行，原因见 build.bat 顶部的说明。
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist "desktop\app.py" goto :wrongdir

echo [准备] 找 Python ...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

if not exist ".venv-build\Scripts\python.exe" (
    echo [准备] 建虚拟环境 .venv-build（第一次要等一会儿）...
    %PY% -m venv .venv-build
    if errorlevel 1 goto :fail
)
set "VPY=.venv-build\Scripts\python.exe"
set "VPYW=.venv-build\Scripts\pythonw.exe"

"%VPY%" -c "import webview, pystray" >nul 2>nul
if errorlevel 1 (
    echo [准备] 装桌面依赖 ...
    "%VPY%" -m pip install -r requirements-desktop.txt
    if errorlevel 1 goto :fail
)

if /i "%~1"=="web"   goto :web
if /i "%~1"=="pyw"   goto :pyw
if /i "%~1"=="popup" goto :popup

echo.
echo [运行] 源码模式 + 控制台。关窗口只是缩托盘，要退出走托盘菜单或 Ctrl+C。
echo.
"%VPY%" desktop\app.py --force
goto :done

:pyw
if not exist "%VPYW%" goto :nopyw
echo.
echo [运行] 用 pythonw 启动（没有控制台）。日志在 logs\app.log
echo        想复现「闪黑框」，就去页面上点启动，然后盯住屏幕。
echo.
start "" "%VPYW%" desktop\app.py --force
goto :done

:web
echo.
echo [运行] 只跑网页版，用浏览器打开它打印出来的地址。
echo.
"%VPY%" server.py
goto :done

:popup
if not exist "%VPYW%" goto :nopyw
echo.
echo [检查] 连起 20+20 个子进程，盯住屏幕看黑框闪不闪，十几秒。
echo.
"%VPYW%" desktop\devcheck.py
goto :done

:wrongdir
echo.
echo   [错误] 当前目录不对，没找到 desktop\app.py。
echo.
pause
exit /b 1

:nopython
echo.
echo   [错误] 没找到 Python。
echo.
echo   如果敲 python 弹的是微软商店，那是商店的占位符，不是真的 Python。
echo   去 python.org 装一个 3.8 以上版本，安装时勾上 Add python.exe to PATH。
echo.
pause
exit /b 1

:nopyw
echo.
echo   [错误] 虚拟环境里没有 pythonw.exe。删掉 .venv-build 重跑一次。
echo.
pause
exit /b 1

:fail
echo.
echo   [失败] 上一步出错了，往上翻看具体报错。
echo.
pause
exit /b 1

:done
echo.
pause
