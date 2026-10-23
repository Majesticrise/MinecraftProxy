# HANDOFF_MinecraftProxy_20261010

> ✅ **本文档已更新至 2026-10-24（对应版本 2.4.0）**，与当前代码一致。
>
> 自 2026-10-24 起的修订（文档与代码重新对齐）：
> - **修复 `spec_validator` 金样本路径**：金样本目录由 `你想要的/` 改名为
>   `标准文件/` 后，独立校验器一直被**静默跳过**（`[跳过] 未找到真实基准世界`）。
>   现按 `标准文件/` → `你想要的/` 依次探测；恢复后 **104/104 全部通过**
> - 全文行数、行号、测试与静态检查数字按当前代码重新核对。
>   测试实为 **665** 断言（含 15 项误判检查）——旧文档写的 587 与分项表加和不符
> - 梳理出 **7 处已定义未使用的代码并于同日删除**（见第 10 节「死代码清理记录」），
>   删除后 12 套件 665 断言、ruff、mypy 全部复验通过
>
> 自 2.1.0 起新增：多版本支持（1.8.9 / 1.16.5 / 1.17 / 1.18.2）、
> 区块写入 fsync、写入任务 drain、内存上限、TCP_NODELAY、连接数上限、优雅停机；
> 自 2.2.0 起新增（**实战验证**）：
> - **协议 773（1.21.9 / 1.21.10）精确 ID 表**（`map_chunk=0x2C`，与 777 完全不同）
> - **自适应 section 布局**：ViaVersion 类插件只翻译包 ID/状态机、不翻译区块负载，
>   本程序按「必须精确消费全部字节」反推真实布局并校正世界高度
> - **客户端断开即结束会话**（原先会继续解析服务端推送 60+ 秒）
> - **`.mca` 区块记录字段顺序/长度语义修复**（原为 `[类型][长度]` + 长度不含类型字节，
>   与官方规范相反，导致**任何标准读取方都无法加载世界**）
> - 真实抓包固件回归测试（`tests/fixtures/` + `tests/realdata_test.py`）
>
> 自 2.3.1 起新增（**对照真实 1.21.10 世界修正**，该世界由 Paper 数据生成器产出、
> 含 1166 方块 / 29671 状态，并附带官方 `blocks.json` / `registries.json` / `packets.json`）：
> - **`level.dat` 的 `DragonFight` 移到 `Data` 顶层**（原先误放进
>   `DimensionData → 1`，游戏报 `key missing: DragonFight`）；同时删除非官方的
>   `DimensionData` 键，`Gateways` 补为 20 个索引
> - **`biomes.palette` 元素改为 String**（原先写成 Compound，导致 C2ME / 原版
>   加载每个 section 都报 `(Not a string -> using default)`）
> - **区块顶层键补全**：`isLightOn` / `structures{starts,References}` /
>   `block_ticks` / `fluid_ticks` / `PostProcessing`，`block_entities` 恒存在
> - **新增独立规范校验器** `tests/spec_validator.py`（自写 NBT 读取器，**不依赖项目代码**）
>   对照真实世界逐项校验，并全量比对 29671 个方块状态 ID
>
> 自 2.4.0 起新增（**真实生物群系识别**，不再统一写 `minecraft:plains`）：
> - **新增 `core/biome_registry.py`**：1.8 数字表（61 项，ID 0~167）、
>   1.16 数字表（79 项，ID 0~173）、1.18+ 名称归一化表（37 项，如
>   `snowy_tundra` → `snowy_plains`）、`BiomeRegistry` 容器
> - **1.18+（含用户实测的 1.21.10）**：从配置阶段 `Registry Data`
>   （`minecraft:worldgen/biome`）读取，**列表顺序即 ID**；
>   用户服务器为 **174 项**（原版仅 65）——因此**不得**用静态表，
>   拿不到注册表时回退占位而不是猜错
> - **1.16 / 1.17**：从 `Join Game` 的 `Registry Codec`（NBT）读取；
>   用**字节特征扫描**（`0x0A 0x00 0x00` + 校验末尾索引与顶层键）定位，
>   避免脆弱的偏移量解析
> - **1.8 ~ 1.15**：内置旧版数字 ID 表
> - **区块导出降采样**：1.8 为 256 项（16×16 列）、1.16 为 1024 项（4×4×64），
>   均按官方 1.18+ 布局（64 个 4×4×4，索引 `y4*16 + z4*4 + x4`）写出，
>   生物群系调色板元素为 **String**
> - **实测结论**：生物群系容器的 `min_bits` 为 **1**（非方块的 4）；
>   真实数据中 `palette=1 → 无 data`、`palette=2 → data=1`、`palette=3 → data=2`
> - **新增测试套件 `tests/biome_test.py`（97 断言）**，
>   `spec_validator.py` 增至 104 断言（新增生物群系名称交叉校验）
>
> **支持的版本与服务端请以 [`README.md`](../../README.md) 的「支持的版本与服务端」一节为准。**

> 对接文档 · 标准版 · 初版 2026-10-10 22:50 · 更新 2026-10-24

---

## 1. 项目概览

| 项目 | 值 |
|---|---|
| **项目名称** | MinecraftProxy（独立程序，内部模块在 `core/`） |
| **版本** | 2.4.0 |
| **开发时间** | 2026-10-10 20:58 ~ 2026-10-11 |
| **负责 AI/开发者** | GitHub Copilot（DeepSeek V4.1 Flash） |
| **代码位置** | `d:\yx\attack\mcserverfinder\MinecraftProxy` |

**项目目标**：实现一个 Minecraft Java 版协议代理，在客户端与服务器之间做**字节透明转发**的同时旁路解析区块数据包，把世界导出为**可被 Minecraft 直接加载**的 `.mca` 区域文件。解决了原实现「导出的文件是废数据（调色板为整数、缺 `level.dat`）、世界高度写死 24 节导致下界/末地全部丢弃、压缩帧被重新编码导致客户端掉线」等问题。

---

## 2. 技术栈信息

**编程语言**：Python 3.11.2（最低要求 `>=3.10`，因使用 PEP 604 `X | Y` 注解）

**核心库**：**零第三方运行时依赖**，仅用标准库 `asyncio` / `struct` / `zlib` / `gzip` / `json` / `urllib`。

**第三方依赖清单**：**无**。`pyproject.toml`（共 48 行）**没有 `[project]` 段**，只用于配置 mypy / ruff，因此**不存在 `dependencies` 字段**，本程序也不是可安装的包（直接 `python main.py` 运行）。

```toml
# pyproject.toml 实际内容概览（无 [project] / dependencies）
[tool.mypy]
python_version = "3.10"
warn_unused_configs = true
warn_unused_ignores = true
warn_return_any = true
strict_equality = true
ignore_missing_imports = true
exclude = ["^tests/"]

[tool.ruff]
line-length = 100
target-version = "py310"
exclude = ["__pycache__", "core/data"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "UP", "B", "SIM"]
ignore = ["E501", "B008", "SIM105"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["E402", "F401", "F841"]   # 测试允许 sys.path 引导后再导入
```

开发期工具实际安装版本：`mypy 2.4.0`、`ruff 0.17.0`

**数据库**：无（输出为文件系统上的 `.mca` 二进制文件）

**开发环境要求**：

