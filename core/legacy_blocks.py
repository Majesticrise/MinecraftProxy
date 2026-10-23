"""
旧版（1.8 及更早）方块数字 ID -> 现代方块名称映射

背景
----
1.13 之前的区块数据用「数字方块 ID + 4 位 metadata」编码，而现代 Anvil 格式
要求调色板里是 **方块名称**（`Name` + `Properties`）。两者之间没有公式可换算，
必须有一张对照表。

本模块把 1.8 的 `(block_id, metadata)` 解析为现代方块名（`minecraft:xxx`），
再交给 `BlockStateRegistry.state_id_for_name()` 得到现代全局状态 ID，从而让
1.8 的区块能写进现代 NBT。

设计取舍
--------
* 数字 ID -> 名称：完整覆盖 1.8 的全部方块（数据取自 PrismarineJS
  minecraft-data `pc/1.8/blocks.json`，已人工核对）。
* metadata -> 变体：只覆盖真正会改变方块种类的方块（羊毛颜色、木板种类、
  原木种类、台阶种类等）。纯朝向/开关类 metadata（如楼梯、火把、红石中继器）
  不影响方块名称，直接忽略。
* 未知 ID：返回 None，由调用方降级为空气，保证文件始终可加载。
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# 数字 ID -> 现代方块名（metadata=0 的默认变体）
# --------------------------------------------------------------------------
LEGACY_BLOCK_NAMES: dict[int, str] = {
    0: "air",
    1: "stone",
    2: "grass_block",
    3: "dirt",
    4: "cobblestone",
    5: "oak_planks",
    6: "oak_sapling",
    7: "bedrock",
    8: "water",
    9: "water",
    10: "lava",
    11: "lava",
    12: "sand",
    13: "gravel",
    14: "gold_ore",
    15: "iron_ore",
    16: "coal_ore",
    17: "oak_log",
    18: "oak_leaves",
    19: "sponge",
    20: "glass",
    21: "lapis_ore",
    22: "lapis_block",
    23: "dispenser",
    24: "sandstone",
    25: "note_block",
    26: "red_bed",
    27: "powered_rail",
    28: "detector_rail",
    29: "sticky_piston",
    30: "cobweb",
    31: "dead_bush",
    32: "dead_bush",
    33: "piston",
    34: "piston_head",
    35: "white_wool",
    36: "piston_head",
    37: "dandelion",
    38: "poppy",
    39: "brown_mushroom",
    40: "red_mushroom",
    41: "gold_block",
    42: "iron_block",
    43: "stone_slab",
    44: "stone_slab",
    45: "bricks",
    46: "tnt",
    47: "bookshelf",
    48: "mossy_cobblestone",
    49: "obsidian",
    50: "torch",
    51: "fire",
    52: "spawner",
    53: "oak_stairs",
    54: "chest",
    55: "redstone_wire",
    56: "diamond_ore",
    57: "diamond_block",
    58: "crafting_table",
    59: "wheat",
    60: "farmland",
    61: "furnace",
    62: "furnace",
    63: "oak_sign",
    64: "oak_door",
    65: "ladder",
    66: "rail",
    67: "cobblestone_stairs",
    68: "oak_wall_sign",
    69: "lever",
    70: "stone_pressure_plate",
    71: "iron_door",
    72: "oak_pressure_plate",
    73: "redstone_ore",
    74: "redstone_ore",
    75: "redstone_torch",
    76: "redstone_torch",
    77: "stone_button",
    78: "snow",
    79: "ice",
    80: "snow_block",
    81: "cactus",
    82: "clay",
    83: "sugar_cane",
    84: "jukebox",
    85: "oak_fence",
    86: "carved_pumpkin",
    87: "netherrack",
    88: "soul_sand",
    89: "glowstone",
    90: "nether_portal",
    91: "jack_o_lantern",
    92: "cake",
    93: "repeater",
    94: "repeater",
    95: "white_stained_glass",
    96: "oak_trapdoor",
    97: "infested_stone",
    98: "stone_bricks",
    99: "brown_mushroom_block",
    100: "red_mushroom_block",
    101: "iron_bars",
    102: "glass_pane",
    103: "melon",
    104: "pumpkin_stem",
    105: "melon_stem",
    106: "vine",
    107: "oak_fence_gate",
    108: "brick_stairs",
    109: "stone_brick_stairs",
    110: "mycelium",
    111: "lily_pad",
    112: "nether_bricks",
    113: "nether_brick_fence",
    114: "nether_brick_stairs",
    115: "nether_wart",
    116: "enchanting_table",
    117: "brewing_stand",
    118: "cauldron",
    119: "end_portal",
    120: "end_portal_frame",
    121: "end_stone",
    122: "dragon_egg",
    123: "redstone_lamp",
    124: "redstone_lamp",
    125: "oak_slab",
    126: "oak_slab",
    127: "cocoa",
    128: "sandstone_stairs",
    129: "emerald_ore",
    130: "ender_chest",
    131: "tripwire_hook",
    132: "tripwire",
    133: "emerald_block",
    134: "spruce_stairs",
    135: "birch_stairs",
    136: "jungle_stairs",
    137: "command_block",
    138: "beacon",
    139: "cobblestone_wall",
    140: "flower_pot",
    141: "carrots",
    142: "potatoes",
    143: "oak_button",
    144: "skeleton_skull",
    145: "anvil",
    146: "trapped_chest",
    147: "light_weighted_pressure_plate",
    148: "heavy_weighted_pressure_plate",
    149: "comparator",
    150: "comparator",
    151: "daylight_detector",
    152: "redstone_block",
    153: "nether_quartz_ore",
    154: "hopper",
    155: "quartz_block",
    156: "quartz_stairs",
    157: "activator_rail",
    158: "dropper",
    159: "white_terracotta",
    160: "white_stained_glass_pane",
    161: "acacia_leaves",
    162: "acacia_log",
    163: "acacia_stairs",
    164: "dark_oak_stairs",
    165: "slime_block",
    166: "barrier",
    167: "iron_trapdoor",
    168: "prismarine",
    169: "sea_lantern",
    170: "hay_block",
    171: "white_carpet",
    172: "terracotta",
    173: "coal_block",
    174: "packed_ice",
    175: "sunflower",
    176: "white_banner",
    177: "white_wall_banner",
    178: "daylight_detector",
    179: "red_sandstone",
    180: "red_sandstone_stairs",
    181: "red_sandstone_slab",
    182: "red_sandstone_slab",
    183: "spruce_fence_gate",
    184: "birch_fence_gate",
    185: "jungle_fence_gate",
    186: "dark_oak_fence_gate",
    187: "acacia_fence_gate",
    188: "spruce_fence",
    189: "birch_fence",
    190: "jungle_fence",
    191: "dark_oak_fence",
    192: "acacia_fence",
    193: "spruce_door",
    194: "birch_door",
    195: "jungle_door",
    196: "acacia_door",
    197: "dark_oak_door",
}

# 16 种染料颜色后缀（metadata 0-15）
_DYE_COLORS = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink",
    "gray", "light_gray", "cyan", "purple", "blue", "brown", "green", "red",
    "black",
)

# 6 种木材后缀（metadata 0-5）
_WOODS = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak")


def _dyed(name: str) -> dict[int, str]:
    """生成「颜色_名称」形式的变体表（如 white_wool / red_carpet）"""
    return {i: f"{color}_{name}" for i, color in enumerate(_DYE_COLORS)}


def _wooded(name: str) -> dict[int, str]:
    """生成「木材_名称」形式的变体表（如 oak_planks / birch_slab）"""
    return {i: f"{wood}_{name}" for i, wood in enumerate(_WOODS)}


# --------------------------------------------------------------------------
# 会随 metadata 改变「方块种类」的方块
# --------------------------------------------------------------------------
LEGACY_VARIANTS: dict[int, dict[int, str]] = {
    # 石头变体
    1: {0: "stone", 1: "granite", 2: "polished_granite", 3: "diorite",
        4: "polished_diorite", 5: "andesite", 6: "polished_andesite"},
    # 泥土
    3: {0: "dirt", 1: "coarse_dirt", 2: "podzol"},
    # 木板
    5: _wooded("planks"),
    # 树苗
    6: {0: "oak_sapling", 1: "spruce_sapling", 2: "birch_sapling",
        3: "jungle_sapling", 4: "acacia_sapling", 5: "dark_oak_sapling"},
    # 沙子
    12: {0: "sand", 1: "red_sand"},
    # 原木（仅取树种，忽略朝向）
    17: {0: "oak_log", 1: "spruce_log", 2: "birch_log", 3: "jungle_log",
         4: "oak_log", 5: "spruce_log", 6: "birch_log", 7: "jungle_log",
         8: "oak_log", 9: "spruce_log", 10: "birch_log", 11: "jungle_log",
         12: "oak_wood", 13: "spruce_wood", 14: "birch_wood", 15: "jungle_wood"},
    # 树叶（取树种）
    18: {0: "oak_leaves", 1: "spruce_leaves", 2: "birch_leaves", 3: "jungle_leaves",
         4: "oak_leaves", 5: "spruce_leaves", 6: "birch_leaves", 7: "jungle_leaves",
         8: "oak_leaves", 9: "spruce_leaves", 10: "birch_leaves", 11: "jungle_leaves",
         12: "oak_leaves", 13: "spruce_leaves", 14: "birch_leaves", 15: "jungle_leaves"},
    # 海绵
    19: {0: "sponge", 1: "wet_sponge"},
    # 砂岩
    24: {0: "sandstone", 1: "chiseled_sandstone", 2: "cut_sandstone"},
    # 草（灌木 / 草 / 蕨）
    31: {0: "dead_bush", 1: "grass", 2: "fern"},
    # 羊毛
    35: _dyed("wool"),
    # 红砂岩
    179: {0: "red_sandstone", 1: "chiseled_red_sandstone", 2: "cut_red_sandstone"},
    # 石台阶（上半 / 双台阶同样按名称处理，朝向属性忽略）
    43: {0: "stone_slab", 1: "sandstone_slab", 2: "oak_slab", 3: "cobblestone_slab",
         4: "brick_slab", 5: "stone_brick_slab", 6: "nether_brick_slab",
         7: "quartz_slab", 8: "smooth_stone_slab", 9: "smooth_sandstone_slab",
         15: "smooth_quartz_slab"},
    44: {0: "stone_slab", 1: "sandstone_slab", 2: "oak_slab", 3: "cobblestone_slab",
         4: "brick_slab", 5: "stone_brick_slab", 6: "nether_brick_slab",
         7: "quartz_slab", 8: "stone_slab", 9: "sandstone_slab", 10: "oak_slab",
         11: "cobblestone_slab", 12: "brick_slab", 13: "stone_brick_slab",
         14: "nether_brick_slab", 15: "quartz_slab"},
    # 木台阶
    125: _wooded("slab"),
    126: {**{i: f"{w}_slab" for i, w in enumerate(_WOODS)},
          **{8 + i: f"{w}_slab" for i, w in enumerate(_WOODS)}},
    # 红砂岩台阶
    181: {0: "red_sandstone_slab", 8: "smooth_red_sandstone_slab"},
    182: {0: "red_sandstone_slab", 8: "red_sandstone_slab"},
    # 染色玻璃
    95: _dyed("stained_glass"),
    # 染色玻璃板
    160: _dyed("stained_glass_pane"),
    # 染色黏土（陶瓦）
    159: _dyed("terracotta"),
    # 地毯
    171: _dyed("carpet"),
    # 虫蚀方块
    97: {0: "infested_stone", 1: "infested_cobblestone", 2: "infested_stone_bricks",
         3: "infested_mossy_stone_bricks", 4: "infested_cracked_stone_bricks",
         5: "infested_chiseled_stone_bricks"},
    # 石砖
    98: {0: "stone_bricks", 1: "mossy_stone_bricks", 2: "cracked_stone_bricks",
         3: "chiseled_stone_bricks"},
    # 石英块
    155: {0: "quartz_block", 1: "chiseled_quartz_block", 2: "quartz_pillar",
          3: "quartz_pillar", 4: "quartz_pillar"},
    # 海晶石
    168: {0: "prismarine", 1: "prismarine_bricks", 2: "dark_prismarine"},
    # 圆石墙
    139: {0: "cobblestone_wall", 1: "mossy_cobblestone_wall"},
    # 两格高植物（取下半部分名称）
    175: {0: "sunflower", 1: "lilac", 2: "tall_grass", 3: "large_fern",
          4: "rose_bush", 5: "peony"},
    # 花（红）
    38: {0: "poppy", 1: "blue_orchid", 2: "allium", 3: "azure_bluet",
         4: "red_tulip", 5: "orange_tulip", 6: "white_tulip", 7: "pink_tulip",
         8: "oxeye_daisy"},
    # 第二类树叶（金合欢 / 深色橡木）
    161: {0: "acacia_leaves", 1: "dark_oak_leaves", 4: "acacia_leaves",
          5: "dark_oak_leaves", 8: "acacia_leaves", 9: "dark_oak_leaves",
          12: "acacia_leaves", 13: "dark_oak_leaves"},
    # 第二类原木
    162: {0: "acacia_log", 1: "dark_oak_log", 4: "acacia_log", 5: "dark_oak_log",
          8: "acacia_log", 9: "dark_oak_log", 12: "acacia_wood", 13: "dark_oak_wood"},
    # 南瓜
    86: {0: "carved_pumpkin", 1: "carved_pumpkin", 2: "carved_pumpkin",
         3: "carved_pumpkin", 4: "jack_o_lantern"},
    # 头颅
    144: {0: "skeleton_skull", 1: "wither_skeleton_skull", 2: "zombie_head",
          3: "player_head", 4: "creeper_head", 5: "dragon_head"},
    # 铁砧（仅损伤程度）
    145: {0: "anvil", 1: "chipped_anvil", 2: "damaged_anvil"},
}


def legacy_block_name(block_id: int, metadata: int = 0) -> str | None:
    """
    把 1.8 的 (block_id, metadata) 解析为现代方块名（不含 `minecraft:` 前缀）。

    无法识别时返回 None。
    """
    variants = LEGACY_VARIANTS.get(block_id)
    if variants is not None:
        name = variants.get(metadata & 0x0F)
        if name is not None:
            return name
    return LEGACY_BLOCK_NAMES.get(block_id)


def legacy_state_id(block_id: int, metadata: int, registry) -> int | None:
    """
    把 (block_id, metadata) 解析为现代全局方块状态 ID。

    `registry` 需为已加载的 `BlockStateRegistry`；解析失败返回 None。
    """
    name = legacy_block_name(block_id, metadata)
    if name is None:
        return None
    if registry is None or not getattr(registry, "loaded", False):
        return None
    result = registry.state_id_for_name(name)
    return int(result) if result is not None else None
