"""
生物群系注册表（ID -> 资源位置）。

为什么需要它
------------
区块里的生物群系与方块一样，存的是**数字 ID**（1.18+ 是每节的调色板索引，
1.16/1.17 是全局 1024 项数组，1.8 是 256 字节的旧数字 ID），
而 Anvil 的 `biomes.palette` 需要**资源位置字符串**（如 `minecraft:plains`）。
因此必须有一张 ID -> 名称的表，否则只能写占位值（草地/水色全是平原绿）。

三个来源
--------
1. **1.18+**：注册表是**动态**的，服务端在配置阶段用 `Registry Data`
   （`minecraft:worldgen/biome`）下发，**列表顺序即 ID**。必须从线路抓取
   ——原版 65 项，模组服可能上百项（实测某小游戏服 174 项），
   静态表会错位，因此**不做静态回退**（宁可回退成占位值，也不能写错名字）。
2. **1.16 ~ 1.17**（协议 735~756）：注册表随 `Join Game` 的
   `Registry Codec`（NBT）下发，同样是列表顺序即 ID。
3. **1.8 ~ 1.15**（协议 ≤47 及 107~578）：ID 是**硬编码在游戏里**的数字，
   直接用内置表。1.8 是 0~167（含 128~167 的 M 变体），1.16 是 0~173。

数据来源
--------
* 1.8 / 1.16 数字 ID 表：PrismarineJS `minecraft-data`
  `data/pc/1.8/biomes.json`、`data/pc/1.16.1/biomes.json`
  （与 minecraft.wiki《Biome/ID》一致）
* 1.18+ 网络注册表：本程序运行时从 `Registry Data` 包读取
"""

from __future__ import annotations

from typing import Any

# 无法解析生物群系时使用的占位值。
# 地形方块不受影响，仅草地/树叶/水的颜色不准确。
PLACEHOLDER_BIOME = "minecraft:plains"

# ---------------------------------------------------------------------------
# 1.8（协议 47）数字 ID -> 群系名
#
# 61 项，id 范围 0~167（40~127 与 135~139 为空洞，128~167 是 M 变体）。
# 数据来源: PrismarineJS minecraft-data data/pc/1.8/biomes.json
# ---------------------------------------------------------------------------
LEGACY_BIOMES_1_8: dict[int, str] = {
    0: "minecraft:ocean",
    1: "minecraft:plains",
    2: "minecraft:desert",
    3: "minecraft:mountains",
    4: "minecraft:forest",
    5: "minecraft:taiga",
    6: "minecraft:swamp",
    7: "minecraft:river",
    8: "minecraft:nether_wastes",
    9: "minecraft:the_end",
    10: "minecraft:frozen_ocean",
    11: "minecraft:frozen_river",
    12: "minecraft:snowy_tundra",
    13: "minecraft:snowy_mountains",
    14: "minecraft:mushroom_fields",
    15: "minecraft:mushroom_field_shore",
    16: "minecraft:beach",
    17: "minecraft:desert_hills",
    18: "minecraft:wooded_hills",
    19: "minecraft:taiga_hills",
    20: "minecraft:mountain_edge",
    21: "minecraft:jungle",
    22: "minecraft:jungle_hills",
    23: "minecraft:jungle_edge",
    24: "minecraft:deep_ocean",
    25: "minecraft:stone_shore",
    26: "minecraft:snowy_beach",
    27: "minecraft:birch_forest",
    28: "minecraft:birch_forest_hills",
    29: "minecraft:dark_forest",
    30: "minecraft:snowy_taiga",
    31: "minecraft:snowy_taiga_hills",
    32: "minecraft:giant_tree_taiga",
    33: "minecraft:giant_tree_taiga_hills",
    34: "minecraft:wooded_mountains",
    35: "minecraft:savanna",
    36: "minecraft:savanna_plateau",
    37: "minecraft:badlands",
    38: "minecraft:wooded_badlands_plateau",
    39: "minecraft:badlands_plateau",
    129: "minecraft:sunflower_plains",
    130: "minecraft:desert_lakes",
    131: "minecraft:gravelly_mountains",
    132: "minecraft:flower_forest",
    133: "minecraft:taiga_mountains",
    134: "minecraft:swamp_hills",
    140: "minecraft:ice_spikes",
    149: "minecraft:modified_jungle",
    151: "minecraft:modified_jungle_edge",
    155: "minecraft:tall_birch_forest",
    156: "minecraft:tall_birch_hills",
    157: "minecraft:dark_forest_hills",
    158: "minecraft:snowy_taiga_mountains",
    160: "minecraft:giant_spruce_taiga",
    161: "minecraft:giant_spruce_taiga_hills",
    162: "minecraft:modified_gravelly_mountains",
    163: "minecraft:shattered_savanna",
    164: "minecraft:shattered_savanna_plateau",
    165: "minecraft:eroded_badlands",
    166: "minecraft:modified_wooded_badlands_plateau",
    167: "minecraft:modified_badlands_plateau",
}

