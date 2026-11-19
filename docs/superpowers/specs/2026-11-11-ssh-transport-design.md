# 全局 SSH 连接方式设计

日期：2026-11-11　状态：已确认

## 1. 要做什么

现在四个功能（always-adb 除外）都直接调 adb。开发板同时可以通过 USB 网卡用 SSH 访问（`192.168.100.1`，root 登录）。希望在一个地方切换连接方式，切到 SSH 之后：

- 脚本执行走 SSH
- 替换镜像走 SSH
- 推送文件走 SSH
- 反向隧道改成把开发板的 SSH 转发到远端服务器

切回 ADB 时，行为与现在完全一致。成功标准：四个功能在真实开发板上都能用；ADB 模式没有回归。

## 2. 已经确认的决定

- **全局唯一开关**：一个地方切一次，所有功能跟着走。
- **不并行**：SSH 模式下不使用 adb，也不做「ADB 失败自动回退 SSH」这类逻辑。
- **不用 `~/.ssh/config` 别名**：软件里单独填地址、端口、用户名、密钥文件。
- **只支持密钥 / ssh-agent**：不实现密码输入，SSH 一律带 `BatchMode=yes`，连不上就快速失败并给出提示。
- **开发板 SSH 用户是 root**：替换镜像、写 `/dev/block/…` 不需要 sudo。
- **反向隧道方向**：软件所在的电脑主动连远端服务器（`10.27.12.198`），把开发板的 22 转发到远端端口。远端只在 loopback 上访问（那边会配 `Host dev → 127.0.0.1:2222`），所以默认不绑 `0.0.0.0`，默认关闭 GatewayPorts。
- **实现只用系统 OpenSSH + Python 标准库**：沿用 `ssh_tunnel.py` 已经在用的方式，不引入 paramiko 等依赖。

## 3. 非目标

- 多套 SSH 配置或多块板子同时管理（只有一个连接档案）。
- 密码认证、自动生成/分发密钥、管理 ssh-agent。
- ADB 与 SSH 同时在线、按功能各走各的。
- 通过 SSH 执行 adb 命令（不做 adb over SSH）。
- 远端服务器的 sshd / GatewayPorts / 账号配置（用户自己负责）。

## 4. 总体结构

```
            三个功能页 + 隧道页
                    │
            transport.make(tool)
                    │
        ┌───────────┴───────────┐
        │                       │
   AdbTransport             SshTransport
   adbkit + subprocess      系统 ssh + 流式传文件
```

新增三个模块，改四个现有文件：

| 文件 | 作用 |
|------|------|
| `connection.py`（新） | 当前连接模式与 SSH 参数，读写 `<数据目录>/config/connection.json` |
| `transport.py`（新） | 统一传输接口，ADB / SSH 两个实现，按当前模式创建 |
| `sshkit.py`（新） | 找 ssh、拼公共参数、错误提示；从 `ssh_tunnel.py` 抽出复用 |
| `tools/*.py` | 把直接调 `adbkit` / `subprocess.Popen` 的地方换成 transport |
| `devicewatch.py` | 增加 SSH 探测监听 |
| `server.py` | 连接接口、模式切换、`/api/tools` 增加连接信息 |
| `static/*` | 设置页连接卡片、侧边栏连接行、各页按模式隐藏 adb 字段 |

三个工具的业务逻辑（校验、dd 流程、预设、日志）保持不动，只换「怎么连设备」这一层。

## 5. 连接配置

文件：`config/connection.json`，跟着数据目录走。

```json
{
  "mode": "adb",
  "ssh": {
    "host": "",
    "port": 22,
    "user": "",
    "keyFile": "",
    "hostKeyPolicy": "accept-new",
    "connectTimeout": 10,
    "remountCommand": "mount -o remount,rw /"
  }
}
```

规则：

- `mode` 只允许 `adb` / `ssh`，缺省 `adb`。
- `host`、`user` 不预填：不同的板子地址不同，页面留占位提示（`192.168.100.1`、`root`）。SSH 模式下两者都必填，留空直接 400，不交给 OpenSSH 去猜本地用户名。
- 端口范围 1–65535；`keyFile` 可空，空时走 ssh-agent 和默认密钥。
- `hostKeyPolicy` 沿用隧道的三档：`accept-new`（默认）/ `yes` / `no`。
- `remountCommand` 是 SSH 模式下「先 remount」实际执行的命令，放在高级项里。
- 只保存密钥路径，不保存任何密钥内容。
- 切换数据目录后重新加载本文件；切换数据目录已经要求没有任务在跑，规则一致。

