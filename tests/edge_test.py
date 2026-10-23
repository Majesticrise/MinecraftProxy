"""
边界与压力测试：
  * 直接调色板（bits=15）
  * 光照数组（sky/block）
  * 大区块（多节 + 方块实体）
  * 数据分片到达（跨 TCP 包边界）
  * 畸形数据不得导致代理中断
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
from core.chunk_format import build_chunk_nbt, parse_chunk_data_packet
from core.datatypes import write_string, write_varint
from core.logger import setup_logger
from core.proxy import MinecraftProxy

registry = load_registry(770, '.')

logger = setup_logger(__name__)

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


def pack_bits(values, bits):
    per = 64 // bits
    longs = [0] * math.ceil(len(values) / per)
    for i, v in enumerate(values):
        longs[i // per] |= (v & ((1 << bits) - 1)) << ((i % per) * bits)
    return longs


# ---------------------------------------------------------------- 直接调色板
def test_direct_palette():
    print("\n== 直接调色板（bits=15）==")
    p = bytearray()
    p += struct.pack(">i", 0) + struct.pack(">i", 0)
    p += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 1)
    data = bytearray()
    data += struct.pack(">h", 4096) + struct.pack(">h", 0)
    data += bytes([15])                     # 直接调色板
    for lv in pack_bits([(i * 7) & 0x7FFF for i in range(4096)], 15):
        data += struct.pack(">Q", lv)
    data += bytes([0]) + write_varint(3)     # 单一生物群系
    data += struct.pack(">h", -1) * 23
    p += write_varint(len(data)) + data
    p += write_varint(0)
    p += write_varint(0) * 6

    parsed = parse_chunk_data_packet(bytes(p), section_count=24)
    check("解析成功", parsed is not None)
    if parsed:
        sec = parsed.sections[0]
        check("节非空", sec.block_count == 4096)
        check("直接调色板位宽", sec.block_bits == 15)
        check("直接模式无 palette", sec.block_palette is None,
              f"got {type(sec.block_palette)}")
        check("首值正确", sec.block_states[0] == 0)
        check("次值正确", sec.block_states[1] == 7)

        raw = build_chunk_nbt(parsed, registry, data_version=4325)
        root, end = nbt.parse_named_tag(raw, 0)
        check("NBT 可解析", end == len(raw))
        sec0 = root["sections"][0]
        bs = sec0["block_states"]
        check("直接模式保留无 palette", "palette" not in bs,
              f"keys={list(bs.keys())}")
        check("数据长度按 15bit 计算", len(bs["data"]) == 1024,
              f"got {len(bs['data'])}")


# ------------------------------------------------------------------- 光照
def test_light_arrays():
    print("\n== 光照数组 ==")
    p = bytearray()
    p += struct.pack(">i", 1) + struct.pack(">i", 1)
    p += write_varint(0)                            # 无高度图
    data = struct.pack(">h", -1) * 24
    p += write_varint(len(data)) + data
    p += write_varint(0)                            # 无方块实体
    # 光照: 掩码各 1 个 long, 各有 1 个数组
    p += write_varint(1) + struct.pack(">Q", 0xFFFF)
    p += write_varint(1) + struct.pack(">Q", 0x00FF)
    p += write_varint(0) + write_varint(0)
    # 每个光照数组自带 VarInt 长度前缀（官方格式为 Prefixed Array(2048) of Byte）
    p += write_varint(1) + write_varint(2048) + (bytes([0xAB]) * 2048)
    p += write_varint(1) + write_varint(2048) + (bytes([0xCD]) * 2048)

    parsed = parse_chunk_data_packet(bytes(p), section_count=24)
    check("解析成功", parsed is not None)
    if parsed:
        check("天空光照掩码", parsed.light["sky_light_mask"] == [0xFFFF])
        check("方块光照掩码", parsed.light["block_light_mask"] == [0x00FF])
        check("天空光照数组数", len(parsed.light["sky_light"]) == 1)
        check("天空光照内容", parsed.light["sky_light"][0] == bytes([0xAB]) * 2048)
        check("方块光照内容", parsed.light["block_light"][0] == bytes([0xCD]) * 2048)


def test_light_without_inner_prefix():
    """容错：个别服务端省略光照数组的内层 VarInt 前缀，仍应解析成功"""
    print("\n== 光照数组（无内层前缀，容错）==")
    p = bytearray()
    p += struct.pack(">i", 1) + struct.pack(">i", 1)
    p += write_varint(0)
    data = struct.pack(">h", -1) * 24
    p += write_varint(len(data)) + data
    p += write_varint(0)
    p += write_varint(1) + struct.pack(">Q", 0xFFFF)
    p += write_varint(1) + struct.pack(">Q", 0x00FF)
    p += write_varint(0) + write_varint(0)
    p += write_varint(1) + (bytes([0xAB]) * 2048)   # 省略内层前缀
    p += write_varint(1) + (bytes([0xCD]) * 2048)

    parsed = parse_chunk_data_packet(bytes(p), section_count=24)
    check("解析成功", parsed is not None)
    if parsed:
        check("天空光照内容", parsed.light["sky_light"][0] == bytes([0xAB]) * 2048)
        check("方块光照内容", parsed.light["block_light"][0] == bytes([0xCD]) * 2048)


def test_probe_retry_after_failure():
    """
    结构探测不能把「失败」永久固化。

    同一个 packet_id 在不同时刻负载完全不同（例如同一 id 先出现小包、
    后出现大区块包）。若首次探测失败就不再重试，真正的区块包会被永久漏掉。
    """
    print("\n== 结构探测失败后可重试 ==")
    from core.packet_ids import PacketIdResolver

    resolver = PacketIdResolver()
    resolver.set_protocol_version(770)          # 1.21.5，无精确表 → 纯结构探测
    check("无精确表", not resolver.has_exact_table)

    small = b"\x00\x01"                          # 小包：探测必然失败
    big = big_chunk_payload(9, 9)                # 大包：真实区块包

    check("小包不被误判为区块", resolver.resolve("PLAY", "CLIENTBOUND", 0x2C, small) is None)
    name = resolver.resolve("PLAY", "CLIENTBOUND", 0x2C, big)
    check("同 id 大包仍可识别（重试生效）", name == "Chunk Data and Update Light",
          f"got {name}")


# --------------------------------------------------- 方块实体 NBT 编码
def _chunk_with_block_entity(nbt_bytes):
    """构造一个带 1 个方块实体的区块包（高度图为 Prefixed Array 格式）"""
    p = bytearray()
    p += struct.pack(">i", 0) + struct.pack(">i", 0)
    p += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 0)
    data = struct.pack(">h", -1) * 24
    p += write_varint(len(data)) + data
    p += write_varint(1)                       # 1 个方块实体
    p += bytes([0x00]) + struct.pack(">h", 64) + write_varint(1)
    p += nbt_bytes
    p += write_varint(0) * 6
    return bytes(p)


def test_block_entity_nbt():
    """方块实体 NBT：1.18+ 是匿名可选，1.16/1.17 是具名可选"""
    print("\n== 方块实体 NBT 编码 ==")
    anon = b"\x0a\x00"                          # 匿名空 compound
    named = b"\x0a\x00\x00\x00"                 # 具名空 compound（名字长度 0）

    # 1.18+：匿名
    parsed = parse_chunk_data_packet(
        _chunk_with_block_entity(anon), section_count=24,
        block_entity_anonymous=True,
    )
    check("1.18+ 匿名 NBT 可解析", parsed is not None)
    if parsed:
        check("方块实体数正确", len(parsed.block_entities) == 1)
        check("NBT 内容为空字典", parsed.block_entities[0]["nbt"] == {})

    # 无 NBT（首字节 0 = 不存在）
    parsed = parse_chunk_data_packet(
        _chunk_with_block_entity(b"\x00"), section_count=24,
        block_entity_anonymous=True,
    )
    check("无 NBT 时可解析", parsed is not None)
    if parsed:
        check("无 NBT 时 nbt 为 None", parsed.block_entities[0]["nbt"] is None)

    # 1.16/1.17：具名
    parsed = parse_chunk_data_packet(
        _chunk_with_block_entity(named), section_count=24,
        block_entity_anonymous=False,
    )
    check("1.16/1.17 具名 NBT 可解析", parsed is not None)
    if parsed:
        check("方块实体数正确", len(parsed.block_entities) == 1)


# --------------------------------------------------------- 畸形数据健壮性
def test_malformed():
    print("\n== 畸形数据健壮性 ==")
    cases = [
        b"",
        b"\x00",
        struct.pack(">i", 0) + struct.pack(">i", 0),                # 截断
        struct.pack(">i", 0) + struct.pack(">i", 0) + write_varint(200) + b"\x00" * 10,
        struct.pack(">i", 0) + struct.pack(">i", 0) + write_varint(0)
        + write_varint(1) + write_varint(0xFFFFFFFF),
        b"\xff" * 300,
        bytes(range(256)),
    ]
    ok = True
    for i, c in enumerate(cases):
        try:
            r = parse_chunk_data_packet(c)
            if r is not None:
                # 允许极少数巧合，但必须能构建 NBT 而不抛异常
                build_chunk_nbt(r, registry, data_version=4325)
        except Exception as exc:
            ok = False
            print(f"    案例 {i} 抛异常: {exc}")
    check("畸形数据不抛异常", ok)


# ------------------------------------------------------- 分片到达的集成测试
def frame(packet_id, payload, threshold=-1):
    if threshold < 0:
        body = write_varint(packet_id) + payload
        return write_varint(len(body)) + body
    body = write_varint(packet_id) + payload
    if len(body) >= threshold:
        inner = write_varint(len(body)) + zlib.compress(body)
    else:
        inner = write_varint(0) + body
    return write_varint(len(inner)) + inner


def big_chunk_payload(cx, cz):
    p = bytearray()
    p += struct.pack(">i", cx) + struct.pack(">i", cz)
    p += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 0xFF)
    data = bytearray()
    for _ in range(24):
        data += struct.pack(">h", 100) + struct.pack(">h", 5)
        data += bytes([4]) + write_varint(4)
        for v in range(4):
            data += write_varint(v)
        for lv in pack_bits([i % 4 for i in range(4096)], 4):
            data += struct.pack(">Q", lv)
        data += bytes([0]) + write_varint(1)
    p += write_varint(len(data)) + data
    p += write_varint(0)
    p += write_varint(0) * 6
    return bytes(p)


async def fake_server(reader, writer, received):
    from core.varint import read_packet
    buf = bytearray()
    while True:
        buf.extend(await asyncio.wait_for(reader.readexactly(1), timeout=5))
        pid, payload, size = read_packet(buf, 0, -1)
        if pid != -1 and size:
            break

    writer.write(frame(0x03, write_varint(16), -1))
    await writer.drain()
    writer.write(frame(0x02, bytes(range(16)) + write_string("Big") + write_varint(0), 16))
    await writer.drain()

    await read_one(reader, 16, received, "login_ack")
    writer.write(frame(0x03, b"", 16))
    await writer.drain()
    await read_one(reader, 16, received, "config_ack")

    # 大区块：拆成很多小片发送，模拟 TCP 分片
    payload = big_chunk_payload(0, 0)
    received["big"] = payload
    full = frame(0x2E, payload, 16)
    for i in range(0, len(full), 7):
        writer.write(full[i:i + 7])
        await writer.drain()
        await asyncio.sleep(0.001)

    await asyncio.sleep(0.6)
    writer.close()


async def read_one(reader, threshold, received, key):
    from core.varint import read_packet
    buf = bytearray()
    while True:
        data = await asyncio.wait_for(reader.read(4096), timeout=5)
        if not data:
            return
        buf.extend(data)
        pid, payload, size = read_packet(buf, 0, threshold)
        if pid != -1 and size:
            received[key] = (pid, payload)
            return


async def fake_client(port, forwarded):
    from core.varint import read_packet
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    hs = write_varint(777) + write_string("x") + struct.pack(">H", 25565) + write_varint(2)
    writer.write(frame(0x00, hs, -1))
    await writer.drain()

    buf = bytearray()
    got = 0
    while got < 2:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not data:
            break
        buf.extend(data)
        while True:
            pid, payload, size = read_packet(buf, 0, -1 if got == 0 else 16)
            if pid == -1 or not size:
                break
            forwarded.append(bytes(buf[:size]))
            buf = buf[size:]
            got += 1

    writer.write(frame(0x03, b"", 16))
    await writer.drain()
    buf = bytearray()
    while True:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not data:
            break
        buf.extend(data)
        pid, payload, size = read_packet(buf, 0, 16)
        if pid != -1 and size:
            forwarded.append(bytes(buf[:size]))
            break
    writer.write(frame(0x03, b"", 16))
    await writer.drain()

    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + 3
    while asyncio.get_event_loop().time() < deadline:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=0.8)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        buf.extend(data)
        while True:
            pid, payload, size = read_packet(buf, 0, 16)
            if pid == -1 or not size:
                break
            forwarded.append(bytes(buf[:size]))
            buf = buf[size:]

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def test_fragmented():
    print("\n== TCP 分片到达 ==")
    with tempfile.TemporaryDirectory() as tmp:
        received = {}
        forwarded = []
        server = await asyncio.start_server(
            lambda r, w: fake_server(r, w, received), "127.0.0.1", 0
        )
        target_port = server.sockets[0].getsockname()[1]

        proxy = MinecraftProxy(
            local_host="127.0.0.1", local_port=0,
            target_host="127.0.0.1", target_port=target_port,
            world_dir=tmp, debug=False,
        )
        await proxy.start()
        await asyncio.wait_for(fake_client(proxy.port, forwarded), timeout=25)

        for _ in range(60):
            await asyncio.sleep(0.05)
            if list(Path(tmp).rglob("*.mca")):
                break
        await asyncio.sleep(0.3)

        region = Path(tmp) / "region" / "r.0.0.mca"
        check("分片大区块落盘", region.exists(), str(list(Path(tmp).rglob("*.mca"))))

        if region.exists() and "big" in received:
            expected = frame(0x2E, received["big"], 16)
            check("分片帧字节一致", any(f == expected for f in forwarded))

            raw = region.read_bytes()
            loc = struct.unpack_from(">I", raw, 0)[0]
            check("位置表非空", loc != 0)
            if loc:
                body = raw[(loc >> 8) * 4096:]
                ln = struct.unpack_from(">I", body, 0)[0]
                root, _ = nbt.parse_named_tag(zlib.decompress(body[5:5 + ln - 1]), 0)
                check("24 节全部写入", len(root["sections"]) == 24,
                      f"got {len(root['sections'])}")
                check("节 Y 序列（最低节 Y = -4）",
                      [s["Y"] for s in root["sections"]] == list(range(-4, 20)),
                      f"got {[s['Y'] for s in root['sections']][:3]}...")

        await proxy.shutdown()
        server.close()
        await server.wait_closed()


async def main():
    test_direct_palette()
    test_light_arrays()
    test_light_without_inner_prefix()
    test_probe_retry_after_failure()
    test_block_entity_nbt()
    test_malformed()
    await test_fragmented()
    print(f"\n===== 边界测试: {PASS} 通过, {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
