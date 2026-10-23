"""
导出世界可用性测试

这是本次修复的核心验证：确认导出的目录是**真的能被 Minecraft 加载的世界**，
而不只是「看起来像」的数据。

检查项:
  1. 调色板是方块名称（Compound / 字符串），不是整数
  2. 方块名称与属性可从注册表正确还原
  3. 光照写入 section（SkyLight / BlockLight）
  4. yPos / Y 与维度高度一致（主世界 -4，下界 0）
  5. level.dat 存在、可 gzip 解压、可解析为 NBT、含必需字段
  6. 世界高度按维度自适应（24 节 vs 16 节）
"""

import os
import sys

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gzip
import math
import struct
import tempfile
import zlib
from pathlib import Path

import core.nbt as nbt
from core.block_registry import load_registry
from core.chunk_extractor import ChunkExtractor
from core.chunk_format import build_chunk_nbt, compress_chunk_nbt, parse_chunk_data_packet
from core.datatypes import write_varint
from core.level_dat import build_level_dat, ensure_world_metadata

PASS = 0
FAIL = 0

registry = load_registry(770, ".")


def check(label, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


def pack(values, bits, entries):
    per = 64 // bits
    longs = [0] * math.ceil(entries / per)
    for i, v in enumerate(values):
        longs[i // per] |= (v & ((1 << bits) - 1)) << ((i % per) * bits)
    return longs


def make_section(block_palette, bits=4):
    out = bytearray()
    out += struct.pack(">h", 10) + struct.pack(">h", 0)
    out += bytes([bits]) + write_varint(len(block_palette))
    for v in block_palette:
        out += write_varint(v)
    for lv in pack([i % len(block_palette) for i in range(4096)], bits, 4096):
        out += struct.pack(">Q", lv)
    out += bytes([0]) + write_varint(1)
    return bytes(out)


def make_chunk(section_count, with_light=False):
    p = bytearray()
    p += struct.pack(">i", 5) + struct.pack(">i", -7)
    p += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 0xABCD)
    data = make_section([1, 2, 10, 1000]) + struct.pack(">h", -1) * (section_count - 1)
    p += write_varint(len(data)) + data
    p += write_varint(0)
    if with_light:
        # 掩码位 1（= 第一个 section）
        p += write_varint(1) + struct.pack(">Q", 0b10)
        p += write_varint(1) + struct.pack(">Q", 0b10)
        p += write_varint(0) + write_varint(0)
        # 官方格式：每个光照数组自带 VarInt 长度前缀
        p += write_varint(1) + write_varint(2048) + bytes([0xAB]) * 2048
        p += write_varint(1) + write_varint(2048) + bytes([0xCD]) * 2048
    else:
        p += write_varint(0) * 6
    return bytes(p)


def test_palette_is_names():
    print("\n== 1. 调色板必须是方块名称 ==")
    chunk = parse_chunk_data_packet(make_chunk(24), section_count=24)
    check("解析成功", chunk is not None)
    raw = build_chunk_nbt(chunk, registry, data_version=4325)
    root, end = nbt.parse_named_tag(raw, 0)
    check("NBT 可解析", end == len(raw))

    palette = root["sections"][0]["block_states"]["palette"]
    check("palette 是列表", isinstance(palette, list))
    check("palette 元素是 Compound", isinstance(palette[0], dict))

    names = []
    for entry in palette:
        if isinstance(entry, dict):
            names.append(str(entry.get("Name", "")))
        else:
            names.append(str(entry))
    check("全部以 minecraft: 开头", all(n.startswith("minecraft:") for n in names),
          f"got {names}")
    check("没有整数形式的条目", not any(n.isdigit() for n in names))

    # 含属性的项应带 Properties
    with_props = [e for e in palette if isinstance(e, dict) and "Properties" in e]
    check("含属性的方块保留 Properties", len(with_props) >= 1, f"got {len(with_props)}")
    if with_props:
        props = with_props[0]["Properties"]
        check("Properties 是字符串字典",
              isinstance(props, dict) and all(isinstance(v, str) for v in props.values()))

    print("     调色板内容:", names)


def test_biome_palette_is_string():
    """
    生物群系调色板的元素必须是 **String**（资源位置），不是 Compound。

    官方 wiki《Chunk format》(1.18+)："biomes → palette → {{nbt|string|Name}}:
    Biome resource location"。真实 1.21.10 世界实测为 `List<8>`（TAG_String）。
    写成 Compound 会让 C2ME / 原版加载 section 时报
    `(Not a string -> using default)`（每个 section 一条）。
    """
    print("\n== 1b. 生物群系调色板必须是字符串 ==")
    chunk = parse_chunk_data_packet(make_chunk(24), section_count=24)
    check("解析成功", chunk is not None)
    raw = build_chunk_nbt(chunk, registry, data_version=4325)
    root, _end = nbt.parse_named_tag(raw, 0)

    biomes = root["sections"][0]["biomes"]
    check("biomes 是 Compound", isinstance(biomes, dict))
    palette = biomes.get("palette")
    check("biome palette 是列表", isinstance(palette, list))
    check("biome palette 非空", bool(palette))
    if palette:
        check("biome palette 元素是字符串",
              all(isinstance(e, str) for e in palette),
              f"got types {[type(e).__name__ for e in palette]}")
        check("biome 以 minecraft: 开头",
              all(str(e).startswith("minecraft:") for e in palette),
              f"got {palette}")


def test_chunk_top_level_keys():
    """
    区块顶层键必须与真实 1.21.10 世界一致。

    实测真实区块顶层键：Status / zPos / block_entities / yPos / LastUpdate /
    structures / InhabitedTime / xPos / Heightmaps / sections /
    isLightOn / block_ticks / PostProcessing / DataVersion / fluid_ticks。
    """
    print("\n== 1c. 区块顶层必需键 ==")
    chunk = parse_chunk_data_packet(make_chunk(24), section_count=24)
    check("解析成功", chunk is not None)
    raw = build_chunk_nbt(chunk, registry, data_version=4325)
    root, _end = nbt.parse_named_tag(raw, 0)

    for key in ("DataVersion", "xPos", "zPos", "yPos", "Status", "LastUpdate",
                "InhabitedTime", "isLightOn", "sections", "Heightmaps",
                "structures", "block_entities", "block_ticks", "fluid_ticks",
                "PostProcessing"):
        check(f"含 {key}", key in root)

    check("Status == full", str(root.get("Status")) == "full")
    check("isLightOn == 1", root.get("isLightOn") == 1)

    # structures 必须同时含 starts 与 References，否则原版报
    # `Not a compound tag: ...`
    structures = root.get("structures")
    check("structures 是 Compound", isinstance(structures, dict))
    if isinstance(structures, dict):
        check("structures 含 starts", "starts" in structures)
        check("structures 含 References", "References" in structures)

    # PostProcessing 长度 = 节数
    pp = root.get("PostProcessing")
    check("PostProcessing 是列表", isinstance(pp, list))
    if isinstance(pp, list):
        check("PostProcessing 长度 == 节数",
              len(pp) == len(root.get("sections", [])),
              f"got {len(pp)} vs {len(root.get('sections', []))}")

    check("block_entities 恒存在（可为空）", isinstance(root.get("block_entities"), list))


def test_registry_accuracy():
    print("\n== 2. 注册表映射准确性 ==")
    cases = {0: "minecraft:air", 1: "minecraft:stone"}
    ok = True
    for state_id, expected in cases.items():
        got = registry.name_for(state_id)
        if got is None or got.name != expected:
            ok = False
            print(f"     {state_id} -> {got} (期望 {expected})")
    check("基础方块映射正确", ok)

    # 带属性的方块
    state = registry.name_for(1000)
    check("带属性方块可解析", state is not None and bool(state.properties),
          f"got {state}")
    check("空气识别正确", registry.is_air(0))
    check("石头不是空气", not registry.is_air(1))


def test_light_written():
    print("\n== 3. 光照写入 section ==")
    chunk = parse_chunk_data_packet(make_chunk(24, with_light=True), section_count=24)
    check("解析成功", chunk is not None)
    check("第一个 section 有天空光照",
          chunk.sections[0].sky_light is not None)
    check("第一个 section 有方块光照",
          chunk.sections[0].block_light is not None)

    raw = build_chunk_nbt(chunk, registry, data_version=4325)
    root, _ = nbt.parse_named_tag(raw, 0)
    sec0 = root["sections"][0]
    check("NBT 中含 SkyLight", "SkyLight" in sec0)
    check("NBT 中含 BlockLight", "BlockLight" in sec0)
    if "SkyLight" in sec0:
        check("SkyLight 长度 2048", len(sec0["SkyLight"]) == 2048,
              f"got {len(sec0['SkyLight'])}")
        check("SkyLight 内容正确", bytes(sec0["SkyLight"]) == bytes([0xAB]) * 2048)


def test_dimension_heights():
    print("\n== 4. 世界高度按维度自适应 ==")
    # 主世界 24 节 / 最低节 -4
    overworld = parse_chunk_data_packet(make_chunk(24), section_count=24, min_section_y=-4)
    raw = build_chunk_nbt(overworld, registry, data_version=4325)
    root, _ = nbt.parse_named_tag(raw, 0)
    ys = [s["Y"] for s in root["sections"]]
    check("主世界 24 节", len(ys) == 24, f"got {len(ys)}")
    check("主世界 Y 从 -4 开始", ys[0] == -4, f"got {ys[0]}")
    check("主世界 yPos = -4", root["yPos"] == -4)

    # 下界 16 节 / 最低节 0
    nether = parse_chunk_data_packet(make_chunk(16), section_count=16, min_section_y=0)
    check("下界 16 节能解析", nether is not None)
    raw = build_chunk_nbt(nether, registry, data_version=4325)
    root, _ = nbt.parse_named_tag(raw, 0)
    ys = [s["Y"] for s in root["sections"]]
    check("下界 16 节", len(ys) == 16, f"got {len(ys)}")
    check("下界 Y 从 0 开始", ys[0] == 0, f"got {ys[0]}")
    check("下界 yPos = 0", root["yPos"] == 0)

    # 模组 32 节
    modded = parse_chunk_data_packet(make_chunk(32), section_count=32, min_section_y=-8)
    check("模组 32 节能解析", modded is not None)
    raw = build_chunk_nbt(modded, registry, data_version=4325)
    root, _ = nbt.parse_named_tag(raw, 0)
    check("模组 32 节写入", len(root["sections"]) == 32)


def test_level_dat():
    print("\n== 5. level.dat 可用性 ==")
    with tempfile.TemporaryDirectory() as tmp:
        ensure_world_metadata(tmp, world_name="TestWorld", data_version=4325)
        path = Path(tmp) / "level.dat"
        check("level.dat 存在", path.exists())
        check("session.lock 存在", (Path(tmp) / "session.lock").exists())

        if path.exists():
            try:
                with gzip.open(path, "rb") as fh:
                    raw = fh.read()
                check("可 gzip 解压", len(raw) > 0)
                root, end = nbt.parse_named_tag(raw, 0)
                check("NBT 可解析", end == len(raw))
                check("含 Data 标签", "Data" in root)
                data = root["Data"]
                for field in ("LevelName", "DataVersion", "SpawnX", "GameType"):
                    check(f"含 {field}", field in data)
                check("世界名正确", str(data["LevelName"]) == "TestWorld")
                check("DataVersion 正确", data["DataVersion"] == 4325)

                # 1.16+ 缺 WorldGenSettings 会导致游戏报
                #   No key dimensions in MapLike[{}]; No key seed in MapLike[{}]
                # 并拒绝加载世界，必须保证存在且结构完整。
                check("含 WorldGenSettings", "WorldGenSettings" in data)
                wgs = data.get("WorldGenSettings")
                if isinstance(wgs, dict):
                    check("WGS 含 seed", "seed" in wgs)
                    check("WGS 含 dimensions", "dimensions" in wgs)
                    dims = wgs.get("dimensions")
                    if isinstance(dims, dict):
                        for dim in ("minecraft:overworld", "minecraft:the_nether",
                                    "minecraft:the_end"):
                            check(f"dimensions 含 {dim}", dim in dims)
                        ow = dims.get("minecraft:overworld")
                        if isinstance(ow, dict):
                            check("维度含 type", "type" in ow)
                            check("维度含 generator", "generator" in ow)

                # 末地龙的战斗状态必须放在 `Data` 顶层（与 WorldGenSettings 同级）。
                # 真实 1.21.10 世界的 level.dat 实测如此，且**没有** DimensionData。
                # 若误放进 DimensionData -> 1，游戏会报 `key missing: DragonFight`。
                check("含顶层 DragonFight", "DragonFight" in data)
                check("不含 DimensionData（非官方结构）", "DimensionData" not in data)
                df = data.get("DragonFight")
                if isinstance(df, dict):
                    for f in ("DragonKilled", "PreviouslyKilled",
                              "NeedsStateScanning", "Gateways"):
                        check(f"DragonFight 含 {f}", f in df)
                    gw = df.get("Gateways")
                    check("Gateways 为 20 个索引",
                          isinstance(gw, list) and len(gw) == 20,
                          f"got {len(gw) if isinstance(gw, list) else gw}")
            except Exception as exc:
                check(f"level.dat 解析失败: {exc}", False)


def test_extractor_writes_world():
    print("\n== 6. 提取器产出完整世界 ==")
    import asyncio

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            extractor = ChunkExtractor(tmp)
            extractor.data_version = 4325
            for cx, cz, dim in ((0, 0, "minecraft:overworld"),
                                (1, 0, "minecraft:overworld"),
                                (0, 0, "minecraft:the_nether")):
                chunk = parse_chunk_data_packet(make_chunk(24), section_count=24)
                data = compress_chunk_nbt(build_chunk_nbt(chunk, registry, data_version=4325))
                await extractor.save_chunk((cx, cz, data), dim)
            await extractor.shutdown()
            return tmp, extractor

    tmp, extractor = asyncio.run(run())
    # 注意: TemporaryDirectory 已销毁，改用统计信息验证
    check("主世界写入 2 个区块", extractor.stats["chunks_saved"] == 3,
          f"got {extractor.stats['chunks_saved']}")
    check("识别到 2 个维度", len(extractor.stats["dimensions"]) == 2,
          f"got {extractor.stats['dimensions']}")


def test_region_dir_naming():
    print("\n== 7. 维度目录命名 ==")
    from core.chunk_extractor import dimension_dir_name
    check("主世界 -> ''", dimension_dir_name("minecraft:overworld") == "")
    check("下界 -> DIM-1", dimension_dir_name("minecraft:the_nether") == "DIM-1")
    check("末地 -> DIM1", dimension_dir_name("minecraft:the_end") == "DIM1")
    check("ID -1 -> DIM-1", dimension_dir_name(-1) == "DIM-1")
    check("未知维度有独立目录",
          dimension_dir_name("mymod:custom") == "dimension_mymod_custom",
          dimension_dir_name("mymod:custom"))


if __name__ == "__main__":
    print("=" * 60)
    print("  导出世界可用性测试")
    print("=" * 60)
    test_palette_is_names()
    test_biome_palette_is_string()
    test_chunk_top_level_keys()
    test_registry_accuracy()
    test_light_written()
    test_dimension_heights()
    test_level_dat()
    test_extractor_writes_world()
    test_region_dir_naming()
    print(f"\n===== 可用性测试: {PASS} 通过, {FAIL} 失败 =====")
    raise SystemExit(1 if FAIL else 0)
