"""
自测脚本：验证 NBT、区块解析、区域写入与包 ID 解析。

运行: python selftest.py
"""

import os
import sys

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import math
import struct
import tempfile
import zlib
from pathlib import Path

import core.nbt as nbt
from core.block_registry import load_registry
from core.chunk_extractor import AnvilWriter, ChunkExtractor
from core.chunk_format import (
    build_chunk_nbt,
    parse_block_entity_data_packet,
    parse_chunk_data_packet,
    parse_unload_chunk_packet,
    parse_update_light_packet,
)
from core.datatypes import PacketBuffer, write_string, write_varint
from core.packet_ids import (
    DIR_CLIENTBOUND,
    DIR_SERVERBOUND,
    STATE_LOGIN,
    STATE_PLAY,
    PacketIdResolver,
)
from core.protocol import ConnectionState, ProtocolParser

registry = load_registry(770, '.')

PASS = 0
FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


# --------------------------------------------------------------------- NBT
def test_nbt_roundtrip():
    print("\n== NBT 往返 ==")
    original = {
        "id": nbt.String("minecraft:chest"),
        "x": nbt.Int(12),
        "y": nbt.Int(64),
        "z": nbt.Int(-30),
        "Items": nbt.List(nbt.TAG_COMPOUND, [
            nbt.compound({"Slot": nbt.Byte(0), "Count": nbt.Byte(3)}),
        ]),
        "data": nbt.LongArray([1, 2, 3]),
    }
    raw = nbt.build_root(original, "")
    parsed, end = nbt.parse_named_tag(raw, 0)
    check("解析消耗全部字节", end == len(raw))
    check("id 一致", parsed["id"] == "minecraft:chest")
    check("x 一致", parsed["x"] == 12)
    check("z 为负", parsed["z"] == -30)
    check("列表长度", len(parsed["Items"]) == 1)
    check("长数组", list(parsed["data"]) == [1, 2, 3])

    size = nbt.nbt_size(raw, 0)
    check("nbt_size 正确", size == len(raw))


# ------------------------------------------------------------- 调色板打包
def pack_bits(values, bits):
    entries_per_long = 64 // bits
    long_count = math.ceil(len(values) / entries_per_long)
    longs = [0] * long_count
    for i, v in enumerate(values):
        li = i // entries_per_long
        bi = (i % entries_per_long) * bits
        longs[li] |= (v & ((1 << bits) - 1)) << bi
    return longs


def build_section(block_palette, biome_palette, bits=4):
    """构造一个区块节的字节流"""
    out = bytearray()
    out += struct.pack(">h", 10)   # block_count
    out += struct.pack(">h", 0)    # fluid_count
    # block states: 间接调色板, bits=4
    out += bytes([bits])
    out += write_varint(len(block_palette))
    for v in block_palette:
        out += write_varint(v)
    values = [i % len(block_palette) for i in range(4096)]
    for long_val in pack_bits(values, bits):
        out += struct.pack(">Q", long_val)
    # biomes: 单一值
    out += bytes([0])
    out += write_varint(biome_palette[0])
    return bytes(out)


def test_chunk_packet():
    print("\n== 区块数据包解析 ==")
    payload = bytearray()
    payload += struct.pack(">i", 7)     # chunk x
    payload += struct.pack(">i", -3)    # chunk z
    # heightmaps: 1 个元素
    payload += write_varint(1)
    payload += write_varint(4)          # MOTION_BLOCKING
    payload += write_varint(1)          # 1 个 long
    payload += struct.pack(">Q", 0x1234)
    # 区块数据
    section_bytes = build_section([0, 1, 2, 3], [5], bits=4)
    section_bytes += build_section([0, 1], [7], bits=4)  # 第 2 节
    # 其余 22 节为空
    section_bytes += struct.pack(">h", -1) * 22
    payload += write_varint(len(section_bytes))
    payload += section_bytes
    # 方块实体: 1 个
    payload += write_varint(1)
    payload += bytes([0x21])            # packed_xz = (2<<4)|1
    payload += struct.pack(">h", 70)
    payload += write_varint(6)
    # 1.18+ 的方块实体 NBT 是「匿名可选」：type + value，无名字
    payload += nbt.serialize_anonymous_tag({"id": nbt.String("minecraft:chest")})
    # 光照
    payload += write_varint(0)  # sky mask
    payload += write_varint(0)  # block mask
    payload += write_varint(0)  # empty sky
    payload += write_varint(0)  # empty block
    payload += write_varint(0)  # sky arrays
    payload += write_varint(0)  # block arrays

    parsed = parse_chunk_data_packet(bytes(payload), section_count=24)
    check("解析成功", parsed is not None)
    if parsed:
        check("x/z 正确", (parsed.x, parsed.z) == (7, -3))
        check("高度图", "MOTION_BLOCKING" in parsed.heightmaps)
        check("节数", len(parsed.sections) == 24)
        check("第 1 节非空", parsed.sections[0].block_count == 10)
        check("第 3 节为空", parsed.sections[2].block_count == -1)
        check("方块实体数量", len(parsed.block_entities) == 1)
        check("方块实体局部坐标", (parsed.block_entities[0]["local_x"],
                                   parsed.block_entities[0]["local_z"]) == (2, 1))

        raw = build_chunk_nbt(parsed, registry, data_version=4325)
        check("NBT 非空", len(raw) > 0)
        root, end = nbt.parse_named_tag(raw, 0)
        check("NBT 解析成功", end == len(raw))
        check("xPos 正确", root["xPos"] == 7)
        check("zPos 正确", root["zPos"] == -3)
        check("状态为 full", str(root["Status"]) == "full")
        check("sections 数量（含空节，共 24）", len(root["sections"]) == 24)
        sec0 = root["sections"][0]
        check("节 Y（最低节 Y = -4）", sec0["Y"] == -4)
        check("节调色板", len(sec0["block_states"]["palette"]) == 4)
        check("节数据长度", len(sec0["block_states"]["data"]) == 256)

    # 反向验证：非区块包应返回 None
    check("错误包返回 None", parse_chunk_data_packet(b"\x00" * 4) is None)


