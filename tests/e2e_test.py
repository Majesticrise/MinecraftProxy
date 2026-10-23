"""
端到端集成测试：真实 TCP 连接
  伪客户端 <-> 代理 <-> 伪服务端

验证:
  1. 握手包地址被改写为真实目标
  2. 压缩帧（Set Compression 之后）被原样转发，字节完全一致
  3. 状态机正确流转 LOGIN -> CONFIGURATION -> PLAY
  4. 区块包被解析并写入 .mca 区域文件
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
from core.datatypes import write_string, write_varint
from core.logger import setup_logger
from core.proxy import MinecraftProxy

registry = load_registry(770, '.')

logger = setup_logger(__name__)

PROTOCOL = 777

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


# ---------------------------------------------------------------- 帧工具
def frame(packet_id, payload, threshold=-1):
    """按当前压缩阈值封装一个数据包"""
    if threshold < 0:
        body = write_varint(packet_id) + payload
        return write_varint(len(body)) + body
    body = write_varint(packet_id) + payload
    if len(body) >= threshold:
        compressed = zlib.compress(body)
        inner = write_varint(len(body)) + compressed
    else:
        inner = write_varint(0) + body
    return write_varint(len(inner)) + inner


def pack_bits(values, bits):
    per_long = 64 // bits
    longs = [0] * math.ceil(len(values) / per_long)
    for i, v in enumerate(values):
        longs[i // per_long] |= (v & ((1 << bits) - 1)) << ((i % per_long) * bits)
    return longs


def section(palette, bits=4):
    out = bytearray()
    out += struct.pack(">h", 10)
    out += struct.pack(">h", 0)
    out += bytes([bits])
    out += write_varint(len(palette))
    for v in palette:
        out += write_varint(v)
    for lv in pack_bits([i % len(palette) for i in range(4096)], bits):
        out += struct.pack(">Q", lv)
    out += bytes([0])
    out += write_varint(1)
    return bytes(out)


def build_chunk_payload(cx, cz, be=None):
    p = bytearray()
    p += struct.pack(">i", cx) + struct.pack(">i", cz)
    p += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 0xAA)
    data = section([0, 1, 2, 3]) + struct.pack(">h", -1) * 23
    p += write_varint(len(data)) + data
    if be:
        p += write_varint(1)
        p += bytes([(be[0] << 4) | be[1]]) + struct.pack(">h", be[2])
        p += write_varint(1)
        p += nbt.build_root({"id": nbt.String("minecraft:chest")}, "")
    else:
        p += write_varint(0)
    p += write_varint(0) * 6
    return bytes(p)


def build_block_entity_payload(x, y, z):
    pos = ((x & 0x3FFFFFF) << 38) | (y & 0xFFF) | ((z & 0x3FFFFFF) << 12)
    return (
        struct.pack(">Q", pos)
        + write_varint(1)
        + nbt.build_root({"id": nbt.String("minecraft:furnace")}, "")
    )


def build_login_play(dimension="minecraft:overworld"):
    p = bytearray()
    p += struct.pack(">i", 42)          # entity id
    p += bytes([0])                      # hardcore
    p += write_varint(1) + write_string("minecraft:overworld")  # worlds
    p += write_varint(20)                # max players
    p += write_varint(10)                # view distance
    p += write_varint(10)                # simulation distance
    p += bytes([0, 0, 0])                # reduced debug / respawn screen / limited crafting
    p += write_varint(0)                 # dimension type (registry id)
    p += write_string(dimension)         # dimension name
    return bytes(p)


# ---------------------------------------------------------------- 伪服务端
async def fake_server(reader, writer, received):
    """伪 Minecraft 服务端，发送一串固定的数据包"""
    handshake = await asyncio.wait_for(reader.readexactly(1), timeout=5)

    # 读取完整握手包
    buf = bytearray(handshake)
    while True:
        try:
            from core.varint import read_packet
            pid, payload, size = read_packet(buf, 0, -1)
            if pid != -1 and size:
                break
        except Exception:
            pass
        buf.extend(await asyncio.wait_for(reader.readexactly(1), timeout=5))

    received["handshake"] = bytes(buf[:size])
    received["handshake_payload"] = payload

    # 1) Set Compression (threshold=64)
    writer.write(frame(0x03, write_varint(64), -1))
    await writer.drain()

    # 2) Login Success (未压缩 -> 阈值以下）
    login_success = bytes(range(16)) + write_string("Tester") + write_varint(0)
    writer.write(frame(0x02, login_success, 64))
    await writer.drain()

    # 3) 等待 Login Acknowledged (serverbound 0x03, 空)
    await read_one_packet(reader, 64, received, "login_ack")

    # 4) Finish Configuration (clientbound 0x03, 空)
    writer.write(frame(0x03, b"", 64))
    await writer.drain()

    # 5) 等待 Acknowledge Configuration
    await read_one_packet(reader, 64, received, "config_ack")

    # 6) Login (play) 0x32
    writer.write(frame(0x32, build_login_play(), 64))
    await writer.drain()

    # 7) Block Entity Data 0x06 (chunk 3,-2, 局部 5,9)
    writer.write(frame(0x06, build_block_entity_payload(3 * 16 + 5, 64, -2 * 16 + 9), 64))
    await writer.drain()

    # 8) Chunk Data 0x2E (chunk 3,-2)，无内嵌方块实体
    chunk_payload = build_chunk_payload(3, -2)
    received["chunk_payload"] = chunk_payload
    writer.write(frame(0x2E, chunk_payload, 64))
    await writer.drain()

    # 9) 再来一个区块，验证多次写入
    writer.write(frame(0x2E, build_chunk_payload(4, -2), 64))
    await writer.drain()

    await asyncio.sleep(0.4)
    writer.close()


async def read_one_packet(reader, threshold, received, key):
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


# ---------------------------------------------------------------- 伪客户端
async def fake_client(port, forwarded):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    hs = write_varint(PROTOCOL) + write_string("play.example.com") + struct.pack(">H", 25565) + write_varint(2)
    writer.write(frame(0x00, hs, -1))
    await writer.drain()

    # 读取 Set Compression / Login Success
    from core.varint import read_packet

    buf = bytearray()
    got = 0
    while got < 2:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not data:
            break
        buf.extend(data)
        while True:
            pid, payload, size = read_packet(buf, 0, -1 if got == 0 else 64)
            if pid == -1 or not size:
                break
            forwarded.append(bytes(buf[:size]))
            buf = buf[size:]
            got += 1

    # Login Acknowledged（压缩已启用，必须使用压缩帧格式）
    writer.write(frame(0x03, b"", 64))
    await writer.drain()

    # 等待 Finish Configuration
    buf = bytearray()
    while True:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not data:
            break
        buf.extend(data)
        pid, payload, size = read_packet(buf, 0, 64)
        if pid != -1 and size:
            forwarded.append(bytes(buf[:size]))
            break

    # Acknowledge Configuration
    writer.write(frame(0x03, b"", 64))
    await writer.drain()

    # 接收剩余所有包（Login play + BE + 2 chunks）
    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + 3
    while asyncio.get_event_loop().time() < deadline:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=1)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        buf.extend(data)
        while True:
            pid, payload, size = read_packet(buf, 0, 64)
            if pid == -1 or not size:
                break
            forwarded.append(bytes(buf[:size]))
            buf = buf[size:]

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def main():
    print("\n== 端到端集成测试 ==")
    with tempfile.TemporaryDirectory() as tmp:
        received = {}
        forwarded = []

        server = await asyncio.start_server(
            lambda r, w: fake_server(r, w, received), "127.0.0.1", 0
        )
        target_port = server.sockets[0].getsockname()[1]

        proxy = MinecraftProxy(
            local_host="127.0.0.1",
            local_port=0,
            target_host="127.0.0.1",
            target_port=target_port,
            world_dir=tmp,
            debug=False,
        )
        # 使用固定端口以便客户端连接
        proxy_holder = {}

        await proxy.start()
        proxy_port = proxy.port
        check("代理已启动", proxy_port > 0, f"port={proxy_port}")

        await asyncio.wait_for(fake_client(proxy_port, forwarded), timeout=20)

        # 给区块写盘留时间
        for _ in range(40):
            await asyncio.sleep(0.05)
            if len(list(Path(tmp).rglob("*.mca"))) > 0:
                break
        await asyncio.sleep(0.3)

        # --- 断言 ---
        check("收到握手包", "handshake_payload" in received)
        if "handshake_payload" in received:
            from core.datatypes import PacketBuffer
            b = PacketBuffer(received["handshake_payload"])
            ver = b.read_varint()
            addr = b.read_string()
            port = b.read_ushort()
            nxt = b.read_varint()
            check("协议版本保留", ver == PROTOCOL, f"got {ver}")
            check("地址被改写为真实目标", addr == "127.0.0.1", f"got {addr}")
            check("端口被改写", port == target_port, f"got {port}")
            check("next_state 保留", nxt == 2, f"got {nxt}")

        check("客户端收到 Login Acknowledged 之后的数据", len(forwarded) >= 3, f"n={len(forwarded)}")

        # 字节一致性：比对转发出来的区块帧与伪服务端发送的完全一致
        if "chunk_payload" in received and forwarded:
            expected = frame(0x2E, received["chunk_payload"], 64)
            matched = any(f == expected for f in forwarded)
            check("区块帧字节完全一致（压缩帧透明转发）", matched)

        # 落盘验证
        region = Path(tmp) / "region" / "r.0.-1.mca"   # chunk (3,-2) 与 (4,-2)
        check("区域文件已生成", region.exists(), str(list(Path(tmp).rglob("*.mca"))))
        if region.exists():
            raw = region.read_bytes()
            idx1 = (3 % 32) + ((-2 % 32) * 32)
            idx2 = (4 % 32) + ((-2 % 32) * 32)
            loc1 = struct.unpack_from(">I", raw, idx1 * 4)[0]
            loc2 = struct.unpack_from(">I", raw, idx2 * 4)[0]
            check("区块 (3,-2) 已写入", loc1 != 0)
            check("区块 (4,-2) 已写入", loc2 != 0)

            if loc1:
                off = (loc1 >> 8) * 4096
                body = raw[off:]
                ln = struct.unpack_from(">I", body, 0)[0]
                check("压缩类型 Zlib", body[4] == 2)
                root, end = nbt.parse_named_tag(zlib.decompress(body[5:5 + ln - 1]), 0)
                check("NBT 可解析", end > 0)
                check("xPos", root["xPos"] == 3)
                check("zPos", root["zPos"] == -2)
                be = root.get("block_entities")
                check("方块实体已合并（先发 BE 后发区块）", be is not None and len(be) == 1,
                      f"got {be}")
                if be:
                    check("方块实体世界坐标",
                          (be[0]["x"], be[0]["z"]) == (3 * 16 + 5, -2 * 16 + 9),
                          f"got {be[0]}")

        # 侧信道文件
        side = Path(tmp) / "region" / "chunk_3_-2_block_entities.json"
        check("方块实体侧信道文件", side.exists(), str(list(Path(tmp).rglob("*.json"))))

        await proxy.shutdown()
        server.close()
        await server.wait_closed()

    print(f"\n===== 集成测试: {PASS} 通过, {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