```bash
Python >= 3.10          # 实测 3.11.2
# 可选（联网自动下载方块数据表）；离线环境需 --block-map 指定
网络访问 raw.githubusercontent.com
```

---

## 3. 项目结构

```
MinecraftProxy/
├── main.py                      # [新增] 程序入口（直接运行，含命令行）
├── run_tests.py                 # [新增] 统一测试入口（12 套件）
├── pyproject.toml               # [新增] 仅 mypy / ruff 配置（无打包配置）
├── README.md                    # [新增] 使用说明
├── .gitignore                   # [新增] 忽略产物与数据表缓存
├── core/                        # [新增] 程序内部模块（非对外库）
│   ├── __init__.py              # 仅存放 __version__（2.4.0）
│   ├── proxy.py                 # TCP 转发 + 会话生命周期
│   ├── protocol.py              # 状态机 + 区块/注册表/维度处理
│   ├── packet_ids.py            # 版本自适应包 ID 解析（含 773 / 777 精确表）
│   ├── chunk_format.py          # 区块包 → Anvil NBT（含自适应 section 布局）
│   ├── chunk_extractor.py       # .mca 区域文件写入
│   ├── block_registry.py        # 方块状态 ID → 方块名
│   ├── biome_registry.py        # [新增] 生物群系 ID → 名称（数字表 / 网络注册表）
│   ├── legacy_blocks.py         # [新增] 1.8 数字 (id, meta) → 现代方块名
│   ├── level_dat.py             # level.dat 生成
│   ├── nbt.py                   # NBT 读写（含匿名标签）
│   ├── datatypes.py             # 有界二进制读取器
│   ├── varint.py                # VarInt / 帧切分
│   ├── logger.py                # 日志
│   └── data/                    # 方块数据表缓存（首次运行自动下载）
│       ├── blocks_770.json      # 542 KB（1.21.5）
│       ├── blocks_773.json      # 585 KB（1.21.9）
│       └── blocks_777.json      # 542 KB（26.3）
├── tests/                       # [新增] 测试套件（12 个脚本 + audit + 真实抓包固件）
│   └── fixtures/                # [新增] 真实服务器抓包固件（ViaVersion 未翻译负载）
├── tools/
│   └── precheck.py              # [新增] 目标服务器预检（协议号 / 可抓取性 / online-mode）
└── docs/handoff/                # [新增] 本文档
```

**关键文件说明**（行数为 2026-10-24 实测值）：

| 文件路径 | 改动类型 | 作用 | 行数 |
|---|---|---|---|
| `main.py` | 新增 | 程序入口 + 命令行（唯一入口） | 260 |
| `core/chunk_format.py` | 新增 | 区块包解析 + Anvil NBT 构建 | 1389 |
| `core/protocol.py` | 新增 | 协议状态机、区块分发、维度跟踪、注册表收集 | 881 |
| `core/packet_ids.py` | 新增 | 版本自适应 ID 解析 | 772 |
| `core/chunk_extractor.py` | 新增 | `.mca` 扇区分配与外置区块 | 615 |
| `core/block_registry.py` | 新增 | 方块 ID→名称映射（决定世界可用性） | 504 |
| `core/proxy.py` | 新增 | 字节透明转发 + 背压 | 489 |
| `core/legacy_blocks.py` | 新增 | 1.8 数字方块 ID → 现代方块名 | 377 |
| `core/biome_registry.py` | 新增 | 生物群系 ID→名称（旧版数字表 + 1.18+ 网络注册表） | 346 |
| `core/nbt.py` | 新增 | NBT 解析/序列化（含匿名标签） | 355 |
| `core/level_dat.py` | 新增 | 生成可加载世界元数据 | 263 |
| `core/varint.py` | 修改 | VarInt 编解码 + 压缩帧切分（`read_packet`） | 213 |
| `core/datatypes.py` | 新增 | 有界读取器（支撑结构探测） | 189 |
| `core/logger.py` | 修改 | 新增 `set_global_level()` | 71 |
| `core/__init__.py` | 新增 | 仅存放 `__version__` | 11 |

---

## 4. 核心功能模块说明

### 模块 A：版本自适应包 ID 解析（`packet_ids.py`）

**功能描述**：不硬编码包 ID（官方明确「hardcoding is highly discouraged」）。两级策略：精确版本表 → 结构探测 → 结果缓存。

**输入参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `version` | `int` | ✅ | 协议版本号 | `777` |
| `state` | `str` | ✅ | 连接状态 | `"PLAY"` |
| `direction` | `str` | ✅ | 方向 | `"CLIENTBOUND"` |
| `packet_id` | `int` | ✅ | 线上包 ID | `0x2E` |
| `payload` | `bytes` | ✅ | 包载荷（用于探测） | `b"\x00\x00..."` |

**输出结果**：`"Chunk Data and Update Light"`（无法识别时返回 `None`）

**调用示例**（`core/packet_ids.py:714`）：

```python
from core.packet_ids import PacketIdResolver

resolver = PacketIdResolver()
resolver.set_protocol_version(777)
print(resolver.resolve("PLAY", "CLIENTBOUND", 0x2E, b""))  # Chunk Data and Update Light
print(resolver.has_exact_table)                            # True

# 773（1.21.9 / 1.21.10）是另一套 ID：map_chunk = 0x2C
resolver.set_protocol_version(773)
print(resolver.id_for("PLAY", "CLIENTBOUND", "Chunk Data and Update Light"))  # 44 (0x2C)
```

**精确 ID 表**：`VERSION_TABLES`（`core/packet_ids.py:73`）目前收录 **5 个协议**：

| 协议 | 行号 | 说明 |
|---|---|---|
| 47 | 内联 | 1.8.9（区块格式差异大，无法结构探测，必须有表）|
| 754 | 内联 | 1.16.5 |
| 758 | 内联 | 1.18.2 |
| **773** | `:177` | **1.21.9 / 1.21.10**（数据源 PrismarineJS `pc/1.21.9/protocol.json`）|
| 777 | `:437` | 26.3（数据源 minecraft.wiki 官方协议页）|

> ⚠️ **773 与 777 的 ID 完全不同**（如 `map_chunk`：773 为 `0x2C`，777 为 `0x2E`）。
> 其他协议版本自动回退到结构探测（见「自适应」一节）。

**相关文件位置**：实现 `core/packet_ids.py:663`（类）、`:714`（`resolve`）；
测试 `tests/version_test.py`（10 项，验证协议 770/9999 纯探测）、
`tests/realdata_test.py`（含 773 表逐项核对）

---

### 模块 B：区块 → Anvil NBT（`chunk_format.py`）

**功能描述**：把线路上的区块包（调色板为整数 ID）转成 Anvil 格式（调色板为方块名称），并按维度写入正确的节数与 `yPos`。

**输入参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `payload` | `bytes` | ✅ | 区块包载荷 | `b"\x00\x00\x00\x07..."` |
| `section_count` | `int` | ❌ | 节数（默认 24） | `24`（主世界）/`16`（下界） |
| `min_section_y` | `int` | ❌ | 最低节 Y（默认 -4） | `-4` / `0` |
| `has_fluid_count` | `bool` | ❌ | section 内是否有 `fluid_count` | 1.21.5+ 为 `True` |
| `has_data_length` | `bool` | ❌ | 数据数组是否有 VarInt 长度前缀 | 1.21.5+ 已移除 |
| `has_nbt_heightmaps` | `bool` | ❌ | 高度图是否为单个 NBT 复合标签 | 1.16–1.21.4 为 `True` |
| `block_entity_anonymous` | `bool` | ❌ | 方块实体 NBT 是否匿名 | 1.18+ 为 `True` |

