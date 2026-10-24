# MinecraftProxy

Minecraft Java 版协议代理：在客户端与服务器之间做**字节透明转发**，同时旁路解析区块数据包，
把世界导出为**可直接被 Minecraft 加载**的 `.mca` 区域文件。

这是一个**独立程序**，直接 `python main.py` 运行即可，不需要安装、也不作为库被其它项目引用。

## 特性

| 能力 | 说明 |
|---|---|
| **字节透明** | 只解析数据副本，转发永远是原始字节切片；不会破坏压缩帧 |
| **版本自适应** | 从握手包读 `protocol_version`，精确 ID 表 → 结构探测，**不硬编码包 ID** |
| **可加载的世界** | 方块状态 ID → 真实方块名（`Name` + `Properties`），并生成 `level.dat`（出生点 = 退出位置） |
| **真实生物群系** | 1.8 数字表 / 1.16 Registry Codec / 1.18+ `Registry Data` 动态注册表，逐版本识别 |
| **容器内容** | **打开过的**箱子/木桶/潜影盒/熔炉等，内容直接写进导出世界的方块实体（协议限制：服务器只在你打开时才下发内容） |
| **地图画** | `Map Data` 包累积 128×128 色板，导出为 `data/map_<id>.dat`（用 `/give @s filled_map[minecraft:map_id=<id>]` 取出查看） |
| **实体快照** | 怪物/动物/矿车/掉落物/展示框（仅 1.13+）：类型 + 坐标 + 朝向 + UUID，写入 1.17+ 的 `entities/` 区域文件（装备/血量等元数据暂不采集） |
| **一键自检** | `python main.py --doctor`：Python / 数据表 / 端口 / JDK / agent 逐项检查，问题截图即可求助 |
| **动态世界高度** | 主世界 24 节 / 下界末地 16 节 / 模组可更多，自动适配 |
| **多维度** | 主世界、下界、末地、以及模组自定义维度各自独立目录 |
| **多玩家安全** | 进程级共享锁，并发写同一区域文件不会损坏 |
| **光照保真** | `SkyLight` / `BlockLight` 写入对应 section |

## 支持的版本与服务端

### 客户端 / 服务端协议版本

从握手包读取 `protocol_version` 后自动适配，**无需任何配置**。
包 ID 采用两级策略：**精确 ID 表**（已逐项核对）优先，缺失时回退**结构探测**
（不依赖版本号，按负载二进制结构判断包身份）。

区块线格式分三个家族：`legacy`（1.8）、`flattened`（1.16–1.17）、`modern`（1.18+）。

| 游戏版本 | 协议号 | 区块抓取 | 包 ID 解析 | 状态 |
|---|---|---|---|---|
| 1.8 – 1.8.9 | 47 | ✅ legacy（含 Map Chunk Bulk） | 精确表 | 已专项测试 |
| **1.9 – 1.15.2** | **107 – 578** | **❌ 不支持** | 结构探测 | 区块格式与 1.18 不同 |
| 1.16 – 1.16.5 | 735 – 754 | ✅ flattened（带 `groundUp`） | 754 精确表，其余探测 | 754 已专项测试 |
| 1.17 – 1.17.1 | 755 – 756 | ✅ flattened（`i64[]` 位图，无 `groundUp`） | 结构探测 | 已专项测试 |
| 1.18 – 1.18.2 | 757 – 758 | ✅ modern | 758 精确表 | 已专项测试 |
| 1.19 – 1.21.4 | 759 – 769 | ✅ modern | 结构探测 | 未逐个回归 |
| 1.21.5 – 1.21.10 | 770 – 773 | ✅ modern | **773 精确表**，其余探测 | 773 已实战验证 |
| 1.21.11 | 774 | ✅ modern | 结构探测 | 未逐个回归 |
| 26.3 | 777 | ✅ modern | 精确表 | 已专项测试 |

> **关于 1.9 – 1.15.2**：这段版本的区块包结构与 1.18+ 差异较大
> （有 `bitMap`、无 section 数据长度前缀、生物群系是 256 字节而非 VarInt 数组），
> 目前没有对应的解析器，区块会**解析失败但不影响转发**（客户端仍能正常游戏）。

