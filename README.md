# my-adb-desktop

`my-adb-app` 的桌面版：同一个后端，外面套一层 Windows 原生窗口 + 托盘，可以打包成 exe。

功能完全一样（always-adb、adb 反向隧道），**网页版那一套原样保留** ——
`python3 server.py` 照样能跑，两边不会打架。

> 这份是从 `../my-adb-app` 复制出来的独立副本，原目录没被动过。
> 以后改了 `tools/` 或 `static/` 的公共逻辑，记得两边同步。

## 桌面版多了什么

| | 网页版 | 桌面版 |
|---|---|---|
| 怎么开 | 敲命令，自己开浏览器、记端口 | 双击 exe |
| 端口 | 固定 8765，撞了就起不来 | 系统随机分配，永不冲突 |
| 关掉之后 | 服务跟着没 | 缩进托盘继续跑 |
| 开机自启 | 得自己写计划任务 | 托盘里勾一下 |
| 同时开两个 | 可以，各跑各的 | 拦住，只留一个 |

## 用法

### 源码方式跑（调试用）

```bash
pip install -r requirements-desktop.txt
python3 desktop/app.py                 # 开窗口
python3 desktop/app.py --minimized     # 直接进托盘，不弹窗
```

网页版照旧：

```bash
python3 server.py                      # 默认 http://127.0.0.1:8765/
python3 server.py --port 9000 --host 0.0.0.0
```

### 打包成 exe

在 Windows 上双击 `build.bat`，或者在命令行里跑：

```bat
build.bat
```

它会建一个独立的虚拟环境 `.venv-build`（**别在全局环境里打** ——
PyInstaller 会把当前环境扫得到的包全塞进 exe，打出来又大又杂），
装依赖、生成图标、打包。产物在 `dist\my-adb-app.exe`。

### 打包的三种方式

```bat
build.bat           普通打包，单文件 exe
build.bat dir       出文件夹而不是单文件
build.bat clean     清掉 PyInstaller 缓存重打
```

**开发期间用 `build.bat dir`**：单文件每次都要把整个包重新压一遍，改一行代码也得等很久；
文件夹模式只重写变了的那些文件，快得多，启动也快、杀软误报也少。对外发的时候再换回单文件。

**`clean` 平时不要加** —— 它会清空 PyInstaller 的缓存，每次都是全量重打。
只有改了 `build.spec`、或者怀疑缓存脏了才需要。
（缓存放在 `build\build\`。分析和 PYZ 都在里面，所以第二次打同一份代码基本是秒过。）

### 打包前会先关掉正在跑的实例

`build.bat` 的第 5 步会自动 `taskkill` 掉正在跑的 `my-adb-app.exe`，然后把
`dist\my-adb-app.exe` 删一次试试还占不占得住。看到这两行是正常的：

```
[5/6] 关掉正在跑的实例 ...
     发现 my-adb-app.exe 还在跑，已经关掉它。