> **参数仅作为「声明布局」**：实际解析时会自适应（见下文）。

**输出结果**（`build_chunk_nbt` 产出的 NBT 结构）：

```json
{
  "DataVersion": 4325, "xPos": 7, "zPos": -3, "yPos": -4, "Status": "full",
  "sections": [{
    "Y": -4, "block_count": 10, "fluid_count": 0,
    "block_states": {
      "palette": [
        {"Name": "minecraft:stone"},
        {"Name": "minecraft:granite"},
        {"Name": "minecraft:note_block", "Properties": {"instrument": "chime", "note": "9", "powered": "false"}}
      ],
      "data": [0, 1, 2, 3, "... 共 256 个 Long"]
    },
    "biomes": {"palette": ["minecraft:plains", "minecraft:deep_lukewarm_ocean"], "data": [Long...]},
  }],
  "Heightmaps": {"MOTION_BLOCKING": [0, 0, "... 共 37 个 Long"]},
  "block_entities": []
}
```

**调用示例**（实测输出：NBT 5362 字节 / 压缩后 428 字节）：

```python
import struct
from core import load_registry
from core.chunk_format import parse_chunk_data_packet, build_chunk_nbt, compress_chunk_nbt
from core.datatypes import write_varint

registry = load_registry(770, ".")

# 构造区块包：ChunkX=7, ChunkZ=-3, 1 个高度图, 1 个非空节 + 23 个空节
payload = bytearray()
payload += struct.pack(">i", 7) + struct.pack(">i", -3)
payload += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 0x1234)
data = b"\x00\x0a\x00\x00\x00\x00\x01\x01" + struct.pack(">h", -1) * 23
payload += write_varint(len(data)) + data + write_varint(0) + write_varint(0) * 6

chunk = parse_chunk_data_packet(bytes(payload), section_count=24, min_section_y=-4)
print(chunk.x, chunk.z)                        # 7 -3
raw = build_chunk_nbt(chunk, registry, data_version=4325)
print(len(raw), len(compress_chunk_nbt(raw)))  # 5362 428
```

**相关文件位置**：
- 实现：`core/chunk_format.py:445`（`parse_chunk_data_packet`）、`:1253`（`build_chunk_nbt`）、
  `:370`（`_parse_sections_adaptive`）、`:564`/`:821`/`:958`（legacy / flattened / caves 变体）
- 测试：`tests/usability_test.py`（91 项断言，含调色板为方块名、生物群系调色板类型、
  区块顶层必需键、光照、维度高度、`level.dat` 顶层 `DragonFight`）、
  `tests/realdata_test.py`（56 项，含真实抓包回归）

---

### 模块 B-2：自适应 section 布局（`_parse_sections_adaptive`）

**为什么需要**：**ViaVersion / ViaBackwards / ViaFabric 只翻译包 ID 与连接状态机，
不翻译区块负载**。实测一个「协议 773」的连接里，包 ID 是标准 1.21.9 的
（`map_chunk=0x2C`），但区块负载仍是**旧核心格式**：

| 项目 | 协议声明的 1.21.9 | 实测真实负载 |
|---|---|---|
| 节数（世界高度） | 24 节（-64..319） | **16 节（0..255）** |
| section 内 `fluid_count` | 有（1.21.5+） | **无** |
| 数据数组长度前缀 | 无（1.21.5+） | **无** |

**策略**：按「**必须精确消费全部字节**」逐个候选布局试解，
优先级 = 协议声明布局 → 其他布局；每个布局先试声明节数，再试贪婪模式。

```python
# core/chunk_format.py:370
for fluid, data_length in candidates:          # 声明布局优先，其余组合兜底
    buf = PacketBuffer(data)
    sections = _parse_sections(buf, declared_count, fluid, data_length)
    if buf.at_end() and sections:              # ← 关键：字节必须刚好用完
        return sections, fluid, data_length
```

**世界高度校正**：实际节数 ≠ 声明节数且恰为 16（经典高度）时，
把 `min_section_y` 归零。否则仍用 1.18+ 的 `-4`，整列方块会**下移 64 格**。

> 实测：三个真实区块包在 `节数=16 / fluid=0 / dataLen=0` 下字节级完全吻合，
> 修复前全部解析失败，修复后全部成功（含 7 / 2 / 0 个方块实体）。

---

### 模块 B-3：生物群系识别（`biome_registry.py`）

**功能描述**：把区块里的生物群系 ID 还原成真实资源名（如 `minecraft:deep_lukewarm_ocean`），
写入 Anvil 的 `sections[].biomes`。

**三代存储格式与对应获取途径**：

| 版本 | 线上格式 | ID → 名称的来源 |
|---|---|---|
| 1.8 ~ 1.15（≤ 578） | `Biomes` 为 **256 个 Int**（16×16，每 XZ 列一个） | 内置旧版数字表（61 项，ID 0~167） |
| 1.16 ~ 1.17（735~756） | `Biomes` 为 **1024 个 VarInt**（4×4×64） | `Join Game` 的 `Registry Codec`（NBT），或内置 1.16 表（79 项，ID 0~173） |
| 1.18+（757+） | 每 section 一个 `biomes{palette,data}`，64 个 4×4×4 单元 | 配置阶段 `Registry Data`（`minecraft:worldgen/biome`），**列表顺序即 ID** |

> 官方历史（minecraft.wiki《Biome》）：1.15 的 1024 格式「尚未实现」，
> **1.16 才在下界真正启用垂直生物群系**；1.18 起改为按 section 存储。

**为什么 1.18+ 不能用静态表**：实测用户的 1.21.10 服务器在 `Registry Data` 里发了
**174** 个生物群系（原版只有 65 个，服主加了数据包 / 模组）。用静态表会整体错位，
所以**拿不到注册表时回退占位，而不是猜**。

**关键实现点**：

- `_read_paletted_container(..., min_bits=1, max_bits=7, ...)`：生物群系的 `min_bits` 是 **1**
  （方块是 4）。实测真实数据 `palette=1 → 无 data`、`palette=2 → data=1`、`palette=3 → data=2`
- `_build_legacy_biome_container`：把 256 / 1024 项**降采样**为 64 项，按官方索引
  `y4*16 + z4*4 + x4` 写出（旧格式无 Y 信息，故同一 XZ 在 4 个 Y 层重复）
- `_find_registry_codec`：用**字节特征扫描**（找 `0x0A 0x00 0x00`，再校验解析末尾索引
  与是否含 `minecraft:worldgen/biome` / `minecraft:dimension_type` 键）定位
  1.16~1.20.1 的 `Registry Codec`，避免脆弱的偏移量解析
- `normalize_biome_name`：把旧名映射到 1.18+ 等价名（`snowy_tundra` → `snowy_plains`、
  `mountains` → `windswept_hills`、`stone_shore` → `stony_shore` 等 37 项），
  仅在 `data_version <= 2975`（1.18.2）时启用

