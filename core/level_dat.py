"""
level.dat 生成

为什么需要
----------
`.mca` 只是世界的一部分。Minecraft 要加载一个世界，还需要 `level.dat`
（世界元数据）与 `session.lock`。缺少它们时游戏不会把目录识别为世界。

本模块用默认值生成一个最小可用的 `level.dat`，让导出的目录能被
「选择世界」界面直接加载。世界高度等参数按实际抓取到的维度写入。

参考: https://minecraft.wiki/w/Java_Edition_level_format
"""

from __future__ import annotations

import gzip
import struct
import time
from pathlib import Path
from typing import Any

from . import nbt
from .block_registry import version_name_for_data_version
from .logger import setup_logger

logger = setup_logger(__name__)


# --------------------------------------------------------------------------
# 世界生成设置（WorldGenSettings）
# --------------------------------------------------------------------------
# 1.16+ 的 `level.dat` **必须**包含 `WorldGenSettings`，否则游戏加载时会抛
#   java.lang.IllegalStateException: No key dimensions in MapLike[{}];
#                                    No key seed in MapLike[{}]
# 并拒绝进入世界（`MapLike[{}]` 表示游戏读到了**空**的世界生成设置，
# 即 Data 里根本没有这个复合标签）。
#
# 结构参考 minecraft.wiki《Java Edition level format》的 generatorOptions 段：
#   WorldGenSettings:
#     seed              Long    世界种子
#     generate_features Byte    是否生成结构（村庄/神殿等）
#     bonus_chest       Byte    是否生成奖励箱
#     dimensions        Compound 维度 ID -> { type, generator }
#       type            String  维度类型（注册表 ID）
#       generator       Compound 区块生成器
#         type          String  "minecraft:noise"
#         biome_source  Compound { type, preset }
#         settings      String  噪声设置 ID
#
# 注意：`seed` 只需与原版**同量级**（64 位有符号）。这里写死一个固定值，
# 保证导出的世界可重复且能正常加载；地形本身来自抓取到的区块，
# 生成器只在**未抓取区域**（走出抓取范围时）生效。
DEFAULT_WORLD_SEED = 0


def _vanilla_dimensions() -> dict[str, Any]:
    """三个原版维度的生成器定义（1.16 ~ 1.21 结构一致）"""
    return {
        "minecraft:overworld": nbt.compound({
            "type": nbt.String("minecraft:overworld"),
            "generator": nbt.compound({
                "type": nbt.String("minecraft:noise"),
                "biome_source": nbt.compound({
                    "type": nbt.String("minecraft:multi_noise"),
                    "preset": nbt.String("minecraft:overworld"),
                }),
                "settings": nbt.String("minecraft:overworld"),
            }),
        }),
        "minecraft:the_nether": nbt.compound({
            "type": nbt.String("minecraft:the_nether"),
            "generator": nbt.compound({
                "type": nbt.String("minecraft:noise"),
                "biome_source": nbt.compound({
                    "type": nbt.String("minecraft:multi_noise"),
                    "preset": nbt.String("minecraft:nether"),
                }),
                "settings": nbt.String("minecraft:nether"),
            }),
        }),
        "minecraft:the_end": nbt.compound({
            "type": nbt.String("minecraft:the_end"),
            "generator": nbt.compound({
                "type": nbt.String("minecraft:noise"),
                "biome_source": nbt.compound({
                    "type": nbt.String("minecraft:the_end"),
                }),
                "settings": nbt.String("minecraft:end"),
            }),
        }),
    }


def _world_gen_settings(seed: int) -> Any:
    """构建 WorldGenSettings 复合标签（缺失它游戏会拒绝加载）"""
    return nbt.compound({
        "seed": nbt.Long(seed),
        "generate_features": nbt.Byte(1),
        "bonus_chest": nbt.Byte(0),
        "dimensions": nbt.compound(_vanilla_dimensions()),
    })


def _default_dragon_fight() -> Any:
    """
    末地龙战斗的「初始（未触发）」状态。

    位置：**`Data` 顶层**，与 `WorldGenSettings` 同级（**不是**
    `DimensionData -> 1 -> DragonFight`）。

    依据：真实 1.21.10 世界的 `level.dat`（Paper 数据生成器产出）实测结构为
    `Data.DragonFight = {DragonKilled, PreviouslyKilled, NeedsStateScanning, Gateways}`，
    且该文件**不存在** `DimensionData` 键。

    字段：
      * `DragonKilled`：龙是否已被击杀（0 = 尚未击杀）
      * `PreviouslyKilled`：是否击杀过（0 = 从未击杀，首次进入会生成龙）
      * `NeedsStateScanning`：是否需要重新扫描战斗状态
      * `Gateways`：尚未生成的末地传送门索引列表。官方在首次生成末地时
        写入 20 个 0..19 的乱序索引（即全部传送门都还没生成）；
        空表会让部分实现认为「无可生成传送门」。
    """
    return nbt.compound({
        "DragonKilled": nbt.Byte(0),
        "PreviouslyKilled": nbt.Byte(0),
        "NeedsStateScanning": nbt.Byte(1),
        # 20 个传送门索引（0..19），与官方首次生成末地时的状态一致
        "Gateways": nbt.List(nbt.TAG_INT, [nbt.Int(i) for i in range(20)]),
    })