## 6. 传输层接口

`transport.make(tool)` 按 `connection.get()["mode"]` 返回实现。接口：

| 方法 | 说明 |
|------|------|
| `preview(args)` | 日志里显示的命令行（ADB 是 adb 命令，SSH 是 ssh 命令） |
| `devices(timeout)` | 返回与 `adb devices -l` 解析结果同构的列表和错误 |
| `pick_serial(wanted, error)` | ADB 沿用 `adbkit.pick_serial`；SSH 永远返回唯一的合成序列号 |
| `run(args, timeout)` | 短命令，返回 `(rc, text)` |
| `popen(args)` | 长命令 / 传输，返回进程适配器（见下） |

**序列号怎么绑定。** 传输对象是「一次操作」的：工具先 `devices()` 拿列表、`select(devices, config.serial, error)` 得到本次用的序列号（ADB 沿用 `adbkit.pick_serial` 的多设备规则；SSH 在线时返回唯一的合成序列号），再 `build(self, serial)` 出真正执行命令的对象。这样 ADB 的自动选设备逻辑原样保留。

合成序列号只存在于运行时，**绝不写回 `config.serial`**；SSH 模式下前端不渲染也不提交序列号字段，服务端保留原来的值，切回 ADB 时原样使用。

**SSH 探测失败时**：`devices()` 返回空列表和原因，`select()` 返回 `("", 原因)`，与 ADB 无设备时一致；三个工具原有的「没设备就别开始」前置检查继续生效，不会对着连不上的板子开始写。

参数继续使用 adb 风格，SSH 实现负责翻译：

| adb 形式 | SSH 下实际执行 |
|----------|----------------|
| `devices -l` | 一次探测，合成一台设备 |
| `shell CMD` | `ssh … CMD`（CMD 作为一个参数交给远端 shell） |
| `push SRC DST` | `ssh … "cat > DST"`，本地分块写 stdin 并统计字节 |
| `root` | 直接成功（登录用户已经是 root） |
| `remount` | 执行 `ssh.remountCommand` |
| `wait-for-device` | 探测一次，通即返回 0 |
| `reboot` | `ssh … reboot`；连接被断开（rc 255）按「已发出重启」算成功 |

SSH 基础命令（`sshkit.py` 统一拼）：

```
ssh -T -p <port> -o BatchMode=yes -o ConnectTimeout=<n>
    -o StrictHostKeyChecking=<policy> [-i <key> -o IdentitiesOnly=yes]
    <user>@<host> <远程命令>
```

- **`shell` 后面的命令字符串原样传给远端，传输层不再加任何引号。** 现有调用点传的都是已经拼好的完整命令（如 `test -b … && echo …`），工具内部该用 `adbkit.device_quote` 的地方继续用；传输层如果二次转义，复合命令会被当成一个 token，三个工具全坏。
- `push` 的远端路径由传输层用 `device_quote` 转义后拼成 `cat > '路径'`。
- 进度行沿用 adb 的格式（`[ 37%] /mnt/ats-xxx.img`），前端和日志不需要改。

**进程适配器**要实现现有调用点真正用到的东西：`stdout`（可按字节读、可取 `fileno()`）、`stdin`（push 时写本地文件流）、`poll()`、`wait(timeout)`、`terminate()`、`kill()`、`returncode`。`push_file` 的推送路径改成和替换镜像一样的流式读法，因此不要求适配器实现 `communicate()`；如果实现时保留了 `communicate()` 调用，则适配器必须一并支持。

## 7. 各功能在 SSH 模式下的行为

### 7.1 脚本执行

- 传脚本到 `/data/local/tmp/ats-<id>-<name>`（与现在一致，root 可写），执行 `sh 远端路径 参数`。
- 不需要 `adb root`；停止仍是杀本地进程、断开连接，远端脚本可能继续，沿用现有提示语。

### 7.2 替换镜像

- 流程完全不变：推送 → `test -b` → `blockdev` → `wc -c` → `dd` → `sync` → 可选重启。
- SSH 登录即 root，没有 root / remount 步骤。
- 推送镜像时由本地流式计数生成进度行，日志表现与 ADB 模式一致。
- 重启后 SSH 连接断开属于正常现象，按成功处理。

### 7.3 推送文件

- 目录浏览 `ls -a -p -L`；推送流式带进度（现在 push_file 是结束后一次性出结果，这里顺手改成边传边出进度）。
- 预设里的「先 root 并 remount」：SSH 下 root 步骤为空操作，remount 执行配置的命令；页面文案改为「先 remount（SSH 已是 root）」。
- 目标路径校验、预设、本地文件副本逻辑不变。