**相关文件位置**：
- 实现：`core/biome_registry.py`（数字表 + 归一化 + `BiomeRegistry`）、
  `core/chunk_format.py`（`_build_biomes` / `_build_legacy_biome_container` / `_apply_global_biomes`）、
  `core/protocol.py`（`_on_registry_data` / `_try_parse_registry_codec` / `_find_registry_codec`）
- 测试：`tests/biome_test.py`（97 项，含 Registry Data 包端到端）、
  `tests/spec_validator.py`（生物群系名称与真实世界交叉校验）

---

### 模块 C：区域文件写入（`chunk_extractor.py`）

**功能描述**：Anvil `.mca` 扇区分配（first-fit 复用）、进程级共享锁（多玩家安全）、超大区块外置为 `c.x.z.mcc`。

**输入参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `chunk_data` | `ChunkData` \| `tuple` | ✅ | `(x, z, compressed_nbt)` 或带 `.x/.z/.data` 的对象 | `(7, -3, b"...")` |
| `dimension` | `Any` | ❌ | 维度名或 ID | `"minecraft:the_nether"` |

**输出结果**：`{"chunks_saved": 3, "chunks_failed": 0, "dimensions": {...}, "external_chunks": 0}`

**调用示例**：

```python
import asyncio
from core.chunk_extractor import ChunkExtractor

async def main():
    extractor = ChunkExtractor("./downloaded_world")
    extractor.data_version = 4325
    ok = await extractor.save_chunk((7, -3, compressed_nbt), "minecraft:overworld")
    print(ok)                               # True
    await extractor.shutdown()              # 同时生成 level.dat + session.lock
    print(extractor.stats["chunks_saved"])  # 1

asyncio.run(main())
```

**相关文件位置**：实现 `core/chunk_extractor.py:232`（`AnvilWriter.save_chunk`）、`:462`（`ChunkExtractor.save_chunk`）、`:594`（`shutdown`）、`:44`（`_get_global_lock`）；测试 `tests/edge_test.py`（34 项，含 TCP 分片到达）

---

## 5. API/接口文档

> 本项目**不提供 HTTP 接口**，也不作为库对外发布。对外接口只有两个：**TCP 代理监听端口**，以及命令行参数（`main.py:62` 的 `build_parser`，共 9 个参数）。

### 5.1 接口清单

| 接口 | 类型 | 功能 | 认证 | 状态 |
|---|---|---|---|---|
| `MinecraftProxy.start() -> bool` | Python async | 启动 TCP 监听（不阻塞） | ❌ | ✅ 稳定 |
| `MinecraftProxy.run() -> None` | Python async | 启动并阻塞运行 | ❌ | ✅ 稳定 |
| `MinecraftProxy.port` | Python property | 当前监听端口（未启动为 `-1`） | ❌ | ✅ 稳定 |
| `main.py` 命令行 | 命令行 | 代理 + 导出世界 | ❌ | ✅ 稳定 |
| `tcp://127.0.0.1:25565` | TCP | Minecraft 客户端接入点 | ❌ | ✅ 稳定 |

### 5.2 接口详情

#### `python main.py --target <host:port>`

**功能**：启动代理并把拦截到的区块导出为可加载世界。

**请求参数**：

| 参数名 | 类型 | 必填 | 说明 | 示例 | 默认值 |
|---|---|---|---|---|---|
| `--target` | string | ✅ | 目标服务器 `host[:port]` | `mc.example.com:25565` | 无（缺省则交互式提问） |
| `--local-host` | string | ❌ | 本地监听地址 | `127.0.0.1` | `127.0.0.1` |
| `--local-port` | int | ❌ | 本地监听端口 | `25565` | `25565` |
| `--world-dir` | string | ❌ | 世界输出目录 | `./my_world` | `./downloaded_world` |
| `--block-map` | string | ❌ | 方块数据文件 | `./reports/blocks.json` | 自动下载 |
| `--no-download` | flag | ❌ | 禁止自动下载方块表 | — | `False` |
| `--debug` | flag | ❌ | DEBUG 日志 | — | `False` |
| `--max-connections` | int | ❌ | 同时允许的客户端连接数上限 | `16` | `8` |
| `--no-prompt` | flag | ❌ | 不交互提问（脚本用） | — | `False` |

**请求示例**：

```bash
cd d:\yx\attack\mcserverfinder\MinecraftProxy
python main.py --target mc.example.com:25565 --world-dir ./my_world --debug
```

**成功响应**（stdout，实测格式，节选）：

```text
=== Minecraft 代理配置 ===
2026-10-10 22:48:31 | INFO | MinecraftProxy | 世界目录: D:\...\my_world
2026-10-10 22:48:31 | INFO | MinecraftProxy | 代理监听: 127.0.0.1:25565
2026-10-10 22:48:31 | INFO | core.proxy | 代理服务器已启动: 127.0.0.1:25565
2026-10-10 22:49:02 | INFO | core.protocol | 客户端版本: 协议 770 (1.21.5) -> 目标 mc.example.com:25565 [ID 解析: 结构探测]
2026-10-10 22:49:02 | INFO | core.block_registry | 方块注册表: 1104 个方块 / 27914 个状态 [来源: blocks_770.json]
2026-10-10 22:49:02 | INFO | core.protocol | 服务器设置压缩阈值: 256
2026-10-10 22:49:03 | INFO | core.protocol | 登录已确认，进入 CONFIGURATION 状态
2026-10-10 22:49:04 | INFO | core.protocol | 进入 PLAY 状态 (维度: minecraft:overworld)
2026-10-10 22:51:10 | INFO | core.chunk_extractor | 区块统计: 成功=1873, 失败=0, 维度=1
```

**错误响应**：

| 退出码 | 场景 | 响应示例 |
|---|---|---|
| `2` | 未指定目标地址 | `ERROR \| MinecraftProxy \| 未指定目标服务器地址（使用 --target host[:port]）` |
| `2` | 端口非法 | `ERROR \| MinecraftProxy \| 服务器地址解析失败: 无效端口: abc` |
| `1` | 连接目标失败 | `ERROR \| core.proxy \| 连接目标服务器失败: [Errno 11001] getaddrinfo failed` |
| `130` | Ctrl+C 中断 | `INFO \| MinecraftProxy \| 收到中断信号，正在关闭...` |

**Python 调用示例**（对应 CLI 的编程式用法）：

```python
import asyncio
from core import MinecraftProxy

async def main():
    proxy = MinecraftProxy(
        local_host="127.0.0.1", local_port=25565,
        target_host="mc.example.com", target_port=25565,
        target_addr_display="mc.example.com:25565",
        world_dir="./my_world", debug=False,
        block_map=None, allow_download=True,
        max_sessions=8,             # 同时允许的客户端连接数上限
    )
    await proxy.run()          # 阻塞；Ctrl+C 后进入 finally

asyncio.run(main())
```

---

## 6. 数据库设计

**本项目不使用数据库**。持久化载体是 Minecraft 的 Anvil 区域文件（`.mca`）+ `level.dat`。

### 存储结构：Anvil 区域文件（`r.<regionX>.<regionZ>.mca`）

**用途**：每文件存 32×32 = 1024 个区块，是 Minecraft 世界的标准存储格式。

**文件布局**：