# ---------------------------------------------------------------------------
# 1.16 ~ 1.17（协议 735 ~ 756）数字 ID -> 群系名
#
# 79 项，id 范围 0~173。比 1.8 多出 40~50（末地与海洋变体）、127（the_void）、
# 168~173（竹子丛林 / 下界三个群系 / 玄武岩三角洲）。
# 数据来源: PrismarineJS minecraft-data data/pc/1.16.1/biomes.json
# ---------------------------------------------------------------------------
LEGACY_BIOMES_1_16: dict[int, str] = {
    0: "minecraft:ocean",
    1: "minecraft:plains",
    2: "minecraft:desert",
    3: "minecraft:mountains",
    4: "minecraft:forest",
    5: "minecraft:taiga",
    6: "minecraft:swamp",
    7: "minecraft:river",
    8: "minecraft:nether_wastes",
    9: "minecraft:the_end",
    10: "minecraft:frozen_ocean",
    11: "minecraft:frozen_river",
    12: "minecraft:snowy_tundra",
    13: "minecraft:snowy_mountains",
    14: "minecraft:mushroom_fields",
    15: "minecraft:mushroom_field_shore",
    16: "minecraft:beach",
    17: "minecraft:desert_hills",
    18: "minecraft:wooded_hills",
    19: "minecraft:taiga_hills",
    20: "minecraft:mountain_edge",
    21: "minecraft:jungle",
    22: "minecraft:jungle_hills",
    23: "minecraft:jungle_edge",
    24: "minecraft:deep_ocean",
    25: "minecraft:stone_shore",
    26: "minecraft:snowy_beach",
    27: "minecraft:birch_forest",
    28: "minecraft:birch_forest_hills",
    29: "minecraft:dark_forest",
    30: "minecraft:snowy_taiga",
    31: "minecraft:snowy_taiga_hills",
    32: "minecraft:giant_tree_taiga",
    33: "minecraft:giant_tree_taiga_hills",
    34: "minecraft:wooded_mountains",
    35: "minecraft:savanna",
    36: "minecraft:savanna_plateau",
    37: "minecraft:badlands",
    38: "minecraft:wooded_badlands_plateau",
    39: "minecraft:badlands_plateau",
    40: "minecraft:small_end_islands",
    41: "minecraft:end_midlands",
    42: "minecraft:end_highlands",
    43: "minecraft:end_barrens",
    44: "minecraft:warm_ocean",
    45: "minecraft:lukewarm_ocean",
    46: "minecraft:cold_ocean",
    47: "minecraft:deep_warm_ocean",
    48: "minecraft:deep_lukewarm_ocean",
    49: "minecraft:deep_cold_ocean",
    50: "minecraft:deep_frozen_ocean",
    127: "minecraft:the_void",
    129: "minecraft:sunflower_plains",
    130: "minecraft:desert_lakes",
    131: "minecraft:gravelly_mountains",
    132: "minecraft:flower_forest",
    133: "minecraft:taiga_mountains",
    134: "minecraft:swamp_hills",
    140: "minecraft:ice_spikes",
    149: "minecraft:modified_jungle",
    151: "minecraft:modified_jungle_edge",
    155: "minecraft:tall_birch_forest",
    156: "minecraft:tall_birch_hills",
    157: "minecraft:dark_forest_hills",
    158: "minecraft:snowy_taiga_mountains",
    160: "minecraft:giant_spruce_taiga",
    161: "minecraft:giant_spruce_taiga_hills",
    162: "minecraft:modified_gravelly_mountains",
    163: "minecraft:shattered_savanna",
    164: "minecraft:shattered_savanna_plateau",
    165: "minecraft:eroded_badlands",
    166: "minecraft:modified_wooded_badlands_plateau",
    167: "minecraft:modified_badlands_plateau",
    168: "minecraft:bamboo_jungle",
    169: "minecraft:bamboo_jungle_hills",
    170: "minecraft:soul_sand_valley",
    171: "minecraft:crimson_forest",
    172: "minecraft:warped_forest",
    173: "minecraft:basalt_deltas",
}

