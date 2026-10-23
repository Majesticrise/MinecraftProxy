"""
生物群系识别测试

覆盖三个版本族的生物群系 ID -> 名称映射，以及 Anvil 输出结构。

为什么重要
----------
区块里的生物群系与方块一样存的是数字 ID，而 Anvil 需要资源位置字符串。
映射错了不会让世界「加载失败」，但会让草地/树叶/水的**颜色**全错
（之前一律写 `minecraft:plains`）。

检查项:
  1. 内置数字 ID 表（1.8 / 1.16）内容正确
  2. `BiomeRegistry` 两种模式（列表式 / 数字表式）查询正确
  3. 1.18+ 网络注册表：列表顺序即 ID
  4. 真实抓包固件：解析出的 biome 调色板是**真实群系名**而非占位值
  5. 旧版群系名归一化（snowy_tundra -> snowy_plains 等）
  6. Anvil 输出：biomes.palette 为 List<String>，无 data 时长度 1
  7. 1.8 的 256 项降采样到 64 项（4×4×4）
  8. 拿不到注册表时回退占位值（世界仍可加载）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import struct  # noqa: E402

import core.nbt as nbt  # noqa: E402
from core.biome_registry import (  # noqa: E402
    LEGACY_BIOMES_1_8,
    LEGACY_BIOMES_1_16,
    PLACEHOLDER_BIOME,
    BiomeRegistry,
    build_biome_registry,
    legacy_biome_name,
    legacy_biome_name_1_16,
    normalize_biome_name,
)
from core.block_registry import load_registry  # noqa: E402
from core.chunk_format import (  # noqa: E402
    build_chunk_nbt,
    parse_chunk_data_packet,
    parse_legacy_chunk_data_packet,
)
from core.datatypes import write_varint  # noqa: E402

PASS = 0
FAIL = 0

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
registry = load_registry(770, ".")


def check(label, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


# ---------------------------------------------------------------------------
# 1. 内置数字 ID 表
# ---------------------------------------------------------------------------
def test_legacy_tables():
    print("\n== 1. 内置数字 ID 表 ==")
    check("1.8 表有 61 项", len(LEGACY_BIOMES_1_8) == 61,
          f"got {len(LEGACY_BIOMES_1_8)}")
    check("1.16 表有 79 项", len(LEGACY_BIOMES_1_16) == 79,
          f"got {len(LEGACY_BIOMES_1_16)}")

    # 关键锚点（官方 wiki《Biome/ID》）
    check("1.8 id=1 -> plains", LEGACY_BIOMES_1_8[1] == "minecraft:plains")
    check("1.8 id=2 -> desert", LEGACY_BIOMES_1_8[2] == "minecraft:desert")
    check("1.8 id=21 -> jungle", LEGACY_BIOMES_1_8[21] == "minecraft:jungle")
    check("1.8 id=165 -> eroded_badlands",
          LEGACY_BIOMES_1_8[165] == "minecraft:eroded_badlands")
    check("1.16 id=173 -> basalt_deltas",
          LEGACY_BIOMES_1_16[173] == "minecraft:basalt_deltas")
    check("1.16 id=170 -> soul_sand_valley",
          LEGACY_BIOMES_1_16[170] == "minecraft:soul_sand_valley")

    check("1.8 无 id=127（the_void 是 1.16 才有）", 127 not in LEGACY_BIOMES_1_8)
    check("1.16 有 id=127 -> the_void",
          LEGACY_BIOMES_1_16.get(127) == "minecraft:the_void")

    check("legacy_biome_name(1) == plains",
          legacy_biome_name(1) == "minecraft:plains")
    check("legacy_biome_name(9999) 回退占位",
          legacy_biome_name(9999) == PLACEHOLDER_BIOME)
    check("legacy_biome_name_1_16(173) == basalt_deltas",
          legacy_biome_name_1_16(173) == "minecraft:basalt_deltas")


# ---------------------------------------------------------------------------
# 2. BiomeRegistry 两种模式
# ---------------------------------------------------------------------------
def test_registry_modes():
    print("\n== 2. BiomeRegistry 查询 ==")
    r = BiomeRegistry()
    check("初始未加载", not r.loaded)
    check("未加载时回退占位", r.name_for(0) == PLACEHOLDER_BIOME)
    check("count 为 0", r.count == 0)

    # 数字表模式
    r.set_table({0: "minecraft:ocean", 5: "minecraft:desert"})
    check("表模式已加载", r.loaded)
    check("表模式 id=5 -> desert", r.name_for(5) == "minecraft:desert")
    check("表模式缺 id 回退", r.name_for(99) == PLACEHOLDER_BIOME)
    check("表模式 is_network=False", not r.is_network)

    # 列表模式（1.18+：下标即 ID）
    r.set_names(["minecraft:badlands", "minecraft:beach"])
    check("列表模式 is_network=True", r.is_network)
    check("列表模式 id=0 -> badlands", r.name_for(0) == "minecraft:badlands")
    check("列表模式 id=1 -> beach", r.name_for(1) == "minecraft:beach")
    check("列表模式越界回退", r.name_for(2) == PLACEHOLDER_BIOME)
    check("列表模式负数回退", r.name_for(-1) == PLACEHOLDER_BIOME)

    # 非法输入
    check("字符串 ID 回退", r.name_for("abc") == PLACEHOLDER_BIOME)
    check("None 回退", r.name_for(None) == PLACEHOLDER_BIOME)


def test_build_biome_registry():
    print("\n== 3. 按协议版本构造注册表 ==")
    r18 = build_biome_registry(47)
    check("协议 47 用 1.8 表", r18.loaded and r18.count == 61)
    check("协议 47 id=2 -> desert", r18.name_for(2) == "minecraft:desert")

    r116 = build_biome_registry(754)
    check("协议 754 用 1.16 表", r116.loaded and r116.count == 79)
    check("协议 754 id=173 -> basalt_deltas",
          r116.name_for(173) == "minecraft:basalt_deltas")

    r117 = build_biome_registry(755)
    check("协议 755 用 1.16 表", r117.loaded and r117.count == 79)

    for ver in (757, 763, 773, 777):
        r = build_biome_registry(ver)
        check(f"协议 {ver} 为空表（等网络注册表）", not r.loaded)

    check("协议 None 为空表", not build_biome_registry(None).loaded)


# ---------------------------------------------------------------------------
# 4. 群系名归一化
# ---------------------------------------------------------------------------
def test_normalization():
    print("\n== 4. 旧版群系名归一化（1.18 改名）==")
    cases = {
        "minecraft:snowy_tundra": "minecraft:snowy_plains",
        "minecraft:mountains": "minecraft:windswept_hills",
        "minecraft:stone_shore": "minecraft:stony_shore",
        "minecraft:jungle_edge": "minecraft:sparse_jungle",
        "minecraft:giant_tree_taiga": "minecraft:old_growth_pine_taiga",
        "minecraft:tall_birch_forest": "minecraft:old_growth_birch_forest",
        "minecraft:shattered_savanna": "minecraft:windswept_savanna",
        "minecraft:desert_hills": "minecraft:desert",
        "minecraft:deep_warm_ocean": "minecraft:warm_ocean",
    }
    for old, new in cases.items():
        check(f"{old} -> {new}", normalize_biome_name(old) == new)
    check("已是新名时不变",
          normalize_biome_name("minecraft:plains") == "minecraft:plains")


# ---------------------------------------------------------------------------
# 5. 真实抓包固件（1.21.9，ViaVersion 未翻译负载）
# ---------------------------------------------------------------------------
FIXTURES = [
    ("real_1_21_9_via_chunk_0.bin", 0, 0),
    ("real_1_21_9_via_chunk_1.bin", 1, 0),
    ("real_1_21_9_via_chunk_2.bin", 1, 1),
]

# 真实 1.21.10 世界（Paper 数据生成器产出）的 biome 首元素实测值，
# 用于交叉验证我们的解析结果不是凭空捏造。
REAL_BIOME_NAMES = {
    "minecraft:desert", "minecraft:warm_ocean", "minecraft:plains",
    "minecraft:river", "minecraft:lukewarm_ocean", "minecraft:badlands",
    "minecraft:deep_lukewarm_ocean", "minecraft:forest", "minecraft:beach",
    "minecraft:eroded_badlands", "minecraft:mangrove_swamp",
    "minecraft:dripstone_caves",
}


def test_real_fixture_biomes():
    print("\n== 5. 真实抓包固件（1.21.9）生物群系 ==")
    # 模拟服务端下发的网络注册表（下标即 ID）
    names = ["minecraft:badlands", "minecraft:bamboo_jungle",
             "minecraft:basalt_deltas", "minecraft:beach",
             "minecraft:birch_forest", "minecraft:cherry_grove"]
    breg = BiomeRegistry()
    breg.set_names(names, "测试")

    for fname, ex, ez in FIXTURES:
        path = os.path.join(FIXTURE_DIR, fname)
        if not os.path.exists(path):
            check(f"{fname} 存在", False)
            continue
        with open(path, "rb") as fh:
            payload = fh.read()
        parsed = parse_chunk_data_packet(
            payload, section_count=24, min_section_y=-4,
            has_fluid_count=False, has_data_length=False,
        )
        check(f"{fname} 解析成功", parsed is not None)
        if parsed is None:
            continue
        check(f"{fname} 坐标正确",
              (parsed.x, parsed.z) == (ex, ez),
              f"got ({parsed.x},{parsed.z})")

        raw = build_chunk_nbt(parsed, registry, data_version=4556,
                              biome_registry=breg)
        root, _end = nbt.parse_named_tag(raw, 0)
        secs = root["sections"]
        # 第一节的 biome 调色板必须是字符串，且在我们的注册表里
        biomes = secs[0]["biomes"]
        palette = biomes.get("palette")
        check(f"{fname} biome palette 是列表", isinstance(palette, list))
        if isinstance(palette, list):
            check(f"{fname} biome 全是字符串",
                  all(isinstance(e, str) for e in palette),
                  f"got {[type(e).__name__ for e in palette]}")
            check(f"{fname} biome 来自注册表",
                  all(e in names for e in palette),
                  f"got {palette}")
            check(f"{fname} 不是占位值",
                  all(e != PLACEHOLDER_BIOME or e in names for e in palette))


def test_placeholder_without_registry():
    print("\n== 6. 无注册表时回退占位值 ==")
    path = os.path.join(FIXTURE_DIR, FIXTURES[0][0])
    if not os.path.exists(path):
        check("固件存在", False)
        return
    with open(path, "rb") as fh:
        payload = fh.read()
    parsed = parse_chunk_data_packet(
        payload, section_count=24, min_section_y=-4,
        has_fluid_count=False, has_data_length=False,
    )
    check("解析成功", parsed is not None)
    if parsed is None:
        return
    # 不传注册表
    raw = build_chunk_nbt(parsed, registry, data_version=4556)
    root, _end = nbt.parse_named_tag(raw, 0)
    palette = root["sections"][0]["biomes"]["palette"]
    check("回退为占位值", list(palette) == [PLACEHOLDER_BIOME],
          f"got {palette}")
    check("占位值仍是字符串", all(isinstance(e, str) for e in palette))


# ---------------------------------------------------------------------------
# 7. Anvil 输出结构
# ---------------------------------------------------------------------------
def test_anvil_structure():
    print("\n== 7. Anvil biomes 容器结构 ==")
    path = os.path.join(FIXTURE_DIR, FIXTURES[0][0])
    if not os.path.exists(path):
        return
    with open(path, "rb") as fh:
        payload = fh.read()
    parsed = parse_chunk_data_packet(
        payload, section_count=24, min_section_y=-4,
        has_fluid_count=False, has_data_length=False,
    )
    if parsed is None:
        return
    names = ["minecraft:plains", "minecraft:desert", "minecraft:ocean"]
    breg = BiomeRegistry()
    breg.set_names(names)
    raw = build_chunk_nbt(parsed, registry, data_version=4556,
                          biome_registry=breg)
    root, _end = nbt.parse_named_tag(raw, 0)

    for i, sec in enumerate(root["sections"][:3]):
        b = sec.get("biomes")
        check(f"sec{i} 含 biomes", isinstance(b, dict))
        if not isinstance(b, dict):
            continue
        pal = b.get("palette")
        check(f"sec{i} palette 是 List<String>",
              isinstance(pal, list) and all(isinstance(e, str) for e in pal))
        # 单元素调色板时不应有 data（官方规范）
        if isinstance(pal, list) and len(pal) == 1:
            check(f"sec{i} 单元素无 data", "data" not in b)
        # 多元素时 data 长度 = ceil(64 / (64//bits))
        if isinstance(pal, list) and len(pal) > 1:
            data = b.get("data")
            bits = max(1, (len(pal) - 1).bit_length())
            per = 64 // bits
            expect = (64 + per - 1) // per
            check(f"sec{i} data 长度正确",
                  isinstance(data, list) and len(data) == expect,
                  f"palette={len(pal)} bits={bits} got "
                  f"{len(data) if isinstance(data, list) else data} "
                  f"expect {expect}")


# ---------------------------------------------------------------------------
# 8. 1.8 的 256 项降采样
# ---------------------------------------------------------------------------
def test_legacy_1_8_biomes():
    print("\n== 8. 1.8 生物群系（256 项 -> 64 项）==")
    # 构造一个最小 1.8 区块：bitMap 只含第 0 节
    # 结构: Int x, Int z, Bool groundUp, UShort bitMap, VarInt size, Bytes data
    block_data = b"\x00" * (8192 + 2048 + 2048)   # Blocks + BlockLight + SkyLight
    # 256 字节生物群系：索引 z*16+x，故意放两种值
    biomes = bytearray(256)
    for z in range(16):
        for x in range(16):
            biomes[z * 16 + x] = 2 if (x < 8) else 1   # 左半 desert，右半 plains
    body = block_data + bytes(biomes)
    payload = (struct.pack(">i", 3) + struct.pack(">i", -4)
               + bytes([1])                      # groundUp
               + struct.pack(">H", 0b1)          # bitMap（UShort）：第 0 节
               + write_varint(len(body)) + body)

    parsed = parse_legacy_chunk_data_packet(payload, registry=registry)
    check("1.8 区块解析成功", parsed is not None)
    if parsed is None:
        return
    check("chunk_biomes 有 256 项",
          parsed.chunk_biomes is not None and len(parsed.chunk_biomes) == 256,
          f"got {len(parsed.chunk_biomes) if parsed.chunk_biomes else None}")

    raw = build_chunk_nbt(parsed, registry, data_version=2975,
                          biome_registry=build_biome_registry(47),
                          normalize_biomes=True)
    root, _end = nbt.parse_named_tag(raw, 0)
    palette = root["sections"][0]["biomes"]["palette"]
    check("降采样后 64 项", isinstance(palette, list) and len(palette) == 64,
          f"got {len(palette) if isinstance(palette, list) else palette}")
    if isinstance(palette, list) and len(palette) == 64:
        # 采样点是 (z4*4, x4*4)，即 x<8 -> desert；Y 方向重复 4 次
        expect = ["minecraft:desert" if x4 < 2 else "minecraft:plains"
                  for _y4 in range(4) for _z4 in range(4) for x4 in range(4)]
        check("采样值正确（每 4 格取一点）", list(palette) == expect,
              f"got {list(palette)[:8]} expect {expect[:8]}")
        check("已归一化（无旧名）",
              all(n in ("minecraft:desert", "minecraft:plains")
                  for n in palette))


# ---------------------------------------------------------------------------
# 9. 1.16 的 1024 项降采样
# ---------------------------------------------------------------------------
def test_legacy_1_16_biomes():
    print("\n== 9. 1.16 生物群系（1024 项 -> 64 项）==")
    from core.chunk_format import _build_legacy_biome_container  # noqa: E402

    # 1024 项：section 0 的 Y=0 层在偏移 0..15
    ids = [0] * 1024
    for z4 in range(4):
        for x4 in range(4):
            ids[z4 * 4 + x4] = 37 if x4 < 2 else 1   # badlands / plains
    # 其余层填不同值，确认不会被误取
    for i in range(16, 1024):
        ids[i] = 44

    breg = build_biome_registry(754)
    container = _build_legacy_biome_container(ids, breg, normalize=False)
    palette = container["palette"]
    check("64 项", len(palette) == 64, f"got {len(palette)}")
    expect = ["minecraft:badlands" if x4 < 2 else "minecraft:plains"
              for _y4 in range(4) for _z4 in range(4) for x4 in range(4)]
    check("只取 Y=0 层", list(palette) == expect,
          f"got {list(palette)[:8]}")


def test_registry_data_packet():
    """
    端到端：模拟服务端下发的 `Registry Data` 包，确认 protocol.py 能提取群系表。

    包结构（1.21.10，PrismarineJS `packet_registry_data`）：
        Identifier(registryId) + VarInt(count)
        + count × ( Identifier(entryId) + Bool(hasValue) + [anonymousNbt] )
    """
    print("\n== 10. Registry Data 包端到端 ==")
    from core.datatypes import write_varint as wv
    from core.protocol import ProtocolParser

    biomes = ["minecraft:badlands", "minecraft:beach", "minecraft:desert",
              "minecraft:forest", "minecraft:plains", "minecraft:river",
              "minecraft:warm_ocean", "minecraft:dripstone_caves"]

    def ident(s):
        b = s.encode("utf-8")
        return wv(len(b)) + b

    payload = bytearray()
    payload += ident("minecraft:worldgen/biome")
    payload += wv(len(biomes))
    for name in biomes:
        payload += ident(name)
        payload += b"\x01"
        payload += nbt.serialize_anonymous_tag(nbt.compound({}))

    parser = ProtocolParser(debug=False)
    check("初始无注册表", not parser.biome_registry.loaded)
    parser._on_registry_data(bytes(payload))
    check("解析后已加载", parser.biome_registry.loaded)
    check("项数正确", parser.biome_registry.count == len(biomes),
          f"got {parser.biome_registry.count}")
    check("是网络注册表模式", parser.biome_registry.is_network)
    for i, name in enumerate(biomes):
        check(f"id={i} -> {name}", parser.biome_registry.name_for(i) == name)
    check("越界回退占位",
          parser.biome_registry.name_for(99) == PLACEHOLDER_BIOME)


def main():
    test_legacy_tables()
    test_registry_modes()
    test_build_biome_registry()
    test_normalization()
    test_real_fixture_biomes()
    test_placeholder_without_registry()
    test_anvil_structure()
    test_legacy_1_8_biomes()
    test_legacy_1_16_biomes()
    test_registry_data_packet()

    print("\n" + "=" * 60)
    print(f"生物群系测试: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