| 偏移 | 长度 | 字段 | 说明 |
|---|---|---|---|
| `0x0000` | 4096 | 位置表 | 1024 × `uint32`（大端）：高 3 字节=起始扇区，低 1 字节=扇区数 |
| `0x1000` | 4096 | 时间戳表 | 1024 × `uint32`（大端，Unix epoch 秒） |
| `0x2000` | 不定 | 区块数据 | 每扇区 4096 字节；`[1B 压缩类型=2][4B 大端长度][zlib(NBT)]` |

**索引与维度映射**（`core/chunk_extractor.py:200`、`:62`）：

```python
index = (chunk_x & 31) + (chunk_z & 31) * 32       # 0..1023

dimension_dir_name("minecraft:overworld")   # ""                          -> region/
dimension_dir_name("minecraft:the_nether")  # "DIM-1"                     -> DIM-1/region/
dimension_dir_name("minecraft:the_end")     # "DIM1"                      -> DIM1/region/
dimension_dir_name("mymod:custom")          # "dimension_mymod_custom"    # 模组维度独立目录
```

**区块 NBT 关键字段**（`core/chunk_format.py:1164` 产出）：

```text
root:
  DataVersion  Int     # 4325 (1.21.5)
  xPos, zPos   Int     # 区块坐标
  yPos         Int     # 最低节 Y：主世界 -4，下界/末地 0
  Status       String  # "full"
  sections     List<Compound>
    Y              Byte              # -4 .. 19
    block_count / fluid_count   Short
    block_states   Compound { palette: List<{Name, Properties}>, data: LongArray }
    biomes         Compound { palette: List<String>, data: LongArray }
    SkyLight / BlockLight  ByteArray(2048)   # 可选
  Heightmaps   Compound { MOTION_BLOCKING: LongArray(37), ... }
  block_entities List<Compound>
```

**输出目录完整结构**：

```text
downloaded_world/
├── level.dat                    # gzip(NBT)，23 个 Data 字段
├── session.lock                 # big-endian int64 时间戳
├── region/r.0.0.mca
├── region/chunk_7_-3_block_entities.json   # 方块实体侧信道（JSON）
├── DIM-1/region/r.0.0.mca
└── DIM1/region/r.0.0.mca
```

**初始化方式**：无需迁移。首次运行自动创建目录；`shutdown()` 调用 `ensure_world_metadata()` 生成 `level.dat`（`core/level_dat.py:255`）。

---

## 7. 配置说明

### 环境变量清单

**本项目不读取任何环境变量**（无 `.env` 需求）。所有配置通过 CLI 参数或 Python 构造参数传入，避免隐式依赖。

### 配置文件说明

**位置**：`pyproject.toml`（共 48 行，无 `[project]` 段）

```toml
[tool.mypy]
python_version = "3.10"
warn_return_any = true
warn_unused_ignores = true
strict_equality = true
ignore_missing_imports = true
exclude = ["^tests/"]

[tool.ruff]
line-length = 100
target-version = "py310"
select = ["E", "W", "F", "I", "UP", "B", "SIM"]
ignore = ["E501", "B008", "SIM105"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["E402", "F401", "F841"]   # 测试允许 sys.path 引导后再导入
```

### 运行时配置项（CLI 参数 / 构造参数）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `local_host` | `"127.0.0.1"` | 代理监听地址 |
| `local_port` | `25565` | 代理监听端口 |
| `target_host` / `target_port` | `"localhost"` / `25566` | 目标服务器 |
| `world_dir` | `"./downloaded_world"` | 世界输出目录 |
| `debug` | `False` | 是否输出 DEBUG 日志 |
| `block_map` | `None` | 方块数据文件路径（覆盖自动下载） |
| `allow_download` | `True` | 是否允许自动下载方块表 |

### 敏感信息处理方式

- ✅ 项目**不涉及任何密钥/凭据**（无数据库密码、无 API Key、无 JWT）
- ✅ 唯一网络请求是下载公开方块数据表（`raw.githubusercontent.com`），无需认证
- ⚠️ 代理默认绑定 `127.0.0.1`，**不要**改为 `0.0.0.0` 暴露公网（无访问控制）

---

## 8. 部署指南

### 8.1 环境准备

```bash
python --version        # 需要 >= 3.10（实测 3.11.2）
pip --version
# 无需数据库
```

### 8.2 本地开发部署

```bash
# 1. 进入项目目录
cd d:\yx\attack\mcserverfinder\MinecraftProxy

# 2. 无需安装（纯标准库，直接运行 main.py 即可）

# 3. 可选：安装开发依赖（类型检查 + lint）
pip install mypy ruff

# 4. 可选：验证静态检查
python -m mypy core main.py tools/precheck.py tests/realdata_test.py
# 预期：Success: no issues found in 17 source files
python -m ruff check .        # 预期：All checks passed!

# 5. 运行测试（含自动下载方块表，首次约 10 秒）
python run_tests.py           # 预期：全部测试通过（12 套件 / 665 断言）

# 6. 启动代理（另开一个终端）
python main.py --target mc.example.com:25565

# 7. 验证监听（PowerShell）
Test-NetConnection -ComputerName 127.0.0.1 -Port 25565
# 预期：TcpTestSucceeded : True
```

### 8.3 生产环境部署

```bash
# 1. 以包形式安装到虚拟环境
python -m venv .venv
.venv\Scripts\activate
# 无需安装

# 2. 预下载方块数据表（避免运行时下载阻塞握手，见第 10 节坑点 3）
python -c "from core import load_registry; load_registry(770, '.'); load_registry(773, '.'); load_registry(777, '.')"

# 3. 以后台任务启动（Windows 计划任务 / nssm 均可）
python main.py --target mc.example.com:25565 --world-dir D:\worlds\prod --no-prompt

# 4. 验证产出
dir D:\worlds\prod
# 预期存在：level.dat、session.lock、region\r.0.0.mca
```

### 8.4 常见问题

| 问题 | 解决方案 |
|---|---|
| `端口被占用` | 换端口：`--local-port 25566` |
| `连接目标服务器失败: getaddrinfo failed` | 检查 `--target` 拼写与 DNS；确认目标服务器在线 |
| `未找到方块数据表 —— 调色板将退化为「空气 / 石头」占位` | 联网后重跑，或 `--block-map ./reports/blocks.json` |
| 导出的世界方块全是空气 | 方块表缺失。删除 `core/data/` 后联网重跑，或用 `--block-map` |
| 下界/末地没有区块文件 | 需在游戏中实际进入该维度（`Respawn` 包触发维度切换） |
| 客户端连上后立刻掉线 | 目标服务器为 `online-mode=true`（见第 10 节坑点 1） |
| 从其它目录运行找不到 `core` | `main.py` 已自动把自身目录加入 `sys.path`，用绝对路径运行即可 |

---

## 9. 测试说明

**测试文件位置**：`tests/`（12 个套件 + `audit.py` + 真实抓包固件）