# 这些名字在 1.18 被重命名；归一化输出（DataVersion 2975）时顺手转换，
# 让导出的世界在 1.18.2 下也能正确显示。
_BIOME_RENAMES_1_18: dict[str, str] = {
    "minecraft:snowy_tundra": "minecraft:snowy_plains",
    "minecraft:snowy_mountains": "minecraft:snowy_plains",
    "minecraft:mountains": "minecraft:windswept_hills",
    "minecraft:wooded_mountains": "minecraft:windswept_forest",
    "minecraft:gravelly_mountains": "minecraft:windswept_gravelly_hills",
    "minecraft:modified_gravelly_mountains": "minecraft:windswept_gravelly_hills",
    "minecraft:mountain_edge": "minecraft:windswept_hills",
    "minecraft:stone_shore": "minecraft:stony_shore",
    "minecraft:jungle_edge": "minecraft:sparse_jungle",
    "minecraft:modified_jungle_edge": "minecraft:sparse_jungle",
    "minecraft:modified_jungle": "minecraft:jungle",
    "minecraft:jungle_hills": "minecraft:jungle",
    "minecraft:giant_tree_taiga": "minecraft:old_growth_pine_taiga",
    "minecraft:giant_tree_taiga_hills": "minecraft:old_growth_pine_taiga",
    "minecraft:giant_spruce_taiga": "minecraft:old_growth_spruce_taiga",
    "minecraft:giant_spruce_taiga_hills": "minecraft:old_growth_spruce_taiga",
    "minecraft:tall_birch_forest": "minecraft:old_growth_birch_forest",
    "minecraft:tall_birch_hills": "minecraft:old_growth_birch_forest",
    "minecraft:birch_forest_hills": "minecraft:birch_forest",
    "minecraft:dark_forest_hills": "minecraft:dark_forest",
    "minecraft:snowy_taiga_mountains": "minecraft:snowy_taiga",
    "minecraft:snowy_taiga_hills": "minecraft:snowy_taiga",
    "minecraft:taiga_mountains": "minecraft:taiga",
    "minecraft:taiga_hills": "minecraft:taiga",
    "minecraft:swamp_hills": "minecraft:swamp",
    "minecraft:desert_lakes": "minecraft:desert",
    "minecraft:desert_hills": "minecraft:desert",
    "minecraft:wooded_hills": "minecraft:forest",
    "minecraft:shattered_savanna": "minecraft:windswept_savanna",
    "minecraft:shattered_savanna_plateau": "minecraft:windswept_savanna",
    "minecraft:wooded_badlands_plateau": "minecraft:wooded_badlands",
    "minecraft:badlands_plateau": "minecraft:badlands",
    "minecraft:modified_wooded_badlands_plateau": "minecraft:wooded_badlands",
    "minecraft:modified_badlands_plateau": "minecraft:badlands",
    "minecraft:deep_warm_ocean": "minecraft:warm_ocean",
    "minecraft:mushroom_field_shore": "minecraft:mushroom_fields",
    "minecraft:bamboo_jungle_hills": "minecraft:bamboo_jungle",
}