### 7.4 反向隧道

同一个页面，按当前模式切换两套含义：

| 模式 | 本地目标 | 说明 |
|------|----------|------|
| ADB | `127.0.0.1:5037`（本机 adb server） | 现有行为 |
| SSH | 连接配置里的开发板地址和端口 | `ssh -R 2222:192.168.100.1:22 …` |

- 远端服务器的主机 / 端口 / 用户 / 密钥 / 保活 / 重连沿用现有字段，两种模式共用。
- 页面**不加新字段**，仍是现有的「远程绑定端口 / 本地地址 / 本地端口」；这三个值按模式各存一套（配置文件里分两组），切换模式时页面显示对应的一组，互不覆盖。远端服务器字段（主机、用户、端口、密钥）两种模式共用。
- 标题从「adb 反向隧道」改为通用的「反向隧道」，说明文字随模式变（ADB：把本机 adb server 反向映射到远程；SSH：把开发板 SSH 反向映射到远程）。
- SSH 模式下本地目标默认取 `connection.ssh.host/port`，页面可改（也可以转发板子上的其他服务）。
- 默认只绑远端 loopback；`GatewayPorts` 保留但默认关闭，帮助页说明风险。

## 8. 状态与界面（已确认）

页面风格与现有 ADB 页保持一致：不新起布局，名称栏、读数行、参数/预设/脚本库/日志分栏、按钮都不动。

### 8.1 设置页连接卡片

放在设置页最上方，数据目录卡片之前。ADB / SSH 用分段选择切换，选中哪边就显示哪边的说明/字段：

- ADB：一句说明（设备序列号和 adb 路径仍在各功能页选），状态行显示 `ADB 可用 · N 台设备在线`，按钮为「检测 ADB」「保存并应用」。
- SSH：字段为地址、端口、用户名、密钥文件（带「浏览…」）、主机密钥策略；高级折叠项里放连接超时和 remount 命令；按钮为「测试连接」「保存并应用」。
- 状态行：测试成功显示绿点 + `root@host · 型号`；失败显示红点和可操作原因。
- 有任务在跑时卡片底部出现红条，列出任务名，「保存并应用」禁用。

交互：测试只探测不保存；保存才切换；切换后立即生效，不重启。

### 8.2 侧边栏

- 底部新增「连接」行，放在「服务」上方：`连接 · ADB` 或 `连接 · SSH root@host:22`，点击进设置页连接卡片。
- 「设备」行：ADB 模式与现在完全一样；SSH 模式显示 `设备 · SSH 在线 · 型号` 或 `设备 · 连不上 · <简短原因>`。
- SSH 状态由进程级监听器每 10 秒刷新；保存连接配置、切换数据目录后立即重测。

### 8.3 三个功能页

**布局和 ADB 模式完全一样**，只改两处：

1. 参数区的「adb 路径」整行隐藏；「目标设备」的 `select + 刷新` 换成一行只读的 SSH 目标（绿点 + `SSH · user@host:port` + 「只读」）。
2. 名称栏读数里的「设备」显示 `user@host`。

其余差异只在内容：

- **脚本执行**：远程目录、超时、循环、脚本参数、脚本库、日志全部不变。
- **替换镜像**：参数、预设、预设内容、确认弹窗、重启选项全部不变；没有 root / remount 步骤。
- **推送文件**：「临时文件」标签和目录快捷按钮不变；「预设」标签里的勾选项文案改为「先 remount（SSH 已是 root，不再执行 root）」，提示改为先执行 `mount -o remount,rw /`，成功才开始推。推送时日志里出现按字节的进度行。
- **反向隧道**：页面字段不变（见 7.4），标题改为「反向隧道」，命令预览和说明随模式变；默认仍只绑 loopback。

### 8.4 always-adb

SSH 模式下从侧边栏隐藏；直接访问 `/always-adb` 时显示「本功能仅支持 ADB，切回 ADB 后可用」，启动按钮禁用。

### 8.5 帮助页

补一节 SSH 前提：密钥或 ssh-agent 免密、登录用户是 root、远端隧道默认只在 loopback、主机密钥首次记录后变化会报错。

## 9. 服务端接口

