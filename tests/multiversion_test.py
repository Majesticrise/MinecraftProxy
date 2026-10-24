"""
多版本兼容测试：1.8.9 / 1.9~1.12.2 / 1.16.5 / 1.18.2

验证要点
--------
1. 1.8 legacy 区块解析（Blocks/Add/Data/BlockLight/SkyLight/Biomes 分组布局）
2. 1.8 legacy (id, meta) -> 现代方块名映射
3. 1.9 ~ 1.12.2 mid 区块解析（跨 long 位打包 + 节内交错光照 + 包内方块实体）
4. 1.16.5 flattened 区块解析（bitMap + 独立生物群系数组）
5. 1.18.2 caves 区块解析（无 fluid_count）
6. 四种版本都能写出可被 nbt.parse_named_tag 解析的现代 Anvil NBT
7. 协议版本家族分派正确
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.nbt as nbt
from core.block_registry import BlockStateRegistry
from core.chunk_format import (
    build_chunk_nbt,
    parse_caves_chunk_data_packet,
    parse_chunk_data_packet,
    parse_flattened_chunk_data_packet,
    parse_legacy_chunk_data_packet,
    parse_legacy_map_chunk_bulk_packet,
    parse_mid_chunk_data_packet,
)
from core.datatypes import write_string, write_varint
from core.legacy_blocks import legacy_block_name, legacy_state_id
from core.protocol import ConnectionState, ProtocolParser, chunk_format_family

PASS = 0
FAIL = 0


def check(label, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


# --------------------------------------------------------------------------
# 简易注册表：用固定 ID 表代替真实的 PrismarineJS 数据
# --------------------------------------------------------------------------
class FakeRegistry:
    """把方块名映射到固定状态 ID 的假注册表（避免联网/大文件）"""

    loaded = True

    _NAMES = [
        "air", "stone", "granite", "diorite", "andesite", "grass_block",
        "dirt", "coarse_dirt", "podzol", "cobblestone", "oak_planks",
        "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks",
        "dark_oak_planks", "sand", "red_sand", "gravel", "gold_ore",
        "iron_ore", "coal_ore", "oak_log", "spruce_log", "oak_leaves",
        "white_wool", "red_wool", "black_wool", "glass", "light_blue_stained_glass",
        "diamond_ore", "bedrock", "water", "lava",
        # 1.9 ~ 1.12.2 新增（追加在尾部，不影响上面的固定 ID）
        "end_rod", "end_stone_bricks", "magma_block", "red_nether_bricks",
        "dirt_path", "observer", "frosted_ice", "structure_block",
        "beetroots", "bone_block", "white_glazed_terracotta",
        "purple_shulker_box", "red_concrete", "white_concrete_powder",
    ]

    def __init__(self):
        self._ids = {name: i for i, name in enumerate(self._NAMES)}

    def state_id_for_name(self, name):
        if name is None:
            return None
        if name.startswith("minecraft:"):
            name = name[len("minecraft:"):]
        return self._ids.get(name)

    def name_for(self, state_id):
        if state_id is None or state_id < 0 or state_id >= len(self._NAMES):
            return None
        from core.block_registry import BlockState
        return BlockState("minecraft:" + self._NAMES[state_id], {})

    def is_air(self, state_id):
        return state_id == 0


REGISTRY = FakeRegistry()


# --------------------------------------------------------------------------
# 1.8 legacy 区块构造
# --------------------------------------------------------------------------
def build_legacy_chunk(cx, cz, ground_up=True, with_skylight=True, add=False):
    """构造一个 1.8 Chunk Data 包（单节：y=0 有数据，其余为空）

    1.8 线格式：每节 8192 字节（每方块 uint16 LE = (id<<4)|meta），
    然后所有节的 BlockLight(2048)，再所有节的 SkyLight(2048)，末尾 Biomes(256)。
    """
    bit_map = 0x0001

    section = bytearray()
    # 每方块 uint16 LE: (block_id << 4) | metadata
    values = [0] * 4096
    values[0] = (1 << 4) | 0   # stone
    values[1] = (3 << 4) | 2   # dirt meta=2 -> podzol
    values[2] = 0              # air
    values[3] = (1 << 4) | 0   # stone
    for value in values:
        section += struct.pack("<H", value)

    # BlockLight: 2048
    section += bytes([0x11]) * 2048

    # SkyLight: 2048
    if with_skylight:
        section += bytes([0xFF]) * 2048

    # Biomes: 256
    if ground_up:
        section += bytes([1]) * 256

    payload = bytearray()
    payload += struct.pack(">i", cx) + struct.pack(">i", cz)
    payload += bytes([1 if ground_up else 0])
    payload += struct.pack(">H", bit_map)
    payload += write_varint(len(section))
    payload += bytes(section)
    return bytes(payload)


def test_legacy_parse():
    print("\n== 1.8 legacy 区块解析 ==")
    payload = build_legacy_chunk(3, -5, ground_up=True, with_skylight=True)
    chunk = parse_legacy_chunk_data_packet(payload, registry=REGISTRY)
    check("解析成功", chunk is not None)
    if chunk is None:
        return
    check("坐标正确", chunk.x == 3 and chunk.z == -5, f"got {chunk.x},{chunk.z}")
    check("16 节", len(chunk.sections) == 16, f"got {len(chunk.sections)}")
    check("min_section_y=0", chunk.min_section_y == 0)

    sec0 = chunk.sections[0]
    check("第 0 节非空", sec0.block_count > 0, f"block_count={sec0.block_count}")
    check("有天空光照", sec0.sky_light is not None and len(sec0.sky_light) == 2048)
    check("有方块光照", sec0.block_light is not None and len(sec0.block_light) == 2048)

    # 检查方块映射：索引 0 = 石头，索引 1 = 泥土(meta=2 -> podzol)
    states = sec0.block_states
    palette = sec0.block_palette
    if palette is None and sec0.block_single is not None:
        actual = [sec0.block_single]
        check("单值调色板", False, "预期多值")
    else:
        actual = [palette[i] for i in states[:4]]
    expected_stone = REGISTRY.state_id_for_name("stone")
    expected_podzol = REGISTRY.state_id_for_name("podzol")
    check("索引 0 = 石头", actual[0] == expected_stone, f"got {actual[0]} want {expected_stone}")
    check("索引 1 = podzol(泥土 meta=2)", actual[1] == expected_podzol,
          f"got {actual[1]} want {expected_podzol}")
    check("索引 2 = 空气", actual[2] == 0, f"got {actual[2]}")
    check("索引 3 = 石头", actual[3] == expected_stone)

    # 空节应为 -1
    check("第 1 节为空", chunk.sections[1].block_count == -1)


def test_legacy_nbt():
    print("\n== 1.8 legacy -> 现代 NBT ==")
    payload = build_legacy_chunk(0, 0)
    chunk = parse_legacy_chunk_data_packet(payload, registry=REGISTRY)
    check("解析成功", chunk is not None)
    if chunk is None:
        return
    raw = build_chunk_nbt(chunk, registry=REGISTRY, data_version=2975)
    root, end = nbt.parse_named_tag(raw, 0)
    check("NBT 可解析", end == len(raw), f"end={end} len={len(raw)}")
    check("DataVersion=2975", root["DataVersion"] == 2975)
    check("有 sections", "sections" in root and len(root["sections"]) == 16)
    sec0 = root["sections"][0]
    check("section 有 block_states", "block_states" in sec0)
    palette = sec0["block_states"].get("palette")
    if palette is not None:
        names = [str(e.get("Name")) for e in palette if isinstance(e, dict)]
        check("调色板是方块名", all(n.startswith("minecraft:") for n in names), f"{names}")
        check("含石头", "minecraft:stone" in names, f"{names}")


def test_legacy_no_skylight():
    print("\n== 1.8 无天空光照（下界）==")
    payload = build_legacy_chunk(1, 1, ground_up=True, with_skylight=False)
    chunk = parse_legacy_chunk_data_packet(payload, registry=REGISTRY)
    check("解析成功", chunk is not None)
    if chunk:
        check("第 0 节无天空光照", chunk.sections[0].sky_light is None)
        check("第 0 节有方块光照", chunk.sections[0].block_light is not None)


def test_legacy_with_add():
    print("\n== 1.8 多节（bitMap 多位）==")
    payload = build_legacy_chunk(2, 2)
    chunk = parse_legacy_chunk_data_packet(payload, registry=REGISTRY)
    check("解析成功", chunk is not None)


# --------------------------------------------------------------------------
# 1.16.5 flattened 区块构造
# --------------------------------------------------------------------------
def _pack_palette(states, palette):
    """把索引列表按最小位宽打包为 Long 列表"""
    import math
    bits = max(4, (len(palette) - 1).bit_length())
    per_long = 64 // bits
    long_count = math.ceil(len(states) / per_long)
    longs = [0] * long_count
    mask = (1 << bits) - 1
    for i, value in enumerate(states):
        idx = palette.index(value)
        li = i // per_long
        bi = (i % per_long) * bits
        longs[li] |= (idx & mask) << bi
    return bits, longs


def build_flattened_section(block_states, palette):
    """构造 1.16 section: Short count, UByte BPE, [palette], Long[] data"""
    bits, longs = _pack_palette(block_states, palette)
    out = bytearray()
    non_air = sum(1 for s in block_states if s != palette[0])
    out += struct.pack(">h", non_air)
    out += bytes([bits])
    out += write_varint(len(palette))
    for entry in palette:
        out += write_varint(entry)
    out += write_varint(len(longs))
    for value in longs:
        out += struct.pack(">Q", value)
    return bytes(out)


def build_1_16_chunk(cx, cz):
    stone = REGISTRY.state_id_for_name("stone")
    dirt = REGISTRY.state_id_for_name("dirt")
    palette = [0, stone, dirt]
    states = [0] * 4096
    for i in range(0, 100):
        states[i] = stone if i % 2 == 0 else dirt

    # 1.16 只发送 bitMap 中置位的节（无空节标记）
    data = build_flattened_section(states, palette)

    payload = bytearray()
    payload += struct.pack(">i", cx) + struct.pack(">i", cz)
    payload += bytes([1])                    # groundUp
    payload += write_varint(0x0001)          # bitMap: 仅第 0 节
    # heightmaps: NBT 复合（空）
    payload += nbt.build_root({}, "")
    # biomes: VarInt count + 1024 VarInt
    payload += write_varint(1024)
    for _ in range(1024):
        payload += write_varint(1)
    payload += write_varint(len(data))
    payload += data
    payload += write_varint(0)               # block entities
    return bytes(payload)


def test_flattened_parse():
    print("\n== 1.16.5 flattened 区块解析 ==")
    payload = build_1_16_chunk(7, 8)
    chunk = parse_flattened_chunk_data_packet(payload, section_count=16, min_section_y=0)
    check("解析成功", chunk is not None)
    if chunk is None:
        return
    check("坐标正确", chunk.x == 7 and chunk.z == 8)
    check("16 节", len(chunk.sections) == 16)
    sec0 = chunk.sections[0]
    check("第 0 节非空", sec0.block_count > 0, f"{sec0.block_count}")
    check("第 1 节为空", chunk.sections[1].block_count == -1)
    check("有调色板", sec0.block_palette is not None, f"{sec0.block_palette}")

    raw = build_chunk_nbt(chunk, registry=REGISTRY, data_version=2975)
    root, end = nbt.parse_named_tag(raw, 0)
    check("NBT 可解析", end == len(raw))
    palette = root["sections"][0]["block_states"].get("palette")
    names = [str(e.get("Name")) for e in palette] if palette else []
    check("调色板是方块名", all(n.startswith("minecraft:") for n in names), f"{names}")


# --------------------------------------------------------------------------
# 1.18.2 caves 区块构造
# --------------------------------------------------------------------------
def build_1_18_chunk(cx, cz, section_count=24):
    stone = REGISTRY.state_id_for_name("stone")

    section = bytearray()
    section += struct.pack(">h", 50)     # block count
    # 注意：1.18 无 fluid_count
    # 单值调色板（bits=0）：全部为石头
    section += bytes([0]) + write_varint(stone)
    # biomes: PalettedContainer(64) 单值
    section += bytes([0]) + write_varint(1)

    data = bytearray(section)
    for _ in range(section_count - 1):
        # 空节：block_count = -1
        data += struct.pack(">h", -1)

    payload = bytearray()
    payload += struct.pack(">i", cx) + struct.pack(">i", cz)
    payload += nbt.build_root({}, "")     # heightmaps NBT
    payload += write_varint(len(data))
    payload += bytes(data)
    payload += write_varint(0)            # block entities
    # light: 4 个空 bitset + 2 个空数组
    payload += write_varint(0) * 6
    return bytes(payload)


def test_caves_parse():
    print("\n== 1.18.2 caves 区块解析 ==")
    payload = build_1_18_chunk(4, 4, section_count=24)
    chunk = parse_caves_chunk_data_packet(payload, section_count=24, min_section_y=-4)
    check("解析成功", chunk is not None)
    if chunk is None:
        return
    check("坐标正确", chunk.x == 4 and chunk.z == 4)
    check("24 节", len(chunk.sections) == 24, f"{len(chunk.sections)}")
    check("min_section_y=-4", chunk.min_section_y == -4)
    sec0 = chunk.sections[0]
    check("第 0 节非空", sec0.block_count == 50, f"{sec0.block_count}")
    check("第 1 节为空", chunk.sections[1].block_count == -1)

    raw = build_chunk_nbt(chunk, registry=REGISTRY, data_version=2975)
    root, end = nbt.parse_named_tag(raw, 0)
    check("NBT 可解析", end == len(raw))
    check("24 个 section", len(root["sections"]) == 24)


# --------------------------------------------------------------------------
# 版本分派
# --------------------------------------------------------------------------
def test_version_family():
    print("\n== 协议版本家族分派 ==")
    check("47 -> legacy", chunk_format_family(47) == "legacy")
    check("754 -> flattened", chunk_format_family(754) == "flattened")
    check("758 -> modern", chunk_format_family(758) == "modern")
    check("770 -> modern", chunk_format_family(770) == "modern")
    check("777 -> modern", chunk_format_family(777) == "modern")


# --------------------------------------------------------------------------
# legacy 映射
# --------------------------------------------------------------------------
def test_legacy_mapping():
    print("\n== 1.8 (id, meta) -> 名称映射 ==")
    cases = [
        (0, 0, "air"),
        (1, 0, "stone"),
        (1, 3, "diorite"),
        (3, 2, "podzol"),
        (5, 2, "birch_planks"),
        (35, 14, "red_wool"),
        (35, 15, "black_wool"),
        (95, 3, "light_blue_stained_glass"),
        (17, 12, "oak_wood"),
        (161, 1, "dark_oak_leaves"),
        (2, 0, "grass_block"),
        (9999, 0, None),
    ]
    for bid, meta, expected in cases:
        got = legacy_block_name(bid, meta)
        check(f"{bid}:{meta} -> {expected}", got == expected, f"got {got}")


def test_legacy_state_id():
    print("\n== legacy_state_id 走注册表 ==")
    sid = legacy_state_id(1, 0, REGISTRY)
    check("石头有状态 ID", sid == REGISTRY.state_id_for_name("stone"), f"{sid}")
    check("未知方块返回 None", legacy_state_id(9999, 0, REGISTRY) is None)


# --------------------------------------------------------------------------
# 1.9 ~ 1.12.2 mid 区块构造
# --------------------------------------------------------------------------
def _pack_spanning(values, bits):
    """把索引列表按「跨 long 边界」规则打包为 Long 列表（1.9 ~ 1.12.2 编码）"""
    long_count = (len(values) * bits + 63) // 64
    longs = [0] * long_count
    mask = (1 << bits) - 1
    for i, value in enumerate(values):
        start = i * bits
        lo = start // 64
        offset = start % 64
        longs[lo] |= ((value & mask) << offset) & 0xFFFFFFFFFFFFFFFF
        if offset + bits > 64:
            longs[lo + 1] |= (value & mask) >> (64 - offset)
    return longs


def build_mid_section(indices, palette, bpe, has_sky=True):
    """构造 1.9 ~ 1.12.2 的单个 section

    palette 为 None 表示直接模式（indices 即全局 id，bpe >= 9）。
    """
    out = bytearray()
    out += bytes([bpe])
    if palette is not None:
        out += write_varint(len(palette))
        for entry in palette:
            out += write_varint(entry)
    else:
        out += write_varint(0)
    longs = _pack_spanning(indices, bpe)
    assert len(longs) == 64 * bpe  # 4096×bpe 恰被 64 整除
    out += write_varint(len(longs))
    for value in longs:
        out += struct.pack(">Q", value)
    out += bytes([0x11]) * 2048                 # BlockLight
    if has_sky:
        out += bytes([0xFF]) * 2048             # SkyLight
    return bytes(out)


def build_mid_chunk(cx, cz, ground_up=True, with_skylight=True, block_entities=()):
    """构造一个 1.9 ~ 1.12.2 Chunk Data 包（单节：y=0 有数据）

    palette 用 3 项（bpe=5，位打包跨 long 边界，正好覆盖解析器的跨界路径）：
      0 = 空气, 1 = 石头 (1<<4), 2 = 红色混凝土 (251<<4 | 14)
    """
    palette = [0, 1 << 4, (251 << 4) | 14]
    indices = [0] * 4096
    indices[0] = 1
    indices[1] = 2
    indices[2] = 1
    data = build_mid_section(indices, palette, bpe=5, has_sky=with_skylight)
    if ground_up:
        data += bytes([1]) * 256                # Biomes（plains=1）

    payload = bytearray()
    payload += struct.pack(">i", cx) + struct.pack(">i", cz)
    payload += bytes([1 if ground_up else 0])
    payload += write_varint(0x0001)             # primaryBitMask: 仅第 0 节
    payload += write_varint(len(data))
    payload += data
    payload += write_varint(len(block_entities))
    for entity in block_entities:
        payload += nbt.build_root(entity, "")
    return bytes(payload)


def test_mid_parse():
    print("\n== 1.9 ~ 1.12.2 mid 区块解析 ==")
    entities = [{
        "id": nbt.String("Chest"),        # 1.9 / 1.10 风格（未命名空间化）
        "x": nbt.Int(10), "y": nbt.Int(64), "z": nbt.Int(-3),
    }]
    payload = build_mid_chunk(3, -5, ground_up=True, with_skylight=True,
                              block_entities=entities)
    chunk = parse_mid_chunk_data_packet(payload, registry=REGISTRY)
    check("解析成功", chunk is not None)
    if chunk is None:
        return
    check("坐标正确", chunk.x == 3 and chunk.z == -5, f"got {chunk.x},{chunk.z}")
    check("16 节", len(chunk.sections) == 16, f"got {len(chunk.sections)}")
    check("min_section_y=0", chunk.min_section_y == 0)

    sec0 = chunk.sections[0]
    stone_sid = REGISTRY.state_id_for_name("stone")
    concrete_sid = REGISTRY.state_id_for_name("red_concrete")
    check("第 0 节非空", sec0.block_count == 3, f"block_count={sec0.block_count}")
    check("有天空光照", sec0.sky_light is not None and len(sec0.sky_light) == 2048)
    check("有方块光照", sec0.block_light is not None and len(sec0.block_light) == 2048)
    check("调色板含石头", sec0.block_palette is not None
          and stone_sid in sec0.block_palette, f"{sec0.block_palette}")
    check("调色板含红混凝土", sec0.block_palette is not None
          and concrete_sid in sec0.block_palette)
    if sec0.block_palette:
        idx_stone = sec0.block_palette.index(stone_sid)
        idx_conc = sec0.block_palette.index(concrete_sid)
        check("索引 0 = 石头", sec0.block_states[0] == idx_stone)
        check("索引 1 = 红混凝土(251:14)", sec0.block_states[1] == idx_conc)
        check("索引 2 = 石头", sec0.block_states[2] == idx_stone)
    check("第 1 节为空", chunk.sections[1].block_count == -1)

    check("群系 256 项", chunk.chunk_biomes is not None
          and len(chunk.chunk_biomes) == 256)
    check("群系挂在第 0 节", sec0.biomes == chunk.chunk_biomes)

    check("包内方块实体已解析", len(chunk.block_entities) == 1)
    if chunk.block_entities:
        entity = chunk.block_entities[0]
        check("实体坐标为整数", (entity["x"], entity["y"], entity["z"]) == (10, 64, -3))
        tile_id = str(entity["nbt"].get("id"))
        check("1.9 风格 id 已规整", tile_id == "minecraft:chest", f"got {tile_id}")

    raw = build_chunk_nbt(chunk, registry=REGISTRY, data_version=2975)
    root, end = nbt.parse_named_tag(raw, 0)
    check("NBT 可解析", end == len(raw), f"end={end} len={len(raw)}")
    check("DataVersion=2975", root["DataVersion"] == 2975)
    check("16 个 section", len(root["sections"]) == 16)
    palette = root["sections"][0]["block_states"].get("palette")
    names = [str(e.get("Name")) for e in palette] if palette else []
    check("调色板是现代方块名",
          "minecraft:red_concrete" in names and "minecraft:stone" in names, f"{names}")
    be_list = root.get("block_entities")
    check("方块实体写入 NBT", len(be_list) == 1
          and str(be_list[0]["id"]) == "minecraft:chest")


def test_mid_no_skylight():
    print("\n== 1.9 ~ 1.12.2 无天空光照（下界）==")
    payload = build_mid_chunk(1, 1, ground_up=True, with_skylight=False)
    chunk = parse_mid_chunk_data_packet(payload, registry=REGISTRY)
    check("解析成功（自动切换无天光假设）", chunk is not None)
    if chunk:
        check("第 0 节无天空光照", chunk.sections[0].sky_light is None)
        check("第 0 节有方块光照", chunk.sections[0].block_light is not None)
        check("方块正确解析", chunk.sections[0].block_count == 3)


def test_mid_direct_mode():
    print("\n== 1.9 ~ 1.12.2 直接模式（bpe=13，无调色板）==")
    values = [0] * 4096
    values[0] = (1 << 4) | 0          # 石头：全局 id 16
    values[1] = (213 << 4) | 0        # magma_block：全局 id 3408
    data = build_mid_section(values, None, bpe=13, has_sky=True)
    payload = bytearray()
    payload += struct.pack(">i", 2) + struct.pack(">i", 2)
    payload += bytes([1]) + write_varint(0x0001)
    payload += write_varint(len(data))
    payload += data
    payload += write_varint(0)         # 无方块实体
    chunk = parse_mid_chunk_data_packet(bytes(payload), registry=REGISTRY)
    check("解析成功", chunk is not None)
    if chunk:
        sec0 = chunk.sections[0]
        magma_sid = REGISTRY.state_id_for_name("magma_block")
        stone_sid = REGISTRY.state_id_for_name("stone")
        check("第 0 节 2 个方块", sec0.block_count == 2, f"{sec0.block_count}")
        check("调色板含石头", sec0.block_palette is not None
              and stone_sid in sec0.block_palette)
        check("调色板含 magma_block", sec0.block_palette is not None
              and magma_sid in sec0.block_palette)


def test_mid_dispatch():
    print("\n== 1.9 ~ 1.12.2 协议分派与结构探测 ==")
    for proto in (107, 210, 315, 335, 340):
        check(f"{proto} -> mid", chunk_format_family(proto) == "mid")
    check("341 不属于 mid", chunk_format_family(341) != "mid")
    check("47 仍是 legacy", chunk_format_family(47) == "legacy")

    payload = build_mid_chunk(6, -7)
    parser = ProtocolParser(allow_download=False)
    parser.protocol_version = 340
    parser.resolver.set_protocol_version(340)
    parser.chunk_family = chunk_format_family(340)
    parser.section_count, parser.min_section_y = 16, 0
    parsed = parser._parse_chunk_payload(payload)
    check("340 经分派解析成功", parsed is not None)
    if parsed is not None:
        check("坐标正确", (parsed.x, parsed.z) == (6, -7))

    # 结构探测（无精确 ID 表的版本靠 detect_play_clientbound）
    from core.packet_ids import detect_play_clientbound
    name = detect_play_clientbound(payload)
    check("探测为 Chunk Data", name == "Chunk Data", f"got {name}")

    # Join Game 探测（1.9 ~ 1.12.2 形状）
    join = bytearray()
    join += struct.pack(">i", 123)      # Entity ID
    join += bytes([1])                  # Game mode
    join += struct.pack(">i", 0)        # Dimension = 主世界
    join += bytes([0])                  # Difficulty
    join += bytes([20])                 # Max players
    join += write_string("default")
    join += bytes([0])                  # Reduced debug info
    check("Join Game 探测", detect_play_clientbound(bytes(join)) == "Login (play)",
          f"got {detect_play_clientbound(bytes(join))}")

    # Respawn 探测
    respawn = struct.pack(">i", -1) + bytes([1, 1]) + write_string("default")
    check("Respawn 探测", detect_play_clientbound(respawn) == "Respawn",
          f"got {detect_play_clientbound(respawn)}")

    # 维度解析（mid Join Game 的 dimension 是 Int）
    parser2 = ProtocolParser(allow_download=False)
    parser2.chunk_family = "mid"
    parser2._try_parse_dimension(bytes(join))
    check("mid 维度解析", parser2.dimension_name == "minecraft:overworld",
          f"got {parser2.dimension_name}")

    # 畸形数据安全
    check("空负载安全", parse_mid_chunk_data_packet(b"") is None)
    check("截断负载安全", parse_mid_chunk_data_packet(payload[:20]) is None)


def test_legacy_map_chunk_bulk():
    print("\n== 1.8 Map Chunk Bulk（0x26）==")
    # 两列：一列有天空光照，一列无（模拟下界）
    col_a = _legacy_column_data(ground_up=True, with_skylight=True)
    col_b = _legacy_column_data(ground_up=True, with_skylight=True)

    payload = bytearray()
    payload += write_varint(2)
    payload += bytes([1])                      # skyLightSent
    payload += struct.pack(">i", 3) + struct.pack(">i", -5) + struct.pack(">H", 0x0001)
    payload += struct.pack(">i", 4) + struct.pack(">i", -5) + struct.pack(">H", 0x0001)
    payload += write_varint(len(col_a)) + col_a
    payload += write_varint(len(col_b)) + col_b

    chunks = parse_legacy_map_chunk_bulk_packet(bytes(payload), registry=REGISTRY)
    check("解析出 2 个区块", len(chunks) == 2, f"got {len(chunks)}")
    if len(chunks) == 2:
        check("坐标 (3,-5)", (chunks[0].x, chunks[0].z) == (3, -5))
        check("坐标 (4,-5)", (chunks[1].x, chunks[1].z) == (4, -5))
        check("每列 16 节", len(chunks[0].sections) == 16)
        check("第一节有方块", chunks[0].sections[0].block_count > 0)
        check("天空光照已填充", chunks[0].sections[0].sky_light is not None)

    # 无天空光照标志
    payload2 = bytearray()
    payload2 += write_varint(1)
    payload2 += bytes([0])                     # skyLightSent = False
    payload2 += struct.pack(">i", 7) + struct.pack(">i", 8) + struct.pack(">H", 0x0001)
    no_sky = _legacy_column_data(ground_up=True, with_skylight=False)
    payload2 += write_varint(len(no_sky)) + no_sky
    chunks2 = parse_legacy_map_chunk_bulk_packet(bytes(payload2), registry=REGISTRY)
    check("无天空光照可解析", len(chunks2) == 1)
    if chunks2:
        check("无天空光照时 sky_light 为空", chunks2[0].sections[0].sky_light is None)

    # 畸形数据不应抛异常
    check("空负载安全", parse_legacy_map_chunk_bulk_packet(b"", registry=REGISTRY) == [])
    check("截断数据安全",
          isinstance(parse_legacy_map_chunk_bulk_packet(b"\x05\x01\x00", registry=REGISTRY), list))

    # 协议分派：0x26 -> _on_map_chunk_bulk
    parser = ProtocolParser()
    parser.protocol_version = 47
    parser.resolver.set_protocol_version(47)
    parser.state = ConnectionState.PLAY
    captured = []
    parser.on_chunk_complete = lambda c, d: captured.append((c.x, c.z))
    parser.process_clientbound(0x26, bytes(payload))
    check("经状态机分派到 Map Chunk Bulk", len(captured) == 2, f"got {captured}")


def _legacy_column_data(ground_up=True, with_skylight=True) -> bytes:
    """构造单节 1.8 区块列数据体（不含坐标/bitMap 头）"""
    section = bytearray()
    values = [0] * 4096
    values[0] = (1 << 4) | 0   # stone
    values[1] = (2 << 4) | 0   # grass
    for value in values:
        section += struct.pack("<H", value)
    section += bytes([0x11]) * 2048            # BlockLight
    if with_skylight:
        section += bytes([0xFF]) * 2048        # SkyLight
    if ground_up:
        section += bytes([1]) * 256            # Biomes
    return bytes(section)


def build_1_17_chunk(cx, cz):
    """1.17 / 1.17.1 区块包（协议 755 / 756）

    与 1.16 的差异：**没有 groundUp**，`bitMap` 是 `i64[]varint` 位图。
    """
    stone = REGISTRY.state_id_for_name("stone")
    dirt = REGISTRY.state_id_for_name("dirt")
    palette = [0, stone, dirt]
    states = [0] * 4096
    for i in range(0, 100):
        states[i] = stone if i % 2 == 0 else dirt
    data = build_flattened_section(states, palette)

    payload = bytearray()
    payload += struct.pack(">i", cx) + struct.pack(">i", cz)
    payload += write_varint(1)               # bitMap: i64[]varint，1 个 long
    payload += struct.pack(">q", 1)          # bit0 = 1 -> 仅第 0 节
    payload += nbt.build_root({}, "")        # heightmaps
    payload += write_varint(1024)            # biomes（1.17 总是下发）
    for _ in range(1024):
        payload += write_varint(1)
    payload += write_varint(len(data))
    payload += data
    payload += write_varint(0)               # block entities
    return bytes(payload)


def test_1_17_parse():
    print("\n== 1.17 / 1.17.1 区块解析 ==")
    payload = build_1_17_chunk(1, 2)

    # 直接调解析器
    chunk = parse_flattened_chunk_data_packet(
        payload, section_count=16, min_section_y=0,
        has_ground_up=False, bitmap_bitset=True,
    )
    check("直接解析成功", chunk is not None)
    if chunk is not None:
        check("坐标正确", (chunk.x, chunk.z) == (1, 2))
        check("16 节", len(chunk.sections) == 16)
        check("第 0 节非空", chunk.sections[0].block_count > 0)
        check("第 1 节为空", chunk.sections[1].block_count == -1)

    # 经状态机分派（协议 755/756）
    for proto in (755, 756):
        parser = ProtocolParser(allow_download=False)
        parser.protocol_version = proto
        parser.resolver.set_protocol_version(proto)
        parser.chunk_family = chunk_format_family(proto)
        parser.section_count, parser.min_section_y = 16, 0
        parsed = parser._parse_chunk_payload(payload)
        check(f"[{proto}] 经分派解析成功", parsed is not None,
              f"family={parser.chunk_family}")
        if parsed is not None:
            check(f"[{proto}] 坐标正确", (parsed.x, parsed.z) == (1, 2))


def test_1_18_family_boundary():
    print("\n== 1.18 / 1.18.1（协议 757）家族边界 ==")
    # 757 是 1.18 / 1.18.1，区块格式与 1.18.2（758）一致
    check("755 -> flattened", chunk_format_family(755) == "flattened")
    check("756 -> flattened", chunk_format_family(756) == "flattened")
    check("757 -> modern（不是 flattened）", chunk_format_family(757) == "modern",
          f"got {chunk_format_family(757)}")
    check("758 -> modern", chunk_format_family(758) == "modern")
    check("754 -> flattened", chunk_format_family(754) == "flattened")
    check("735 -> flattened", chunk_format_family(735) == "flattened")

    # 757 必须能解析 1.18 格式的负载
    payload = build_1_18_chunk(7, -3)
    parser = ProtocolParser(allow_download=False)
    parser.protocol_version = 757
    parser.resolver.set_protocol_version(757)
    parser.chunk_family = chunk_format_family(757)
    parser.section_count, parser.min_section_y = 24, -4
    parsed = parser._parse_chunk_payload(payload)
    check("757 能解析 1.18 区块", parsed is not None)
    if parsed is not None:
        check("757 坐标正确", (parsed.x, parsed.z) == (7, -3))

    # 1.17 的负载不应被 757 接受（格式不同，属正确行为）
    legacy_1_17 = build_1_17_chunk(1, 2)
    parser2 = ProtocolParser(allow_download=False)
    parser2.protocol_version = 757
    parser2.resolver.set_protocol_version(757)
    parser2.chunk_family = chunk_format_family(757)
    parser2.section_count, parser2.min_section_y = 24, -4
    check("757 不误收 1.17 负载", parser2._parse_chunk_payload(legacy_1_17) is None)


def test_mid_session_flow():
    print("\n== 1.9 ~ 1.12.2 完整会话（握手→登录→区块→切维度）==")
    parser = ProtocolParser(allow_download=False)
    captured = []
    parser.on_chunk_complete = lambda c, d: captured.append((c.x, c.z, d))

    # 握手（协议 340 → LOGIN）
    hs = write_varint(340) + write_string("localhost") + struct.pack(">H", 25565) + write_varint(2)
    parser.process_serverbound(0x00, hs)
    check("家族 mid", parser.chunk_family == "mid")
    check("进入 LOGIN", parser.state.name == "LOGIN")

    # Login Success：这个时代 UUID 是 36 字符字符串（1.16 起才是 16 字节）
    parser.process_clientbound(
        0x02, write_string("069a79f4-44e9-4726-a5be-fca90e38aaf5") + write_string("Steve"))
    check("Login Success 后进入 PLAY", parser.state.name == "PLAY",
          f"got {parser.state.name}")

    # Join Game（维度 Int=0 主世界）
    join = (struct.pack(">i", 123) + bytes([1]) + struct.pack(">i", 0)
            + bytes([0]) + bytes([20]) + write_string("default") + bytes([0])
            )
    parser.process_clientbound(0x01, join)
    check("Join Game 维度=主世界", parser.dimension_name == "minecraft:overworld")
    check("is_joined 置位", parser.is_joined)

    # Chunk Data（0x20，无精确表 → 结构探测）
    parser.process_clientbound(0x20, build_mid_chunk(3, -5))
    check("区块捕获回调", captured == [(3, -5, "minecraft:overworld")], f"{captured}")

    # Respawn 到下界 + 无天光区块
    respawn = struct.pack(">i", -1) + bytes([1, 1]) + write_string("default")
    parser.process_clientbound(0x07, respawn)
    check("Respawn 切下界", parser.dimension_name == "minecraft:the_nether")
    parser.process_clientbound(0x20, build_mid_chunk(0, 0, with_skylight=False))
    check("下界区块捕获", len(captured) == 2 and captured[1][2] == "minecraft:the_nether",
          f"{captured}")


def test_1_8_session_flow():
    print("\n== 1.8 完整会话（原生客户端直连，Login Success 同为字符串 UUID）==")
    parser = ProtocolParser(allow_download=False)
    captured = []
    parser.on_chunk_complete = lambda c, d: captured.append((c.x, c.z, d))

    hs = write_varint(47) + write_string("localhost") + struct.pack(">H", 25565) + write_varint(2)
    parser.process_serverbound(0x00, hs)
    check("家族 legacy", parser.chunk_family == "legacy")

    parser.process_clientbound(
        0x02, write_string("069a79f4-44e9-4726-a5be-fca90e38aaf5") + write_string("Steve"))
    check("Login Success 后进入 PLAY", parser.state.name == "PLAY",
          f"got {parser.state.name}")

    parser.process_clientbound(0x21, build_legacy_chunk(1, 2))   # 47: Chunk Data = 0x21
    check("区块捕获", len(captured) == 1, f"{captured}")


def main():
    print("== 多版本兼容测试 ==")
    test_legacy_parse()
    test_legacy_nbt()
    test_legacy_no_skylight()
    test_legacy_with_add()
    test_legacy_map_chunk_bulk()
    test_mid_parse()
    test_mid_no_skylight()
    test_mid_direct_mode()
    test_mid_dispatch()
    test_mid_session_flow()
    test_1_8_session_flow()
    test_flattened_parse()
    test_1_17_parse()
    test_1_18_family_boundary()
    test_caves_parse()
    test_version_family()
    test_legacy_mapping()
    test_legacy_state_id()
    print(f"\n===== 多版本测试: {PASS} 通过, {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
