# ADB Tool Studio

`my-adb-app` 的桌面版：同一个后端，外面套一层 Windows 原生窗口 + 托盘，可以打包成 exe。

功能完全一样（always-adb、adb 反向隧道、脚本执行、替换镜像），**网页版那一套原样保留** ——
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
装依赖、生成图标、打包。产物在 `dist\ADB-Tool-Studio.exe`。

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

`build.bat` 的第 5 步会自动 `taskkill` 掉正在跑的 `ADB-Tool-Studio.exe`，然后把
`dist\ADB-Tool-Studio.exe` 删一次试试还占不占得住。看到这两行是正常的：

```
[5/6] 关掉正在跑的实例 ...
     发现 ADB-Tool-Studio.exe 还在跑，已经关掉它。
```

**为什么要这么干**：Windows 不允许删掉正在运行的 exe。双击开着的那个程序缩在托盘里
没退的话，PyInstaller 会在**最后一步**（分析全跑完之后）才撞上这件事，报：

```
PermissionError: [WinError 5] 拒绝访问。: '...\dist\ADB-Tool-Studio.exe'
```

那不是代码有问题，就是文件被自己占着。关掉正在跑的那个实例就好，
而且重跑很快 —— 分析结果都缓存着，只有最后的组装那一步重来。

真关不掉（比如被别的用户启动着、或者杀软咬着不放）脚本会提前停下来说清楚，
不会让你白等一场。手动去任务管理器结束 `ADB-Tool-Studio.exe` 再跑一遍就行。

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
ADB-Tool-Studio/
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
| 打包后 | 包里的 `sys._MEIPASS` | **exe 旁边**；写不进去（比如装在 Program Files）才退到 `%APPDATA%\ADB-Tool-Studio\` |

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
- 「全部停止」= 把所有工具的后台任务都停掉（比如掐掉 ssh 隧道、中断一次烧写）
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
| 远程绑定端口 | `-R` 的远程那一头，默认 `5037`；**改过之后服务端 adb 要设 `ANDROID_ADB_SERVER_PORT`** |
| 本地地址 / 本地端口 | `-R` 转发到本机的什么地址，默认 `127.0.0.1:5037` |
| 私钥文件 | `-i` + `IdentitiesOnly=yes`；留空用 ssh 默认密钥 |
| 主机指纹校验 | `accept-new` / 严格 / 不校验，对应 `StrictHostKeyChecking` |
| 断线自动重连 + 间隔 | ssh 退出后自动重新拉起 |
| 发送心跳保活 | `ServerAliveInterval=15` + `ServerAliveCountMax=3` |
| 禁止交互式提问 | `BatchMode=yes`，没有终端时快速失败而不是傻等 |
| 绑定到 0.0.0.0 | `-R 0.0.0.0:port:...`，**需要远程 sshd 开 `GatewayPorts`** |

页面顶部实时显示**这条命令的实际内容**，改了参数会跟着变，带「复制」按钮 —— 想手动敲一遍或者写进脚本都可以。

ssh 的常见报错会被翻译成一句人话（认证被拒、指纹校验失败、端口占用、网络不通……），每条只提示一次。

**远程端口被占用时**（`Warning: remote port forwarding failed for listen port ...`）：

- 页面左上角的状态变成「远程端口 5037 被占用」，「远程绑定端口」下面的提示标红，
  并直接给出一个建议值（当前端口 +1）
- 日志里写清楚：改哪个输入框、改成多少、服务端要跟着执行什么
- 如果是勾了「绑定到 0.0.0.0」才失败，还会提醒检查远程 sshd 的 `GatewayPorts`

**换端口之后**，服务端的 adb 客户端默认还是连 `5037`，必须让它改指隧道端口：

```bash
export ANDROID_ADB_SERVER_PORT=30717
```

这条提示在两个地方出现：远程端口不是 `5037` 时，页面在端口输入框下面常驻显示；
隧道建立后，日志里也会再写一遍（认证已过、进程活着），并且建议写进 `~/.bashrc` 持久化。

此外，第一次打开这个页面会弹窗提醒：

- 服务器的远端绑定端口如果被占用，需要更换端口，并在服务器端 `~/.bashrc`
  中添加 `export ANDROID_ADB_SERVER_PORT=<远程绑定端口>`
- 弹窗里的命令可以一键复制；换端口保存后，命令里的值会跟着更新
- 勾选「不再提示」再点「知道了」，以后进这个页面就不自动弹了
- 但只要改过参数并保存（点「建立隧道」时带着改过的参数也算），就会再弹一次 ——
  端口可能已经变了，命令要跟着换

> 「隧道已建立」这个判断是**启发式**的：`ssh -N` 成功时什么都不输出，没有可靠的信号。
> 程序的做法是——ssh 进程活过约 3 秒还没退出，就认为连上了（绑定失败、认证失败
> 因为 `ExitOnForwardFailure=yes` + `BatchMode=yes` 会在一秒内退出）。
> 判断早了也只是多一条提示，不会影响隧道本身。

**前提**：本机得有个活着的 adb server 在监听 `5037`（`adb start-server`）。隧道本身只是转发的管道，本地没有监听者的话，远程连过来会被拒。

### 3. 脚本执行（`/scripts`）

把本地脚本推到设备上，用 `adb shell sh` 跑，输出实时回到日志。等价于：

```bash
adb -s <序列号> push check.sh /data/local/tmp/ats-0d30815d-check.sh
adb -s <序列号> shell sh /data/local/tmp/ats-0d30815d-check.sh
```

**注意脚本是在设备上执行的**，不是在本机跑。设备上要有 `sh`（Android 都有）。

| 参数 | 说明 |
|---|---|
| adb 路径 | 默认用跟程序放在一起的 adb，其次 PATH |
| 目标设备 | 不选时只有「恰好一台在线」才自动选；多台必须自己选 |
| 远程目录 | 默认 `/data/local/tmp`，必须是 `/` 开头的绝对路径 |
| 超时 | 单次执行上限，默认 60 秒 |
| 脚本参数 | 跟着选中的脚本保存；空格分隔，支持 `"..."` 和 `'...'`，执行时接在设备上的 `sh` 命令后面 |

脚本库：

- 「导入脚本…」打开文件浏览，也可以直接把文件拖到脚本库下方的任意位置（整个区域都是投放区）；**只导入，没有页内编辑器**
- 导入时去 UTF-8 BOM、把 CRLF 换成 LF —— 不换的话 Windows 上写的 `.sh` 到板子上必报 `: not found`
- 导入后的名字可以就地改，删除要点两次（第二次才算数，防手滑）
- **每次执行都重新推一份**，不做「设备上已有就跳过」：列表里显示的是哪份，跑的就一定是哪份
- 设备上的文件名带脚本 id 前缀（`ats-0d30815d-check.sh`），两个同名脚本不会互相覆盖
- 一次只跑一个，跑的时候不能删、不能再启动第二个
- **参数跟着脚本存**：切回这个脚本时输入框里还是上次那串，失焦和执行前各存一次。
  带空格的参数要加引号（`--name "hello world"`）—— 后端会按设备上 `sh` 的规则再转义一层，
  不会到设备那头被拆成两个；引号没闭合直接拒绝执行，不会推到板子上白跑一趟
- 「停止」只是断开 adb 连接，**设备上正在跑的命令不一定跟着退出**

### 4. 替换镜像（`/replace-image`）

把一个或多个本地镜像推到设备上，`dd` 写进对应的分区，全部成功后重启。等价于：

```bash
adb -s <序列号> push ~/rk3576_r8/kernel-6.1/boot.img /mnt/ats-47fb54bd-1-boot.img
adb -s <序列号> shell "dd if=/mnt/ats-47fb54bd-1-boot.img of=/dev/block/by-name/boot_a bs=1048576"
adb -s <序列号> shell "dd if=/mnt/ats-47fb54bd-1-boot.img of=/dev/block/by-name/boot_b bs=1048576"
adb -s <序列号> push ~/rk3576_r8/kernel-6.1/dtbo.img /mnt/ats-47fb54bd-2-dtbo.img
adb -s <序列号> shell "dd if=/mnt/ats-47fb54bd-2-dtbo.img of=/dev/block/by-name/dtbo_a bs=1048576"
adb -s <序列号> shell "dd if=/mnt/ats-47fb54bd-2-dtbo.img of=/dev/block/by-name/dtbo_b bs=1048576"
adb -s <序列号> shell "reboot"
```

**这个功能会直接写块设备，写错了设备就起不来。** 页面上的流程是：

1. 选一个预设（镜像与目标列表 + 推送目录 + 是否重启）
2. 点「执行选中预设」→ 确认条会逐条列出「哪个镜像写到哪个分区」
3. 点「确认烧写」才真的开始

预设：

- 一个预设可以有**多行「镜像 → 目标」**：`boot.img` 写 `boot_a`、`boot_b`，`dtbo.img` 写 `dtbo_a`、`dtbo_b` 都行；
  同一个镜像推到设备上一次，再分别 `dd` 到它的每个目标
- 目标可以写完整路径，也可以只写分区名（`boot_a boot_b`），只写名字时自动补成
  `/dev/block/by-name/`；一行里多个目标用空格或逗号分隔
- 同一个分区不能同时分给两个镜像，保存时就会拦下来
- 老的单镜像预设会在读取时自动折成一行，不用重新建
- 镜像**不复制进程序目录**，只记绝对路径 —— boot.img 动辄几十上百 MB，复制一份既费磁盘，改了源码目录里的文件还得再导一次
- 名称只是列表里的标签，不影响烧写；留空就用第一个镜像的文件名（`boot.img` → `boot`）
- 每行的「浏览…」在桌面版里直接弹系统的选文件对话框（和「导入脚本」一样），选完只把完整路径填回来；
  默认只看 `.img` / `.bin` / `.raw` / `.iso`。用浏览器跑网页版时浏览器不会把完整路径交给页面，退回服务端的目录浏览器
- 路径输入框可以直接粘贴；网页版的目录浏览器里可以回车跳转、换 Windows 盘符
- 推送目录默认 `/mnt`

执行时依次过这几道闸，**任何一道不过都不写分区，更不会重启**：

| 步骤 | 做什么 | 不过关时 |
|---|---|---|
| 参数校验 | 所有镜像存在且非空；目标是 `/dev/` 下的路径，禁止 `..` 和 shell 元字符；同一分区不能分给两个镜像 | 直接放弃 |
| 预检 `test -b` | **先把所有目标验一遍**，确认都是块设备（符号链接会跟过去） | 直接放弃 |
| 预检 `blockdev` | 每个镜像都不大于对应分区（设备没有 `blockdev` 时只警告） | 直接放弃 |
| `push` | 每个镜像推到 `<推送目录>/ats-<预设 id>-<序号>-<文件名>`，只推一次 | 直接放弃 |
| `wc -c` | 核对设备上的字节数和本地一致 | 直接放弃 |
| `dd` | 逐个目标写分区；返回码非 0，或输出里有 `No space left` / `I/O error` / `Permission denied` 也算失败 | 停止，不重启 |
| `sync` + `reboot` | 所有 dd 都成功后才执行 | — |

先验全再写，是为了 A/B 分区：写了一半才发现 `boot_b` 不存在，比一个都没写更麻烦。

几个要知道的点：

- 参数里的「单步超时」默认 600 秒，每次 push / dd 各算各的；大镜像推得慢就把这个调大（最大 7200 秒）
- 写 `/dev/block/by-name/...` 通常需要 adbd 已经是 root；没有权限时 dd 会失败，程序会中止并且**不重启**
- 中途失败或点「停止」时，**已经写进去的分区不会回滚**，日志里会写清楚「已写入 N/M 个分区」；
  这时设备上的 A/B 两个槽可能对不上，需要重新完整烧一次
- 「停止」在 dd 中途生效时，当前分区可能只写了一半，程序不会自动重启
- 推上去的镜像烧完不删，重启后还在推送目录里（`/mnt` 是 tmpfs 的话重启自然消失）
- 网页版退回的目录浏览器能列本机任意目录，这是「只记录路径」的代价；服务默认只监听 `127.0.0.1`，用 `--host 0.0.0.0` 时请记住整站本来就没有鉴权
- 桌面版的选文件对话框走的是 pywebview 的 `js_api`（`PageApi.pick_image`），只有 exe / `dev.bat` 里才有；浏览器里没有这座桥

## 侧边栏底部的两个状态

侧边栏最下面有两行独立的状态，颜色和文字同时表意：

- **服务**：页面与本地后端的 HTTP 连接。每 3 秒请求一次 `/api/tools`，失败自动重试；
  后面跟的「推送正常 / 推送重连中」是当前页 SSE 长连接的状态。
  它只表示页面和后端之间通不通，**不代表 adb 设备或 adb server 的状态**。
- **设备**：当前页面工具看到的 adb 设备。后端常驻 `adb wait-for-device` /
  `wait-for-disconnect`，设备上线、掉线会立即推送，不依赖轮询。
  在线时绿点并显示序列号；待授权/离线时黄点并说明原因；没有设备是灰点。
  ssh 隧道这类不涉及 adb 的页面不显示这一行。它**不代表 Web 后端连接状态**。

窄窗口（≤1000px）侧边栏收成图标条，只保留圆点，完整说明放在悬浮提示（`title`）里。

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
| GET | `/<page>` | 二级页面，如 `/always-adb`、`/ssh-tunnel`、`/scripts`、`/replace-image` |
| GET | `/api/tools` | 所有工具的状态摘要（一级页面用） |
| GET | `/api/<id>/state` | 单个工具的全量状态 + 最近 500 条日志 |
| GET | `/api/<id>/events` | SSE 流，事件：`snapshot` / `state` / `log` |
| GET | `/api/<id>/devices` | 刷新并返回设备列表（always-adb、脚本执行、替换镜像） |
| POST | `/api/<id>/start` | 启动，body 可带选项（always-adb `once`、脚本执行 `scriptId`、替换镜像 `presetId`） |
| POST | `/api/<id>/stop` | 停止，会打断正在进行的等待并杀掉子进程 |
| POST | `/api/<id>/config` | 更新配置，按补丁合并，字段范围夹紧后落盘 |
| POST | `/api/<id>/preview` | 只返回命令行预览，不执行（隧道） |
| POST | `/api/<id>/logs/clear` | 清空服务端日志历史 |
| POST | `/api/script_runner/scripts/import?name=` | 导入脚本，body 是原始字节（上限 2 MB） |
| POST | `/api/script_runner/scripts/rename` | 脚本改名 |
| POST | `/api/script_runner/scripts/delete` | 删除脚本（执行中返回 409） |
| POST | `/api/script_runner/scripts/args` | 保存某个脚本的参数（`id` + `args`） |
| POST | `/api/replace_image/presets/save` | 新建或更新镜像预设 |
| POST | `/api/replace_image/presets/delete` | 删除预设（烧写中返回 409） |
| POST | `/api/replace_image/fs/list` | 列本机目录，供网页版的镜像浏览器用（`path` + `all`） |
| POST | `/api/replace_image/fs/stat` | 查一个本地文件是否存在、多大 |

工具自己的动作走 `Tool.api()` 这个通用出口（`server.py` 里不用为每个功能加分支），
返回 `None` 表示不认识这个 action，最后统一落到 404。

设备列表不靠前端轮询来发现上线：页面第一次请求 `/api/<id>/devices` 后，后端会为这个工具
常驻一条 `adb wait-for-device`（当前无设备）或 `adb -s <序列号> wait-for-disconnect`（已有设备）。
设备上线/掉线时重新查一次列表，通过 SSE 的 `state` 事件推给页面；空闲时没有轮询开销。
配置里的 adb 路径或序列号一变，旧的等待会被杀掉、按新配置重建；程序退出时一并清理。

## 注意

- 服务本身**没有鉴权**。桌面版默认只监听 `127.0.0.1`，不对外。
  真给 `--host 0.0.0.0` 暴露到局域网时，同网段任何人都能通过这个页面跑 `adb root`、
  或者在你的机器上开 ssh 隧道。
- 页面上的设备型号、adb 报错原文、ssh 输出都按**纯文本**渲染，不拼 HTML（这些是不可信数据）。
- 状态颜色只做辅助：每种状态都同时带图标和文字，色觉障碍或黑白打印下同样可读。
- 反向隧道是把「本机的 adb 控制权」交给远程主机 —— 只在可信的远程主机上开。