```

**为什么要这么干**：Windows 不允许删掉正在运行的 exe。双击开着的那个程序缩在托盘里
没退的话，PyInstaller 会在**最后一步**（分析全跑完之后）才撞上这件事，报：

```
PermissionError: [WinError 5] 拒绝访问。: '...\dist\my-adb-app.exe'
```

那不是代码有问题，就是文件被自己占着。关掉正在跑的那个实例就好，
而且重跑很快 —— 分析结果都缓存着，只有最后的组装那一步重来。

真关不掉（比如被别的用户启动着、或者杀软咬着不放）脚本会提前停下来说清楚，
不会让你白等一场。手动去任务管理器结束 `my-adb-app.exe` 再跑一遍就行。

## 开发时怎么快速验证（别打包一次测一次）

改完代码，**绝大多数东西根本不用打包**，源码模式就能测：

```bat
dev.bat             源码模式 + 控制台，日志和报错直接打在屏幕上（日常用这个）
dev.bat pyw         用 pythonw 跑，没有控制台
dev.bat web         只跑网页版，用浏览器看
dev.bat popup       专门测「子进程会不会弹黑框」，十几秒出结论
```

第一次跑会自动建 `.venv-build` 并装桌面依赖（约一分钟），之后就是秒开。

什么时候**才必须**真打包：

| 要验证的 | 源码模式够吗 |
|---|---|
| 界面、HTML/CSS/JS、工具逻辑（adb/ssh） | ✅ 够 |
| 托盘、开机自启、单实例、关窗缩托盘 | ✅ 够 |
| 「没有控制台」时的行为（闪黑框就是这一类） | ⚠️ 得用 `dev.bat pyw` 才复现得出来 |
| `sys._MEIPASS` 路径、依赖有没有收全 | ❌ 必须打包 |
| 杀软误报、启动速度 | ❌ 必须打包 |

最后两类只在改了 `apppaths.py`、`build.spec`、或者新增第三方依赖时才要复查。

`dev.bat` 默认带 `--force` 跳过单实例检查 —— 不然托盘里还挂着一个旧实例时，
新起的那个会直接退出（这是设计如此，但开发时很烦）。

## 目录结构

```
my-adb-desktop/
├── desktop/                  ← 桌面外壳（网页版不需要这一层）
│   ├── app.py                入口：内嵌服务 + 窗口 + 托盘
│   ├── tray.py               托盘图标与菜单
│   ├── autostart.py          开机自启（HKCU 的 Run 键）
│   ├── singleinstance.py     只允许跑一个
│   ├── devcheck.py           开发用：检查子进程会不会弹黑框
│   └── make_icon.py          用标准库生成 icon.ico（改了图标要重跑一次）
├── apppaths.py               ← 路径解析：源码模式 / 打包后两种
├── spawn.py                  ← 起子进程时别让 Windows 弹黑框
├── server.py                 后端；加了 AppServer 供桌面版内嵌
├── toolbase.py  adbkit.py  tools/  static/     ← 和网页版一致
├── icon.ico
├── dev.bat                   开发回路（源码模式，秒级）
├── build.spec  build.bat  requirements-desktop.txt   打包用
```

> `dev.bat` 和 `build.bat` **必须保持 GBK 编码 + CRLF 换行**。cmd 是按系统 ANSI
> 代码页逐字节读批处理的，存成 UTF-8 的话中文注释会把解析器带跑偏 ——
> 症状是 `'EM' 不是内部或外部命令` 这种莫名其妙的报错。
> `requirements-desktop.txt` 同理要保持 ASCII：pip 在中文 Windows 上按 cp936
> 解码它，UTF-8 注释可能直接抛 `UnicodeDecodeError`。

## 打包时最容易踩的三个坑

这个 spec 里已经处理好了，但值得知道为什么：

1. **`webview/js/*.js` 和 `webview/lib/*.dll` 是包数据**，不在 `.py` 里，
   PyInstaller 的静态分析看不见，得 `collect_data_files("webview")` 收进来。
   漏了 js 的表现是「窗口能开、内容全白」。
2. **pythonnet 和 `webview.platforms.*` 是运行时才 import 的**，
   静态分析同样扫不到，得写进 `hiddenimports`。Windows 上 pywebview 靠 pythonnet
   跟 WinForms/WebView2 说话。
3. **`static/` 和配置的落点**：只读资源和可写数据必须分开（见 `apppaths.py`）。
   单文件模式下 `sys._MEIPASS` 是个临时目录，**退出就删** ——
   配置写那儿会无声无息地丢。

### 文件放在哪

| | 只读资源（static/、icon.ico） | 可写数据（config/、logs/） |
|---|---|---|
| 源码模式 | 项目目录 | 项目目录 |
| 打包后 | 包里的 `sys._MEIPASS` | **exe 旁边**；写不进去（比如装在 Program Files）才退到 `%APPDATA%\my-adb-app\` |

日志在 `<可写数据目录>\logs\app.log`。桌面版没有控制台，**启动失败第一件事就是看它**，
日志超过 512KB 会轮转成 `app.log.1`。

## 两个必须知道的前提

- **WebView2 运行时**：pywebview 在 Windows 上靠 Edge 的 WebView2，Win11 和装了 Edge 的 Win10 都自带。
  pywebview 已经把 SDK 和 Loader DLL 一起打包了，缺的只是运行时。
  程序启动时会查一眼注册表，查不到就直接用系统浏览器打开兜底，不会白屏。
  实在没有的话装一下：[WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)。
- **杀软误报**：PyInstaller 打出来的 exe 被拦是常事（尤其 onefile）。
  已经关掉 UPX 压缩降低概率，但还是可能需要加白名单，或者换成 onedir 模式。

## 托盘的用法

- **双击** 打开窗口；**右键** 出菜单
- 关窗口（点 ×）**不退出**，只是收进托盘；要真退出走托盘的「退出」
- 「全部停止」= 把两个工具的后台任务都停掉（比如掐掉 ssh 隧道）
- 「开机自启」勾上后写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，
  只要用户权限、不弹 UAC。想取消就再点一下，或者去任务管理器的「启动」页关

## 现有功能

### 1. always-adb（`/always-adb`）

`_always_adb.bat` 的 Web 版：设置 `force_adb` → 成功了就 `root` + `sync` → 等一会儿 → 再来一轮；设置失败就按固定间隔重试。

| 原 bat | 本程序 |
|---|---|
| `:MAIN_LOOP` | 后端循环线程 |
| `:RETRY_FORCE_ADB` | 失败重试的内层循环 |
| `timeout /t 1` / `/t 10` | `retryInterval` / `waitSeconds`，页面可改 |
| `adb.exe root` / `shell "sync"` | `doRoot` / `doSync` 开关 |
| `Ctrl+C` 停不干净 | 「停止」会打断正在进行的等待，**0.5 秒内退出** |

比 bat 多做的几件事：

- **失败判定更严**：bat 只看 `errorlevel`。有些 adb 版本在 `settings put` 被系统拒绝时仍返回 0，只在输出里留一句 `SecurityException`；这里连输出一起看。
- **`root` 之后等设备回来**：`adb root` 会重启 adbd，紧接着的 `sync` 常常打到 "device not found"。`waitAfterRoot`（默认开）会先 `adb wait-for-device`。
- **root 假成功会被点破**：production build 上 `adb root` 返回 0 但打印 `adbd cannot run as root`，这里记一条警告，不假装成功。
- **多设备**：多于一台时必须在页面选定序列号，否则 adb 会报错。

### 2. adb 反向隧道（`/ssh-tunnel`）

把本机 adb server 反向映射到远程主机，等价于：

```bash
ssh -R 5037:127.0.0.1:5037 -N zhangshijin2@10.27.12.198
```

建好之后，**在远程主机上直接敲 `adb devices`，就能看到接在本机上的设备**（远程 adb 客户端默认就连 `127.0.0.1:5037`）。

页面可调：

| 参数 | 说明 |
|---|---|
| 远程主机 / SSH 用户名 / SSH 端口 | `user@host` 和 `-p` |
| 远程绑定端口 | `-R` 的远程那一头，默认 `5037` |
| 本地地址 / 本地端口 | `-R` 转发到本机的什么地址，默认 `127.0.0.1:5037` |
| 私钥文件 | `-i` + `IdentitiesOnly=yes`；留空用 ssh 默认密钥 |
| 主机指纹校验 | `accept-new` / 严格 / 不校验，对应 `StrictHostKeyChecking` |
| 断线自动重连 + 间隔 | ssh 退出后自动重新拉起 |
| 发送心跳保活 | `ServerAliveInterval=15` + `ServerAliveCountMax=3` |
| 禁止交互式提问 | `BatchMode=yes`，没有终端时快速失败而不是傻等 |
| 绑定到 0.0.0.0 | `-R 0.0.0.0:port:...`，**需要远程 sshd 开 `GatewayPorts`** |

页面顶部实时显示**这条命令的实际内容**，改了参数会跟着变，带「复制」按钮 —— 想手动敲一遍或者写进脚本都可以。

ssh 的常见报错会被翻译成一句人话（认证被拒、指纹校验失败、端口占用、网络不通……），每条只提示一次。

**前提**：本机得有个活着的 adb server 在监听 `5037`（`adb start-server`）。隧道本身只是转发的管道，本地没有监听者的话，远程连过来会被拒。

## 加新功能

1. 在 `tools/` 下写一个 `Tool` 子类，实现 `default_config()` / `sanitize_config()` / `run_loop()`；
   想要卡片摘要就覆盖 `brief()`，想要额外状态就覆盖 `extra()`。
2. 在 `tools/__init__.py` 的 `TOOL_CLASSES` 里加一行。
3. 在 `static/` 下加一个页面，用 `common.js` 里的 `ToolClient` / `LogConsole` / `paintBadge`。

路由、SSE、启停、配置持久化都是基类给的，不用重复写。
**桌面版不用改** —— 它只是把同一份 registry 和静态页面套进窗口而已。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 一级页面 |
| GET | `/<page>` | 二级页面，如 `/always-adb`、`/ssh-tunnel` |
| GET | `/api/tools` | 所有工具的状态摘要（一级页面用） |
| GET | `/api/<id>/state` | 单个工具的全量状态 + 最近 500 条日志 |
| GET | `/api/<id>/events` | SSE 流，事件：`snapshot` / `state` / `log` |
| GET | `/api/<id>/devices` | 刷新并返回设备列表（always-adb） |
| POST | `/api/<id>/start` | 启动，body 可带选项（always-adb 收 `{"once": true}`） |
| POST | `/api/<id>/stop` | 停止，会打断正在进行的等待并杀掉子进程 |
| POST | `/api/<id>/config` | 更新配置，按补丁合并，字段范围夹紧后落盘 |
| POST | `/api/<id>/preview` | 只返回命令行预览，不执行（隧道） |
| POST | `/api/<id>/logs/clear` | 清空服务端日志历史 |

## 注意

- 服务本身**没有鉴权**。桌面版默认只监听 `127.0.0.1`，不对外。
  真给 `--host 0.0.0.0` 暴露到局域网时，同网段任何人都能通过这个页面跑 `adb root`、
  或者在你的机器上开 ssh 隧道。
- 页面上的设备型号、adb 报错原文、ssh 输出都按**纯文本**渲染，不拼 HTML（这些是不可信数据）。
- 状态颜色只做辅助：每种状态都同时带图标和文字，色觉障碍或黑白打印下同样可读。
- 反向隧道是把「本机的 adb 控制权」交给远程主机 —— 只在可信的远程主机上开。