#### 跨版本插件（ViaVersion / ViaBackwards / ViaFabric）

这类插件**只翻译包 ID 与连接状态机，不翻译区块负载**。实测一个「协议 773」的连接里，
包 ID 是标准 1.21.9 的（`map_chunk=0x2C`），但区块负载仍是旧核心格式：
**16 节（世界高度 256）**、section 内无 `fluid_count`、数据数组无长度前缀。

因此本程序**不按协议号硬套布局**，而是用「必须精确消费全部字节」反推真实 section 布局
（`chunk_format._parse_sections_adaptive`），并据此校正世界高度
（实际 16 节时把最低节 Y 归零，否则整列方块会上下错位 64 格）。

结构探测能识别的包（仅这些，其余包照常转发）：
压缩阈值、区块数据、光照更新、方块实体、状态切换（Login / Configuration / Play）、Join Game 维度。

### 服务端

| 服务端 | 支持 | 说明 |
|---|---|---|
| Vanilla（`online-mode=false`） | ✅ | 主要目标场景 |
| Paper / Spigot / Purpur 等 Bukkit 系 | ✅ | 协议与 Vanilla 一致 |
| Fabric / Forge / NeoForge 模组服 | ✅ | 自定义维度会各自建目录 |
| BungeeCord / Velocity（传统转发） | ✅ | 握手 host 里的 `\0IP\0UUID\0属性` 后缀被完整保留 |
| **Vanilla `online-mode=true`** | **⚠️** | 字节代理无法解密 AES 流量；**检测到 `Encryption Request` 后会明确提示安装 mc-mirror-agent**（见下节），转发不受影响 |

> 离线模式（`online-mode=false`）服务器**无需任何额外组件**，直接使用本代理即可。

### 正版验证服务器（online-mode=true）

字节代理架构上无法解密 AES 流量，因此程序的行为约定是：**离线服零依赖直接用；
正版服检测到加密请求时明确提示，而不是默默无区块**。用代理模式连正版服时，
程序在登录阶段识别到 `Encryption Request`（精确 ID 表 + 结构探测，任意版本可用）后打印：

```text
检测到正版验证服务器（online-mode=true）：服务器发送了 Encryption Request
后续流量为 AES 加密，字节代理无法解析区块；转发不受影响，游戏仍可正常进行
抓取正版服需要安装 mc-mirror-agent（镜像模式），详见 README 的「正版验证服务器」章节
```

此时游戏本身可正常玩，只是代理无法再解析区块。抓取正版服走 **mc-mirror-agent**
（客户端 javaagent）：它在客户端进程内、netty 管道**解密之后**的位置把明文帧
镜像给本程序——加密与正版认证完全由客户端原有逻辑完成，agent 不接触密钥、
不修改任何字节，只旁路复制。

#### 第一步：构建 agent（需要 JDK 8+，一次即可）

```bash
cd agent
build.bat        # Windows；Linux/macOS 用 bash build.sh
# 产物：agent/mc-mirror-agent.jar（约 9MB，内含 byte-buddy）
```

不想开游戏也可以先验证整条链路（netty 测试桩模拟客户端管道）：

```bash
python agent/test/run_smoke.py    # 预期：agent 冒烟: 3 通过, 0 失败
```

#### 第二步：配置启动器（每个游戏实例一次）

在启动器（官方 / PCL / HMCL 均可）的 **JVM 参数** 里追加（注意是完整路径）：

```text
-javaagent:D:\path\to\mc-mirror-agent.jar=127.0.0.1:25566
```

> 也可以不改这一段，用 `-javaagent:D:\path\to\mc-mirror-agent.jar` 加系统属性
> `-Dmcproxy.mirror=127.0.0.1:25566`；地址默认就是 `127.0.0.1:25566`。

#### 第三步：抓取

```bash
python main.py --mirror-port 25566 --world-dir ./downloaded_world
```

然后正常启动游戏，加入任意服务器（**正版 / 离线验证均可**），四处走动收集区块，
`Ctrl+C` 退出生成世界——与代理模式的用法一致。多人菜单的服务器列表 ping
也会被镜像，程序会自动静默处理。

#### 兼容性与说明