def normalize_biome_name(name: str) -> str:
    """把旧版群系名映射到 1.18+ 的等价名（未知名原样返回）"""
    return _BIOME_RENAMES_1_18.get(name, name)


class BiomeRegistry:
    """
    生物群系 ID 查询表。

    两种模式（互斥）：
      * **names**：1.18+ 网络注册表，`list[str]`，下标即 ID
      * **table**：旧版数字 ID 表，`dict[int, str]`

    未加载时 `name_for` 返回占位值。
    """

    __slots__ = ("_names", "_table", "source")

    def __init__(self) -> None:
        self._names: list[str] | None = None
        self._table: dict[int, str] | None = None
        self.source: str = "空（未加载）"

    # ------------------------------------------------------------------ 属性
    @property
    def loaded(self) -> bool:
        return bool(self._names) or bool(self._table)

    @property
    def count(self) -> int:
        if self._names:
            return len(self._names)
        if self._table:
            return len(self._table)
        return 0

    @property
    def is_network(self) -> bool:
        """是否为 1.18+ 的网络注册表（列表式）"""
        return self._names is not None

    # ------------------------------------------------------------------ 装载
    def set_names(self, names: list[str], source: str = "网络注册表") -> None:
        """装载 1.18+ 的列表式注册表（下标即 ID）"""
        if not names:
            return
        self._names = [str(n) for n in names]
        self._table = None
        self.source = f"{source}（{len(self._names)} 项）"

    def set_table(self, table: dict[int, str], source: str = "内置数字 ID 表") -> None:
        """装载旧版数字 ID 表"""
        if not table:
            return
        self._table = dict(table)
        self._names = None
        self.source = f"{source}（{len(self._table)} 项）"

    # ------------------------------------------------------------------ 查询
    def name_for(self, biome_id: Any, fallback: str = PLACEHOLDER_BIOME) -> str:
        """把生物群系 ID 转成资源位置；无法解析时返回占位值"""
        try:
            bid = int(biome_id)
        except (TypeError, ValueError):
            return fallback
        if self._table is not None:
            return self._table.get(bid, fallback)
        if self._names is not None and 0 <= bid < len(self._names):
            return self._names[bid]
        return fallback

    def describe(self) -> str:
        if not self.loaded:
            return f"生物群系注册表: 未加载（{self.source}）"
        return f"生物群系注册表: {self.source}"


def legacy_biome_name(biome_id: Any, fallback: str = PLACEHOLDER_BIOME) -> str:
    """1.8 数字 ID -> 群系名"""
    try:
        return LEGACY_BIOMES_1_8.get(int(biome_id), fallback)
    except (TypeError, ValueError):
        return fallback


def legacy_biome_name_1_16(biome_id: Any, fallback: str = PLACEHOLDER_BIOME) -> str:
    """1.16 / 1.17 数字 ID -> 群系名"""
    try:
        return LEGACY_BIOMES_1_16.get(int(biome_id), fallback)
    except (TypeError, ValueError):
        return fallback


def build_biome_registry(protocol_version: int | None) -> BiomeRegistry:
    """
    为**旧版**协议构造内置注册表；1.18+ 返回空表（等待网络注册表）。

    `protocol_version` 为 None 时返回空表（由网络注册表填充）。
    """
    registry = BiomeRegistry()
    if protocol_version is None:
        return registry
    if protocol_version <= 47:
        registry.set_table(LEGACY_BIOMES_1_8, "1.8 内置数字 ID 表")
    elif 735 <= protocol_version <= 756:
        registry.set_table(LEGACY_BIOMES_1_16, "1.16/1.17 内置数字 ID 表")
    return registry