def test_other_packets():
    print("\n== 其他区块包 ==")
    # Unload Chunk: Z 在前
    payload = struct.pack(">i", -9) + struct.pack(">i", 4)
    check("卸载区块解析", parse_unload_chunk_packet(payload) == (4, -9))
    check("卸载区块长度校验", parse_unload_chunk_packet(payload + b"\x00") is None)

    # Block Entity Data
    pos = ((5 & 0x3FFFFFF) << 38) | (6 & 0xFFF) | ((7 & 0x3FFFFFF) << 12)
    be = struct.pack(">Q", pos) + write_varint(1)
    be += nbt.build_root({"id": nbt.String("minecraft:furnace")}, "")
    parsed_be = parse_block_entity_data_packet(be)
    check("方块实体解析", parsed_be is not None)
    if parsed_be:
        check("坐标", (parsed_be["x"], parsed_be["y"], parsed_be["z"]) == (5, 6, 7))

    # Update Light
    light = write_varint(2) + write_varint(3)
    light += write_varint(1) + struct.pack(">Q", 0b101)
    light += write_varint(1) + struct.pack(">Q", 0b010)
    light += write_varint(0) + write_varint(0)
    light += write_varint(0) + write_varint(0)
    parsed_light = parse_update_light_packet(light)
    check("光照更新解析", parsed_light is not None and parsed_light.x == 2 and parsed_light.z == 3)


# ------------------------------------------------------------- ID 解析器
def test_resolver():
    print("\n== 版本自适应 ID 解析 ==")
    r = PacketIdResolver()

    # 无版本信息时用结构探测
    r.set_protocol_version(9999)
    check("未知版本无精确表", not r.has_exact_table)

    # Login Success 探测: UUID(16) + name
    payload = bytes(range(16)) + write_string("Steve")
    name = r.resolve(STATE_LOGIN, DIR_CLIENTBOUND, 0x02, payload)
    check("探测 Login Success", name == "Login Success")

    # Set Compression 探测: 单个 VarInt
    name = r.resolve(STATE_LOGIN, DIR_CLIENTBOUND, 0x03, write_varint(256))
    check("探测 Set Compression", name == "Set Compression")

    # 空负载 = 配置阶段完成
    name = r.resolve("CONFIGURATION", DIR_SERVERBOUND, 0x03, b"")
    check("探测 Acknowledge Configuration", name == "Acknowledge Configuration")

    # 精确表版本
    r2 = PacketIdResolver()
    r2.set_protocol_version(777)
    check("777 有精确表", r2.has_exact_table)
    check("777 区块包 ID", r2.id_for(STATE_PLAY, DIR_CLIENTBOUND,
                                      "Chunk Data and Update Light") == 0x2E)
    check("777 方块实体 ID", r2.id_for(STATE_PLAY, DIR_CLIENTBOUND,
                                       "Block Entity Data") == 0x06)
    check("777 卸载区块 ID", r2.id_for(STATE_PLAY, DIR_CLIENTBOUND,
                                       "Unload Chunk") == 0x26)
    check("777 光照 ID", r2.id_for(STATE_PLAY, DIR_CLIENTBOUND,
                                   "Update Light") == 0x31)
    check("777 Set Compression", r2.id_for(STATE_LOGIN, DIR_CLIENTBOUND,
                                           "Set Compression") == 0x03)
    check("777 Finish Configuration", r2.id_for("CONFIGURATION", DIR_CLIENTBOUND,
                                                "Finish Configuration") == 0x03)

    # 通过精确表解析
    name = r2.resolve(STATE_PLAY, DIR_CLIENTBOUND, 0x2E, b"")
    check("表驱动解析区块包", name == "Chunk Data and Update Light")