- agent 只插桩包名稳定的 `io.netty`（客户端捆绑库），**不引用任何混淆的
  Minecraft 类**，原版 / Fabric / Forge 客户端通用；依赖 vanilla 管道的
  字符串常量名（`splitter` / `prepender` / `decrypt` / `decompressor`，1.8 起未变）
- 加密/压缩 handler 是登录期动态插入的，agent 会在插入时自动重定位抽头锚点
- 镜像队列满时丢帧不阻塞游戏；agent 连不上本程序时静默，游戏完全不受影响
- 开游戏前也可预判目标服是否要求加密：`python tools/precheck.py <目标> --probe-login`

### 客户端

代理只做字节转发，**客户端版本与目标服务端保持一致即可**，本身不限制版本。

### 方块数据表覆盖的版本

首次运行会从 PrismarineJS 下载方块表并缓存到 `core/data/`，已收录 16 个版本：

| 协议 | 目录 | 协议 | 目录 |
|---|---|---|---|
| 47 | 1.18 | 768 | 1.21.3 |
| 754 | 1.16.2 | 769 | 1.21.4 |
| 758 | 1.18 | 770 | 1.21.5 |
| 763 | 1.20 | 771 | 1.21.6 |
| 764 | 1.20.2 | 772 | 1.21.8 |
| 765 | 1.20.4 | 773 | 1.21.9 |
| 766 | 1.20.6 | 774 | 1.21.11 |
| 767 | 1.21.1 | 777 | 26.3 |

未收录的版本会按同协议号的相邻目录回退（例如 755/756 回退到 `1.17`）；
全部失败时用 `--block-map` 手动指定。

## 环境要求

- **Python >= 3.10**（使用了 `X | None` 注解语法）
- **零第三方依赖**（仅标准库）

```bash
python --version    # 需 >= 3.10
```

## 目录结构

```
MinecraftProxy/
├── main.py                 # 程序入口（直接运行这个文件）
├── run_tests.py            # 测试入口
├── pyproject.toml          # 仅用于配置 mypy / ruff（无打包配置）
├── core/                   # 程序内部模块（非对外库）
│   ├── __init__.py
│   ├── proxy.py            # TCP 转发 + 会话管理（代理模式）
│   ├── mirror.py           # 镜像模式服务端（接收 agent 的明文帧，正版服用）
│   ├── protocol.py         # 状态机 + 区块/注册表处理
│   ├── packet_ids.py       # 版本自适应包 ID 解析
│   ├── chunk_format.py     # 区块包 → Anvil NBT
│   ├── chunk_extractor.py  # .mca 区域文件写入
│   ├── block_registry.py   # 方块状态 ID → 方块名
│   ├── biome_registry.py   # 生物群系 ID → 名称（数字表 / 网络注册表）
│   ├── legacy_blocks.py    # 1.8 数字 (id, meta) → 现代方块名
│   ├── level_dat.py        # level.dat 生成
│   ├── nbt.py              # NBT 读写
│   ├── datatypes.py        # 二进制读取器
│   ├── varint.py           # VarInt / 帧切分
│   ├── logger.py           # 日志
│   └── data/               # 方块数据表缓存（首次运行自动下载）
├── agent/                  # mc-mirror-agent（正版服镜像模式的 javaagent）
│   ├── src/mcproxy/        # agent 源码（premain + netty 插桩 + 镜像客户端）
│   ├── lib/                # byte-buddy（打进 fat jar）/ netty（仅编译期）
│   ├── build.bat / .sh     # 一键构建 mc-mirror-agent.jar
│   └── test/               # 无需游戏的链路冒烟测试（netty 测试桩）
├── tests/                  # 测试套件
│   ├── fixtures/           # 真实服务器抓包固件（ViaVersion 未翻译负载）
│   └── realdata_test.py    # 真实数据回归
├── tools/
│   └── precheck.py         # 目标服务器预检（协议号 / 可抓取性 / online-mode）
└── docs/handoff/           # 对接文档
```

## 使用