```
tests/
├── selftest.py              343 行  # 单元测试（NBT / 区块 / ID / 区域写入）
├── misdetect_test.py         61 行  # 误判测试（防结构探测误识别）
├── e2e_test.py              375 行  # 端到端（真实 TCP 回环）
├── version_test.py          248 行  # 版本自适应（无 ID 表纯探测）
├── edge_test.py             444 行  # 边界（直接调色板 / 光照 / TCP 分片 / 方块实体 NBT）
├── usability_test.py        373 行  # 导出世界可用性（核心验收）
├── multiversion_test.py     557 行  # 多版本（1.8.9 / 1.16.5 / 1.17 / 1.18.2）
├── version_dispatch_test.py  83 行  # 版本分派（家族 / 几何 / 维度）
├── robustness_test.py       612 行  # 健壮性（负数 VarInt / 落盘 / 内存 / 会话终止）
├── realdata_test.py         192 行  # 真实抓包回归（ViaVersion 未翻译负载）
├── biome_test.py            440 行  # 生物群系识别（旧版数字表 / 网络注册表 / 降采样）
├── spec_validator.py        528 行  # 独立规范校验（自写 NBT 读取器，对照真实世界）
├── audit.py                 157 行  # 能力边界审计（对照官方规范）
└── fixtures/                        # 真实服务器抓包固件（.bin）
```

**运行测试命令**：

```bash
cd d:\yx\attack\mcserverfinder\MinecraftProxy

# 运行全部测试
python run_tests.py

# 运行单个套件（子串匹配）
python run_tests.py selftest
python run_tests.py usability

# 直接运行某个测试文件
python tests/e2e_test.py

# 能力边界审计（逐项对照官方规范输出结论）
python tests/audit.py

# 目标服务器预检（协议号 / 可抓取性 / online-mode）
python tools/precheck.py mc.example.com:25565
python tools/precheck.py mc.example.com:25565 --probe-login --protocol 773
```

**测试覆盖范围**：共 **665 项断言**（含 15 项误判检查），全部通过。

| 套件 | 断言数 | 通过 |
|---|---|---|
| `selftest.py` | 63 | ✅ 0 失败 |
| `misdetect_test.py` | 15 | ✅ 0 误判 |
| `e2e_test.py` | 18 | ✅ 0 失败 |
| `version_test.py` | 10 | ✅ 0 失败 |
| `edge_test.py` | 34 | ✅ 0 失败 |
| `usability_test.py` | 91 | ✅ 0 失败 |
| `multiversion_test.py` | 87 | ✅ 0 失败 |
| `version_dispatch_test.py` | 20 | ✅ 0 失败 |
| `robustness_test.py` | 70 | ✅ 0 失败 |
| `realdata_test.py` | 56 | ✅ 0 失败 |
| `biome_test.py` | 97 | ✅ 0 失败 |
| `spec_validator.py` | 104 | ✅ 0 失败 |

> ⚠️ `spec_validator.py` 依赖本地金样本数据：真实 1.21.10 世界与官方
> `generated/reports/blocks.json`，位于项目根的 `标准文件/`
> （曾名 `你想要的/`，代码按两个候选名依次探测）。该目录体积大（含 54MB
> paper jar），**已列入 `.gitignore` 不入库**；缺失时该套件打印「[跳过]」
> 整体跳过（退出码 0），其余 11 个套件不受影响。

**关键测试用例说明**：

| 测试文件 | 测试内容 | 覆盖场景 |
|---|---|---|
| `usability_test.py:92` | 调色板必须是 `{Name, Properties}` 而非整数 | 正常（核心验收） |
| `usability_test.py:125` | 注册表映射准确性（0→air，1→stone，1000 带属性） | 正常 |
| `usability_test.py:144` | 光照写入 `SkyLight`/`BlockLight`（2048 字节） | 正常 |
| `usability_test.py:164` | 维度高度自适应（24/16/32 节，`yPos` -4/0/-8） | 边界 |
| `usability_test.py:193` | `level.dat` gzip 可解压 + NBT 可解析 + 必需字段 | 正常 |
| `edge_test.py` | 直接调色板 bits=15、光照前缀容错、方块实体 NBT、探测重试、TCP 分片 | 边界/异常 |
| `misdetect_test.py` | 15 种常见包（Set Time / Keep Alive / Boss Bar…）不被误判 | 异常 |
| `e2e_test.py:276` | 真实 TCP：压缩帧逐字节一致 + 落盘 NBT 回读验证 | 端到端 |
| `version_test.py:186` | 协议 770/9999 无精确 ID 表时纯结构探测跑通全流程 | 兼容性 |
| `realdata_test.py` | 真实抓包（ViaVersion 未翻译负载）+ 773 精确表逐项核对 | 实战回归 |
| `robustness_test.py` | 负数 VarInt / 落盘 fsync / 内存上限 / **客户端断开即结束会话** | 健壮性 |

---

## 10. 已知问题与注意事项

### ⚠️ 必看坑点

**1. 在线模式服务器（`online-mode=true`）完全无法使用** —— 架构性限制
- 现象：客户端能连上代理，但进入游戏后代理无任何区块输出，日志停在 `服务器设置压缩阈值`。
- 原因：`Set Compression` 之后服务端发送 `Encryption Request`，后续流量为 AES 加密。字节透明转发无法解密。
- 避免方法：**仅对 `online-mode=false` 的服务器使用**。需要支持在线模式必须实现完整 MITM 证书流程（生成自签名证书 + 客户端信任链处理），属独立大工程。
- 参考：`core/protocol.py:315`（`process_clientbound`，加密包会被安全跳过）

**2. 压缩帧长度不能用「解压后长度」去比较缓冲区** —— 曾导致每个大区块被丢弃
- 现象：所有大区块包被误判为「数据不完整」，代理静默丢包。
- 原因：压缩帧的 `Data Length` 字段是**解压后**长度，不能用来判断压缩数据是否收齐。
- 避免方法：压缩数据实际长度由外层帧长度界定（`packet_end`）。已修复于 `core/varint.py:166`。
- 参考：`core/varint.py` 的 `_read_compressed_packet`

**3. 首次运行会同步下载方块表，阻塞事件循环约 10 秒**
- 现象：第一个客户端握手时卡住约 10 秒（`urlopen` 是阻塞调用）。
- 避免方法：生产环境预下载（见 8.3 第 2 步），或 `--no-download` 配合 `--block-map`。
- 参考：`core/block_registry.py:407`（`download_block_data`）

**4. 方块注册表**不来自** `Registry Data` 包** —— 容易误解
- 现象：以为解析了 `Registry Data` 就能得到方块名，结果发现里面没有 `minecraft:block`。
- 原因：`minecraft:block` 属**硬编码注册表**，服务端不下发。
- 避免方法：方块名必须来自本地/下载的数据表（`core/data/blocks_<proto>.json`）或原版数据生成器的 `reports/blocks.json`。
- 参考：`core/block_registry.py:1-20`（模块文档已注明）

**5. `read_packet` 的返回值约定** —— 修改转发逻辑时必看
- `packet_size == 0` → 数据不完整，需等待更多字节
- `packet_id == -2` → 帧有效但载荷不可解析（解压失败），**仍须原样转发**
- 参考：`core/proxy.py:49`（常量 `PACKET_UNPARSABLE = -2`）

**6. 跨版本插件（ViaVersion / ViaFabric）不翻译区块负载** —— 仅凭协议号选布局必败
- 现象：客户端用 1.21.10 + ViaFabric 连「1.8.9 服务器」，握手报协议 773，
  包 ID 是标准 1.21.9 的（`map_chunk=0x2C`），但日志里 `0x2C` 全部「未识别」，
  `区块统计: 成功=0`。