# ------------------------------------------------------------ 状态机
def test_state_machine():
    print("\n== 状态机 ==")
    parser = ProtocolParser()

    # 握手
    hs = write_varint(777) + write_string("mc.example.com") + struct.pack(">H", 25565) + write_varint(2)
    parser.process_serverbound(0x00, hs)
    check("协议版本已记录", parser.protocol_version == 777)
    check("进入 LOGIN", parser.state == ConnectionState.LOGIN)

    # Login Success
    payload = bytes(range(16)) + write_string("Alex") + write_varint(0) + bytes(16)
    parser.process_clientbound(0x02, payload)
    check("玩家名", parser.player_name is None)  # 名字由 Login Start 提供
    check("状态仍为 LOGIN", parser.state == ConnectionState.LOGIN)

    # Login Acknowledged (serverbound 0x03)
    parser.process_serverbound(0x03, b"")
    check("进入 CONFIGURATION", parser.state == ConnectionState.CONFIGURATION)

    # Finish Configuration
    parser.process_clientbound(0x03, b"")
    check("进入 PLAY", parser.state == ConnectionState.PLAY)


# ------------------------------------------------------------ 区域写入
def test_region_write():
    print("\n== 区域文件写入 ==")
    with tempfile.TemporaryDirectory() as tmp:
        writer = AnvilWriter(tmp)
        data = zlib.compress(nbt.build_root({"xPos": nbt.Int(1)}, ""))

        ok = asyncio.get_event_loop().run_until_complete(
            writer.save_chunk(1, 2, data, 0)
        )
        check("写入成功", ok)

        region_file = Path(tmp) / "region" / "r.0.0.mca"
        check("区域文件存在", region_file.exists())
        if region_file.exists():
            raw = region_file.read_bytes()
            check("头部 8KB", len(raw) >= 8192)
            index = 1 + 2 * 32
            loc = struct.unpack_from(">I", raw, index * 4)[0]
            check("位置表已写入", loc != 0)
            offset = loc >> 8
            count = loc & 0xFF
            check("扇区数正确", count >= 1)
            body = raw[offset * 4096:]
            length = struct.unpack_from(">I", body, 0)[0]
            # 官方规范：Length = 1（压缩类型字节）+ 压缩数据长度
            check("长度字段正确（含压缩类型字节）", length == len(data) + 1)
            check("压缩类型 Zlib", body[4] == 2)
            check("数据可解压", zlib.decompress(body[5:5 + length - 1]) == zlib.decompress(data))

        # 重复写入应复用扇区
        ok = asyncio.get_event_loop().run_until_complete(
            writer.save_chunk(1, 2, data, 0)
        )
        check("覆盖写入成功", ok)
        check("扇区被复用", writer.stats.sectors_reused >= 1)

        # 不同维度
        ok = asyncio.get_event_loop().run_until_complete(
            writer.save_chunk(1, 2, data, -1)
        )
        check("下界维度写入", ok)
        check("DIM-1 目录存在", (Path(tmp) / "DIM-1" / "region" / "r.0.0.mca").exists())


def test_extractor():
    print("\n== 区块提取器 ==")
    with tempfile.TemporaryDirectory() as tmp:
        extractor = ChunkExtractor(tmp)
        data = zlib.compress(nbt.build_root({"xPos": nbt.Int(0)}, ""))
        ok = asyncio.get_event_loop().run_until_complete(
            extractor.save_chunk((0, 0, data), 0)
        )
        check("提取器保存", ok)
        check("统计正确", extractor.stats["chunks_saved"] == 1)
        # 重复
        ok = asyncio.get_event_loop().run_until_complete(
            extractor.save_chunk((0, 0, data), 0)
        )
        check("重复保存被跳过", extractor.stats["chunks_saved"] == 1)
        asyncio.get_event_loop().run_until_complete(extractor.shutdown())


if __name__ == "__main__":
    test_nbt_roundtrip()
    test_chunk_packet()
    test_other_packets()
    test_resolver()
    test_state_machine()
    test_region_write()
    test_extractor()

    print(f"\n===== 结果: {PASS} 通过, {FAIL} 失败 =====")
    raise SystemExit(1 if FAIL else 0)