```bash
# 交互式（会依次询问监听地址、端口、目标服务器）
python main.py

# 直接指定目标
python main.py --target mc.example.com:25565

# 离线环境（禁止自动下载方块数据）
python main.py --target 127.0.0.1:25565 --no-download

# 手动指定方块数据（原版数据生成器的 reports/blocks.json 也可用）
python main.py --target mc.example.com --block-map ./blocks.json

# 镜像模式（正版验证服务器，需 mc-mirror-agent，见下文）
python main.py --mirror-port 25566 --world-dir ./downloaded_world

# 一键自检（环境 / 数据表 / 端口 / JDK / agent）
python main.py --doctor

# 调试日志
python main.py --target mc.example.com --debug
```

然后在 Minecraft 客户端中连接代理监听地址（默认 `127.0.0.1:25565`）。
退出后世界文件位于 `--world-dir`（默认 `./downloaded_world`），可直接用「选择世界」加载。

### 参数一览

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--local-host` | `127.0.0.1` | 本地监听地址 |
| `--local-port` | `25565` | 本地监听端口 |
| `--target` | 交互询问 | 目标服务器 `host[:port]`（镜像模式下不需要） |
| `--mirror-port` | 无 | 镜像模式：监听该端口接收 mc-mirror-agent 的明文帧 |
| `--world-dir` | `./downloaded_world` | 世界输出目录 |
| `--block-map` | 无 | 指定 `blocks.json` |
| `--no-download` | 关 | 禁止联网下载方块表 |
| `--debug` | 关 | 输出 DEBUG 日志 |
| `--max-connections` | `8` | 同时允许的客户端连接数上限 |
| `--no-prompt` | 关 | 不交互提问（适合脚本） |

按 `Ctrl+C`（或发送 `SIGTERM`）即可优雅退出：程序会先停止接受新连接、
等待所有正在写入的区块落盘（含 `fsync`），再生成 `level.dat`。

### 实战测试流程

**第一步：预检目标服务器**（不需要开游戏）

```bash
python tools/precheck.py mc.example.com:25565
# 可一次检查多个
python tools/precheck.py a.example.com:25565 b.example.com:25566
```

它会用「服务器列表 ping」返回 MOTD / 版本名 / 协议号 / 在线人数，
并判断该协议号是否**有区块解析器**：

```
  目标: mc.example.com:25565
  MOTD      : 'A Minecraft Server'
  版本名    : 1.21.5
  协议号    : 770
  在线人数  : 12 / 100
  抓取支持  : ✅ 可抓取  1.21.5 · modern 家族

  → 可以用于抓取。注意仍需确认服务器为 offline-mode（离线验证）。
```

预检默认**不登录**，所以无法判断 `online-mode`。加上 `--probe-login`
可以真正登录一次，直接判定服务器是否要求加密：

```bash
python tools/precheck.py mc.example.com:25565 --probe-login --protocol 773
```

```
  登录探测  : 协议 773 -> ✅ 接受该协议号（Set Compression）
  登录探测  : 协议 773 -> 🔐 要求加密（online-mode=true）→ 本代理无法处理
  登录探测  : 协议 773 -> ⚠️ 版本不匹配（服务器拒绝协议 773）