- `GET /api/tools`：返回 `{tools, connection}`；`connection` 含模式、显示名、在线状态和原因。侧边栏用这份数据渲染连接行。SSH 模式下 `tools` 里不包含 always-adb。
- `GET /api/connection`：当前配置 + 状态 + 运行中的任务。
- `POST /api/connection`：保存并应用；校验失败 400，有任务运行 409。
- `POST /api/connection/test`：用提交的 SSH 参数探测，不保存。
- SSH 模式下 `POST /api/always_adb/start` 返回 400。
- 模式切换后：停掉所有工具的设备监听、清空 `_last_online` 和设备状态、广播新状态；不重建 registry（配置文件没变）。
- **SSH 探测只有一个进程级监听器**（`connection.ConnectionMonitor`），由它每 10 秒探测一次，并把合成设备列表推给注册过的工具（`apply_devices` + `publish_state`）；侧边栏直接读它缓存的状态。ADB 模式不启动它，仍用每个工具原有的 `DeviceWatcher`。切换模式、保存配置、切换数据目录时重建/重测；`AppServer` 停止时一并停掉。
- `switch_data_dir` 成功后调用 `connection.reload()`。

## 10. 并发与失败处理

- 连接切换用非阻塞锁；与数据目录切换同一套「有任务在跑就不给切」的规则，避免操作跑到一半换通道。
- SSH 错误给可操作提示：
  - `Permission denied (publickey)` → 没配免密；
  - `Host key verification failed` → 主机密钥变了；
  - `Connection timed out` / `Connection refused` → 地址、端口、网线；
  - `Could not resolve hostname` → 地址写错。
- 停止任务 = 杀本地 ssh 进程；远端 dd / 脚本可能继续跑，沿用现有提示。

## 11. 安全

- `BatchMode=yes`：没有终端，绝不挂在密码提示上。
- 主机密钥默认 `accept-new`，首次记录，之后变化会报错。
- 不保存密钥内容；配置文件里有地址和用户名，属于数据目录的一部分。
- 隧道默认只绑远端 loopback；帮助页写明谁能访问、`GatewayPorts` 打开后的影响。
- SSH 登录是 root：帮助页提示这台板子的 SSH 暴露面。

## 12. 测试

仓库目前没有测试目录，这次建一个 `tests/`，用标准库 `unittest`，不引入 pytest：

- **单元**：临时 PATH 里放假 `ssh` / `adb` 脚本，验证命令拼装、`shell` 参数原样传递、远端路径引号、push 分块与进度、退出码映射、探测失败返回空设备、reboot 255、remount 命令。
- **服务端**：配置校验（必填、端口范围）、运行中 409、`/api/tools` 的连接字段、数据目录切换后重载、always-adb 拦截、模式切换只启停对应的监听器。
- **前端**：设置卡片两种模式、侧边栏连接行、三个页面隐藏 adb 字段并显示只读 SSH 目标、always-adb 提示。
- **回归**：ADB 模式下三个功能用同一套假 adb 重跑，确认行为没变；always-adb 的既有测试也保留。
- **手工**：Windows 上对真实开发板跑四个功能，再切回 ADB 验证序列号等配置没被改动。

## 13. 实施顺序

1. `sshkit.py` + `connection.py` + transport 骨架（ADB 实现保持现有行为）+ 单测。
2. 三个工具接入 transport，ADB 回归。
3. SSH 实现：探测、shell、流式 push 与进度、重启/remount 映射。
4. 设置页连接卡片、侧边栏连接行、服务端接口、模式切换。
5. 隧道双模式、always-adb 拦截、帮助页。
6. 全量测试、README 更新。

## 14. 我替你做的决定（可以推翻）

1. 开关放在**设置页顶部卡片**；侧边栏只显示当前连接并作为入口，不做侧边栏快捷切换。页面布局与现有 ADB 页保持一致（已确认）。
2. 传输层**保留 adb 风格参数**，用 SSH 翻译层减少对三个工具的改动；传输对象按「一次操作」绑定解析后的序列号，ADB 的自动选设备逻辑不动。
3. 传文件用 **`cat >` 流式 + 本地计数**，不用 scp/sftp（非交互下拿不到可靠进度）。
4. SSH 设备状态用**进程级监听器每 10 秒探测**，不维持常驻长连接，也不给每个工具各开一个探测。
5. **always-adb 在 SSH 模式隐藏**，而不是显示但置灰。
6. SSH 下 remount 默认 **`mount -o remount,rw /`**，高级项可改。
7. 隧道的本地目标/远端端口**按模式各存一套**，远端服务器字段共用；标题改为通用的「反向隧道」。
8. 连接配置存 **`config/connection.json`**，跟着数据目录走。
9. 新建 **`tests/`**（标准库 `unittest` + 假 ssh/adb），不再依赖散落在 `/tmp` 的临时测试脚本。