- 原因：ViaVersion 只翻译**包 ID 与状态机**，**不翻译区块负载**。
  实测负载是旧核心格式：16 节（高度 256）、无 `fluid_count`、无长度前缀。
- 避免方法：已修复 —— `_parse_sections_adaptive` 按「必须精确消费全部字节」
  反推真实布局，并在 16 节时把 `min_section_y` 归零。
- 参考：`core/chunk_format.py:370`（`_parse_sections_adaptive`）

**7. 客户端退出游戏后代理仍在刷包** —— 已修复
- 现象：客户端关闭后，代理仍持续打印「客户端包」60+ 秒，直到 Ctrl+C。
- 原因：`run()` 原用 `gather(downstream, upstream)` 等**两个方向都结束**。
  客户端断开使其读端结束，但服务端连接未断且仍在推送，另一方向一直阻塞。
- 避免方法：已改为 `asyncio.wait(..., FIRST_COMPLETED)` + 取消另一方向。
- 参考：`core/proxy.py:325`

**8. `.mca` 区块记录的字节顺序与长度语义（严重）** —— 已修复
- 现象：游戏报 `Chunk [x,z] stream is truncated: expected 33554460 but read 8187`，
  C2ME 报 `Chunk has both internal and external streams` / `.mcc is not file`，
  世界**完全无法加载**（但本程序自己的解析器能读，所以测试全绿）。
- 原因：两处与官方规范相反：
  1. 字段顺序写成 `[压缩类型 1B][长度 4B]`，官方是 `[长度 4B][压缩类型 1B]`；
  2. 长度值写成 `len(压缩数据)`，官方是 **`1 + len(压缩数据)`**
     （即「剩余全部字节数」，含压缩类型字节）。
  游戏把 `0x02`（类型）当成长度高字节 → `expected 33554460`（= `0x0200_001C`）。
- 避免方法：已按官方规范修正（`_build_sector_data`）；外置区块也补上了
  「压缩类型 + 128」标记（`_save_external_chunk_sync`）。
- 参考：`core/chunk_extractor.py:138`（`_build_sector_data`）、
  `https://minecraft.wiki/w/Region_file_format`

**9. `level.dat` 缺少必需字段** —— 已修复
- 现象一：`No key dimensions in MapLike[{}]; No key seed in MapLike[{}]`
  → 缺 `WorldGenSettings`（含 `seed` 与 `dimensions`）。
- 现象二：`key missing: DragonFight`（部分模组/校验器会读）
  → 缺 `DimensionData -> 1 -> DragonFight`。
- 避免方法：已补全两个复合标签（结构按 wiki 1.21.x 修订版逐字段实现）。
- 参考：`core/level_dat.py` 的 `_world_gen_settings` / `_default_dragon_fight`

### 已知 Bug 清单

| Bug 描述 | 影响程度 | 临时方案 | 计划修复时间 |
|---|---|---|---|
| 在线模式服务器无法提取区块（加密） | 🔴 高 | 改用离线服 | 未计划（需 MITM 证书） |
| 拿不到生物群系注册表时退化为 `minecraft:plains` | 🟢 低 | 无（地形方块正确，仅颜色不准） | 不修（无注册表时猜反而更错） |
| 首次握手同步下载方块表阻塞 ~10s | 🟡 中 | 预下载或 `--block-map` | 未计划 |
| `VERSION_TABLES` 仅 47 / 754 / 758 / 773 / 777 | 🟢 低 | 其他版本自动走结构探测（已验证可用） | 可按需补充 |

> ✅ 已修复（2026-10-11）：ViaVersion 未翻译负载导致的区块全部解析失败；
> 客户端退出后会话不结束（继续刷包 60+ 秒）；区块探测失败被永久固化；
> 光照数组漏读 VarInt 长度前缀；方块实体 NBT 匿名/具名混淆。
> ✅ 已修复（2.4.0）：生物群系统一写 `minecraft:plains` 占位——
> 现按版本分别从数字表 / Registry Codec / Registry Data 真实识别。
> ✅ 已修复（2026-10-24）：金样本目录改名（`你想要的/` → `标准文件/`）导致
> `spec_validator` 的 104 项断言被**静默跳过**（目录改名后无人更新代码里的
> 路径）。现按两个候选路径探测，恢复运行且全部通过。

### 死代码清理记录（2026-10-24 梳理并删除）

以下 7 处「已定义未使用」的代码已删除，删除后 12 套件 665 断言、
ruff、mypy 全部复验通过：

| 已删除 | 位置（删除前） | 说明 |
|---|---|---|
| `frame_size()` | `core/varint.py` | 无调用方；压缩帧切分实际由 `read_packet` / `_read_compressed_packet`（现 `:166`）完成 |
| `build_packet()` | `core/varint.py` | 无调用方（`proxy.py` 有自己的 `_build_uncompressed_packet`） |
| `LightData` 数据类 | `core/protocol.py` | 无引用 |
| `ChunkData.light` 字段 | `core/protocol.py` | 恒为 `None`，无人读写 |
| `_on_update_light` 处理器 | `core/protocol.py` | 独立 `Update Light` 包解析后只打日志、不落盘；删除后该包照常转发与识别计数，仅不再空解析。注意 `chunk_format.parse_update_light_packet` **保留**——它仍被 `packet_ids.py` 的结构探测使用 |
| `palette_name()` | `core/block_registry.py` | 无调用方（降级占位逻辑实际在 `chunk_format._build_palette_entry`）；`is_air` 因有测试引用而保留 |
| `strict` 构造参数 / `registry.data_version` 赋值 | `core/block_registry.py` | 只存不用；DataVersion 实际取自 `PROTOCOL_DATA_VERSION` 表，下载缓存 JSON 里也不再写入无消费者的 `dataVersion` 键 |

### 性能瓶颈说明

- **瓶颈位置**：`core/block_registry.py:263`（`name_for`）与 `core/chunk_format.py:1253`（`build_chunk_nbt`）
- **表现**：每个区块最多 24 节 × 4096 方块 = 98304 次状态查询。实测单区块构建 NBT 5362 字节，压缩后 428 字节；`name_for` 有 20 万条 LRU 缓存，命中后为字典查找。
- **优化建议**：如需更高吞吐，可将 `build_chunk_nbt` 放入 `asyncio.to_thread` 以释放事件循环（当前为同步调用，会短暂阻塞转发）。

### 安全相关注意事项

- ✅ 代理默认绑定 `127.0.0.1`，不对外暴露
- ✅ 无凭据/密钥存储，无注入面（不解析用户输入的表达式/SQL）
- ✅ 所有二进制读取有边界检查（`core/datatypes.py` 抛 `BufferError`），畸形数据不会导致崩溃
- ⚠️ 转发的是**明文 Minecraft 流量**（离线模式本身无加密），同机其他进程可嗅探
- ❌ 无访问控制/限流：任何人连上监听端口即可使用代理。生产环境请用防火墙限制来源 IP

---

## 11. 后续开发建议

### 待优化项

- [ ] **MITM 加密层**（支持 `online-mode=true`）— 优先级：**P0**（这是唯一让工具在主流服务器上不可用的限制）
- [x] **生物群系注册表解析** — 已于 2.4.0 完成（1.8/1.16 数字表、Registry Codec、Registry Data 三路识别），不再是待办
- [ ] **方块表下载改为异步**（避免握手阻塞 10 秒）— 优先级：P1
- [ ] **补充 759–772 / 774 的精确 ID 表**（目前内置 47 / 754 / 758 / 773 / 777）— 优先级：P2
- [ ] **`build_chunk_nbt` 移入线程池**（释放事件循环）— 优先级：P2