```

> ⚠️ 在线模式服在 `Set Compression` 之后是 AES 加密流，本代理无法处理（架构性限制）。

**第二步：启动代理**

```bash
python main.py --target mc.example.com:25565 --world-dir ./downloaded_world --debug
```

**第三步：客户端连接 `127.0.0.1:25565`**（不是直接连目标服），
进入世界后走动一会儿，让服务器下发足够多的区块。

> **有登录插件（进服输密码 / 看书）时**：密码交互是 serverbound 的聊天/插件消息，
> 本代理不解析、不受影响。但**未验证前玩家通常被卡在 lobby/limbo**，那里几乎不下发区块。
> 请**先把密码输完、进到正式世界**，再四处走动 30 秒以上，最后 `Ctrl+C`。

**第四步：`Ctrl+C` 退出**，等待日志出现 `代理已关闭`，然后加载
`downloaded_world`。

> 客户端退出游戏后，会话会**立即结束**（不再继续处理服务端推送），
> 你会立刻看到 `会话结束` 与 `区块统计` 两行日志。

**日志对照表**（按顺序出现才说明一切正常）：

| 日志 | 含义 |
|---|---|
| `服务器设置压缩阈值: 256` | 服务器启用了压缩；**若之后没有下一步，通常是在线模式服** |
| `登录成功: <玩家名> (UUID: ...)` | 登录通过 |
| `进入 PLAY 状态 (维度: minecraft:overworld)` | 握手全部完成 |
| `区块统计: 成功=N, 失败=0, 维度=1` | 退出时的汇总；`失败` 应为 0 |

出现 `方块注册表不可用` 表示方块数据没下载成功，此时世界能加载但方块会退化为空气占位。

### 生成方块数据（可选，最准确）

在服务端目录执行，得到 `generated/reports/blocks.json`：

```bash
java -DbundlerMainClass=net.minecraft.data.Main -jar server.jar --reports
```

## 测试

```bash
python run_tests.py                # 全部套件
python run_tests.py selftest       # 单个套件（子串匹配）
python tests/audit.py              # 能力边界审计
```

当前 15 个套件共 745 条断言，全部通过：

| 套件 | 断言数 | 覆盖 |
|---|---|---|
| `selftest.py` | 63 | NBT / 区块解析 / 区域写入 / ID 解析 |
| `misdetect_test.py` | 15 | 常见包不被误判为区块包 |
| `e2e_test.py` | 18 | 真实 TCP 端到端 |
| `version_test.py` | 10 | 无 ID 表时的结构探测 |
| `edge_test.py` | 34 | 直接调色板 / 光照（含前缀容错）/ 方块实体 NBT / 探测重试 / 分片边界 |
| `usability_test.py` | 91 | 导出世界可用性（含区块顶层键 / 生物群系调色板类型 / level.dat 结构） |
| `multiversion_test.py` | 87 | 1.8 / 1.16.5 / 1.17 / 1.18.2（含 Map Chunk Bulk） |
| `version_dispatch_test.py` | 20 | 版本分派（家族 / 几何 / 维度） |
| `robustness_test.py` | 70 | 负数 VarInt / 落盘 / 内存上限 / 网络 / 信号 / 客户端断开即结束 |
| `realdata_test.py` | 56 | 真实抓包（ViaVersion 未翻译负载）/ 1.21.9 精确表 |
| `biome_test.py` | 97 | **生物群系识别**（1.8 / 1.16 数字表 / 1.18+ 网络注册表 / 降采样 / 归一化） |
| `onlinemode_test.py` | 16 | **正版服检测**（Encryption Request 探测 / 精确表别名 / agent 安装提示 / 镜像模式不告警） |
| `mirror_test.py` | 11 | **镜像模式**（协议 magic / 登录链端到端 / 真实固件落盘 / STATUS 静默） |
| `extracapture_test.py` | 53 | **附加捕获**（容器三格式解析与两条合并路径 / 地图色板索引 / 实体 NBT 逐项 / 出生点 / doctor） |
| `spec_validator.py` | 104 | **独立**规范校验（自写 NBT 读取器，对照真实 1.21.10 世界） |

> 注：`spec_validator.py` 依赖本地金样本数据（真实 1.21.10 世界 + 官方
> `blocks.json`，位于项目根的 `标准文件/`，已列入 `.gitignore` 不入库）。
> 缺失时该套件打印「[跳过]」并整体跳过，其余 11 个套件不受影响。

静态检查（可选，需要先 `pip install mypy ruff`）：

```bash
mypy core
ruff check .
```

## 已知限制

- **在线模式服务器（`online-mode=true`）代理模式无法抓取**：`Set Compression` 之后服务端发送
  `Encryption Request`，后续流量为 AES 加密，字节转发无法解密（架构性限制）。
  代理会**自动检测并明确提示**安装 mc-mirror-agent（见「正版验证服务器」一节），
  转发与游戏不受影响。
- **协议 107 – 578（1.9 – 1.15.2）区块无法抓取**：这段版本的区块线格式与 1.18+ 差异较大，
  尚无对应解析器。转发不受影响，只是导出的世界里没有区块。
  （注：1.15+ 的「3D 生物群系」在 1.15 并未实际生效，1.16 才在**下界**真正启用）
- **ViaVersion 等跨版本插件的服务器**：包 ID 与状态机被翻译，但区块负载可能仍是旧核心格式。
  本程序会自适应反推真实 section 布局并校正世界高度，实测（1.21.10 客户端 + ViaFabric
  连 1.8.9 服务器）已能正确抓取。若遇到解析失败，`--debug` 会打印
  `区块包解析失败 (协议 …, 家族 …, 节数 …)` 便于定位。
- **容器内容只覆盖「打开过的」容器**：服务器只在你打开时才下发内容（协议设计），
  站在箱子旁边不会下载里面的东西；1.20.5+（协议 766+）的物品组件格式暂不支持，
  即容器内容捕获当前覆盖 1.8 ~ 1.20.4（精确表版本 + 结构探测）
- **实体是「看到过的快照」**：类型/坐标/朝向/UUID（1.13+），装备、生物血量、
  展示框内物品等元数据暂不采集；已离开视野的实体若未收到销毁包会留在快照里
- **地图画需要对应 mod/指令取出查看**：导出的 `data/map_<id>.dat` 可用
  `/give @s filled_map[minecraft:map_id=<id>]`（1.20.5+）取出
- **生物群系已真实识别**（不再是占位值）：
  - **1.18+**：从配置阶段的 `Registry Data`（`minecraft:worldgen/biome`）读取，
    **列表顺序即 ID**（支持模组服的上百项动态注册表）
  - **1.16 / 1.17**：从 `Join Game` 的 `Registry Codec`（NBT）读取
  - **1.8 ~ 1.15**：用内置的旧版数字 ID 表（0~167 / 0~173）
  - 拿不到注册表时回退为 `minecraft:plains` 占位（世界仍可加载，仅颜色不准）
  - 旧版群系名会归一化到 1.18+ 的等价名（如 `snowy_tundra` → `snowy_plains`）
- **首次运行需联网**下载方块数据表（缓存到 `core/data/`）。离线环境请用 `--block-map`
  指定，或接受退化为空气占位（世界仍可加载，但方块为空）。

## 兼容性说明（按官方规范实现）

导出的世界**严格遵循 Anvil / Region 文件格式**，可被游戏本体、C2ME、MCEdit 等标准工具读取：

- 区块记录字段顺序 = `[长度 4B 大端][压缩类型 1B][压缩数据]`
- **长度值 = 1（压缩类型字节）+ 压缩数据长度**（「剩余全部字节数」）
- 超过 255 扇区的区块外置为 `c.<x>.<z>.mcc`，区域文件里写「压缩类型 + 128、长度 1」
- 区块顶层键与官方一致：`Status` / `isLightOn` / `structures{starts,References}` /
  `block_ticks` / `fluid_ticks` / `PostProcessing` / `block_entities` 等
- `biomes.palette` 元素为 **String**（资源位置），不是 Compound；
  生物群系为 4×4×4 单元（64 项，索引 `y4*16 + z4*4 + x4`）
- `level.dat` 含 `WorldGenSettings`（`seed` + `dimensions`）与**顶层** `DragonFight`
  （与 `WorldGenSettings` 同级；**不存在** `DimensionData` 键）

> 这些细节曾多次实现错误：
> 1. 区块记录顺序对调 + 长度少算 1 → 世界**完全无法加载**
> 2. `DragonFight` 误放进 `DimensionData → 1` → 游戏报 `key missing: DragonFight`
> 3. `biomes.palette` 写成 Compound → 每个 section 报 `(Not a string -> using default)`
>
> 现已用「不依赖项目代码的独立校验器」对照**真实 1.21.10 世界**（Paper 数据生成器
> 产出，含 29671 个方块状态）逐项验证通过（104/104），其中包含**生物群系名称交叉校验**
> （我们导出的 `minecraft:deep_lukewarm_ocean` 等均与真实世界一致）。
> 教训：项目内部测试**无法**发现「自洽但不符合规范」的缺陷——必须用独立实现校验。

## 协议参考

- [Java Edition protocol](https://minecraft.wiki/w/Java_Edition_protocol/Packets)
- [Chunk format](https://minecraft.wiki/w/Chunk_format)
- [Region file format](https://minecraft.wiki/w/Region_file_format)
- [Java Edition level format](https://minecraft.wiki/w/Java_Edition_level_format)