def build_level_dat(
    world_name: str = "downloaded_world",
    data_version: int = 3955,
    spawn_x: int = 0,
    spawn_y: int = 64,
    spawn_z: int = 0,
    last_played: int | None = None,
    seed: int = DEFAULT_WORLD_SEED,
) -> bytes:
    """
    构建 level.dat 的字节内容（未压缩）。

    采用 1.16+ 的结构：根标签含 `Data`，`Data` 内含 `LevelName`、
    `DataVersion`、`Version`、`SpawnX/Y/Z`、`GameType` 等必需字段，
    以及 **`WorldGenSettings`**（含 `seed` 与 `dimensions`，缺失会导致
    游戏报 `No key dimensions / No key seed` 并拒绝加载）。
    `Version.Name` 由 `data_version` 推导，保证与 `DataVersion` 一致。
    """
    now = last_played if last_played is not None else int(time.time() * 1000)
    game_version = version_name_for_data_version(data_version)

    data = nbt.compound({
        "DataVersion": nbt.Int(data_version),
        # Version 段帮助游戏判断是否做数据升级
        "Version": nbt.compound({
            "Id": nbt.Int(data_version),
            "Name": nbt.String(game_version),
            "Series": nbt.String("main"),
            "Snapshot": nbt.Byte(0),
        }),
        "LevelName": nbt.String(world_name),
        "GameType": nbt.Int(1),          # 创造模式，便于查看
        "hardcore": nbt.Byte(0),
        "Difficulty": nbt.Byte(1),
        "DifficultyLocked": nbt.Byte(0),
        "allowCommands": nbt.Byte(1),
        "initialized": nbt.Byte(1),
        "SpawnX": nbt.Int(spawn_x),
        "SpawnY": nbt.Int(spawn_y),
        "SpawnZ": nbt.Int(spawn_z),
        "Time": nbt.Long(0),
        "DayTime": nbt.Long(6000),
        "LastPlayed": nbt.Long(now),
        "version": nbt.Int(19133),       # 旧字段，部分工具会读
        "clearWeatherTime": nbt.Int(0),
        "raining": nbt.Byte(0),
        "thundering": nbt.Byte(0),
        "GameRules": nbt.compound({
            "doDaylightCycle": nbt.String("false"),
            "doMobSpawning": nbt.String("false"),
            "doWeatherCycle": nbt.String("false"),
            "keepInventory": nbt.String("true"),
        }),
        "DataPacks": nbt.compound({
            "Enabled": nbt.List(nbt.TAG_STRING, [nbt.String("vanilla")]),
            "Disabled": nbt.List(nbt.TAG_STRING, []),
        }),
        "ServerBrands": nbt.List(nbt.TAG_STRING, [nbt.String("MinecraftProxy")]),
        "WasModded": nbt.Byte(0),
        # 世界生成设置：**必需**，否则游戏拒绝加载（见上方注释）
        "WorldGenSettings": _world_gen_settings(seed),
        # 末地龙的战斗状态。
        #
        # ⚠️ 位置：**`Data` 顶层**（与 `WorldGenSettings` 同级），
        #    **不是** `DimensionData -> 1 -> DragonFight`。
        #
        # 依据：真实 1.21.10 世界（Paper 数据生成器产出）的 `level.dat`
        # 实测把 `DragonFight` 放在 `Data` 顶层，且**没有** `DimensionData`。
        # 此前误放进 `DimensionData -> 1` 导致游戏报
        # `key missing: DragonFight`（PCL/C2ME 的完整性检查读顶层键）。
        "DragonFight": _default_dragon_fight(),
    })

    return nbt.build_root({"Data": data}, "")


def write_level_dat(
    world_dir: str,
    world_name: str = "downloaded_world",
    data_version: int = 3955,
    overwrite: bool = False,
    spawn: tuple[int, int, int] | None = None,
) -> bool:
    """
    把 level.dat 写入世界目录（gzip 压缩，与官方一致）。

    默认不覆盖已存在的文件，避免破坏用户已经修好的世界。
    spawn 为玩家最后位置：导出的世界出生在「你退出时的位置」而不是 (0,64,0)。
    """
    path = Path(world_dir) / "level.dat"
    if path.exists() and not overwrite:
        logger.debug(f"level.dat 已存在，跳过: {path}")
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        spawn_x, spawn_y, spawn_z = spawn if spawn is not None else (0, 64, 0)
        raw = build_level_dat(
            world_name=world_name, data_version=data_version,
            spawn_x=spawn_x, spawn_y=spawn_y, spawn_z=spawn_z,
        )
        with gzip.open(path, "wb") as fh:
            fh.write(raw)
        logger.info(f"已生成 level.dat: {path}（出生点 {spawn_x},{spawn_y},{spawn_z}）")
        return True
    except Exception as exc:
        logger.error(f"写入 level.dat 失败: {exc}")
        return False


def write_session_lock(world_dir: str) -> bool:
    """
    写入 session.lock。

    内容是一个 big-endian 的时间戳；游戏用它检测世界是否被占用。
    只是为了让目录看起来像完整世界，不参与真正的锁语义。
    """
    path = Path(world_dir) / "session.lock"
    if path.exists():
        return True
    try:
        with open(path, "wb") as fh:
            fh.write(struct.pack(">q", int(time.time() * 1000)))
        return True
    except Exception as exc:
        logger.debug(f"写入 session.lock 失败: {exc}")
        return False


def ensure_world_metadata(
    world_dir: str,
    world_name: str = "downloaded_world",
    data_version: int = 3955,
    spawn: tuple[int, int, int] | None = None,
) -> None:
    """确保世界目录具备 level.dat / session.lock 等最小元数据"""
    Path(world_dir).mkdir(parents=True, exist_ok=True)
    write_level_dat(world_dir, world_name=world_name, data_version=data_version, spawn=spawn)
    write_session_lock(world_dir)