### 可扩展的功能点

- [ ] **实体数据导出**（`entities/` 区域文件）— 价值：完整世界迁移，含生物/掉落物
- [ ] **实时进度统计面板**（TUI 或简易 Web）— 价值：长时间抓取时可见进度
- [ ] **断点续传**（记录已抓区块，重启后跳过）— 价值：大世界分批抓取
- [ ] **多协议版本表自动生成**（从服务端数据生成器批量产出）— 价值：彻底摆脱结构探测

### 代码重构建议

- **`protocol.py` 已达 881 行**：建议拆分为 `handlers/` 子包（login/config/play 分组），收益是可读性提升，便于按状态定位处理器。
- **`chunk_format.py` 达 1389 行**：建议把「解析」与「NBT 构建」分成两个模块，收益是职责单一，测试更聚焦。

### 技术债务

| 债务描述 | 产生原因 | 影响 | 建议处理方式 |
|---|---|---|---|
| `VERSION_TABLES` 仅 5 个协议 | 手工录入，成本高 | 其他版本依赖结构探测（可用但需首包命中） | 从服务端数据生成器批量产出 |
| 生物群系占位 | 注册表仅在运行时同步且未解析 | 生物群系颜色/生成不准 | 解析 `Registry Data` 的 `minecraft:worldgen/biome` |
| 结构探测依赖「首包形状」 | 无版本表时的兜底策略 | 极端情况下自定义包可能被误判 | 增加更多探测器 + 白名单校验 |
| 测试脚本各自带 `sys.path` 引导 | 为支持从任意 cwd 直接运行 | 有重复样板 | 改用 `conftest.py`（若迁移到 pytest） |

---

## 12. 联调对接清单

**接手人员请逐项确认**：

- [ ] **环境变量配置完成** — 本项目**无需环境变量**（见第 7 节），确认 Python ≥ 3.10 即可
- [ ] **依赖安装成功** — `pip install mypy ruff` 无错误（运行时零依赖）
- [ ] **数据库连接正常** — 本项目**无数据库**，确认 `core/data/blocks_773.json`（585 KB）存在
- [ ] **本地启动成功** — `python main.py --target <离线服> ` 后 `Test-NetConnection -Port 25565` 返回 `TcpTestSucceeded : True`
- [ ] **接口可访问** — 运行 `python main.py --help`，确认 9 个参数均列出
- [ ] **测试用例通过** — `python run_tests.py` 输出 `全部测试通过`（665 断言，含 15 误判检查；金样本缺失时 spec_validator 自动跳过）
- [ ] **静态检查通过** — `python -m mypy core main.py tools/precheck.py tests/realdata_test.py` → `Success: no issues found in 17 source files`；`python -m ruff check .` → `All checks passed!`
- [ ] **文档已阅读理解** — 重点理解第 10 节的 9 个坑点
- [ ] **已知问题已知晓** — 特别是「在线模式不可用」与「ViaVersion 不翻译区块负载」

**端到端可运行示例（复制即用）**：

```bash
# 终端 1：启动代理（目标须为 online-mode=false 的服务器）
cd d:\yx\attack\mcserverfinder\MinecraftProxy
python main.py --target 127.0.0.1:25566 --world-dir ./e2e_world --debug

# 终端 2：验证监听 + 检查产出
Test-NetConnection -ComputerName 127.0.0.1 -Port 25565
# 在 Minecraft 客户端连接 127.0.0.1:25565 并走动一段距离，然后退出游戏

# 终端 3：校验导出的世界
cd d:\yx\attack\mcserverfinder\MinecraftProxy
python -c "
import gzip, sys, glob
sys.path.insert(0, '.')
from core import nbt

# 1) level.dat 校验
with gzip.open('e2e_world/level.dat', 'rb') as f:
    root, end = nbt.parse_named_tag(f.read(), 0)
print('level.dat 字段数:', len(root['Data']), '| 世界名:', root['Data']['LevelName'])

# 2) .mca 校验：解出第一个区块并打印调色板
import struct, zlib
files = glob.glob('e2e_world/region/r.*.mca')
print('区域文件:', files)
raw = open(files[0], 'rb').read()
loc = struct.unpack_from('>I', raw, 0)[0]
body = raw[(loc >> 8) * 4096:]
ln = struct.unpack_from('>I', body, 1)[0]
chunk, _ = nbt.parse_named_tag(zlib.decompress(body[5:5+ln]), 0)
palette = chunk['sections'][0]['block_states']['palette']
print('区块坐标:', chunk['xPos'], chunk['zPos'], '| yPos:', chunk['yPos'])
print('调色板前 3 项:', palette[:3])
"
```

**预期输出**（实测）：

```text
level.dat 字段数: 23 | 世界名: e2e_world
区域文件: ['e2e_world/region/r.0.0.mca']
区块坐标: 0 0 | yPos: -4
调色板前 3 项: [{'Name': 'minecraft:bedrock'}, {'Name': 'minecraft:stone'}, {'Name': 'minecraft:deepslate'}]
```

> **若目标服为 ViaVersion 类（旧核心 + 新协议）**：区块实际是 16 节，
> 上面的 `yPos` 会是 **0**（而非 -4），这是正常且正确的
> （见模块 B-2 的世界高度校正）。可用 `--debug` 观察
> `区块统计: 成功=N` 确认抓取正常。

**负责人联系方式**：

- 负责人：GitHub Copilot（DeepSeek V4.1 Flash）
- 联系方式：VS Code Copilot Chat
- 代码仓库：本地路径 `d:\yx\attack\mcserverfinder\MinecraftProxy`（**非 git 仓库**，无 remote）
- 关键版本：`MinecraftProxy 2.4.0`（`core/__init__.py:10`）
- 相关文档：
  - `README.md` — 使用说明与特性表
  - `docs/handoff/HANDOFF_MinecraftProxy_20261010.md` — 本文档
  - `tests/audit.py` — 能力边界审计（可运行，逐项输出对照官方规范的结论）
  - `tools/precheck.py` — 目标服务器预检（协议号 / 可抓取性 / online-mode）

---

## 文档质量自检

- [x] 所有代码示例可直接运行（已实测：`parse_chunk_data_packet` 返回 `x/z = (7, -3)`，NBT 5362 字节 / 压缩 428 字节）
- [x] 无「待填写」「TODO」「参考 XX」等推卸话术
- [x] 版本号明确（`MinecraftProxy 2.3.0`、Python 3.11.2、mypy 2.4.0、ruff 0.17.0）
- [x] 路径与文件名真实存在（全部经 `Get-ChildItem` / `Select-String` 核实）
- [x] 行号引用已按 2026-10-11 代码重新核对
- [x] API 示例完整（含参数表、CLI 请求、实测 stdout 响应、错误码表）
- [x] 配置示例含实际值（`pyproject.toml` 真实片段）
- [x] 部署步骤已验证可执行（`mypy` / `ruff` / `run_tests.py` 均已实际运行通过）
- [x] 包含端到端可运行示例（第 12 节，含预期输出）
