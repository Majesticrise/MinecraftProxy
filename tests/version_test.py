"""
版本自适应测试：对没有精确 ID 表的协议版本（例如 770 / 9999）
验证纯结构探测路径依然能完成完整流程。

与 e2e_test 的区别：使用非 777 协议版本，迫使解析器走探测分支。
"""

import os
import sys

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import struct
import tempfile
from pathlib import Path

import core.nbt as nbt
from core.block_registry import load_registry
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


def frame(packet_id, payload, threshold=-1):
    import zlib
    if threshold < 0:
        body = write_varint(packet_id) + payload
        return write_varint(len(body)) + body
    body = write_varint(packet_id) + payload
    if len(body) >= threshold:
        inner = write_varint(len(body)) + zlib.compress(body)
    else:
        inner = write_varint(0) + body
    return write_varint(len(inner)) + inner


def build_chunk_payload(cx, cz):
    p = bytearray()
    p += struct.pack(">i", cx) + struct.pack(">i", cz)
    p += write_varint(1) + write_varint(4) + write_varint(1) + struct.pack(">Q", 0xBB)
    # 单值调色板节
    data = bytearray()
    data += struct.pack(">h", 5) + struct.pack(">h", 0)
    data += bytes([0]) + write_varint(7)      # blocks: single value
    data += bytes([0]) + write_varint(2)      # biomes: single value
    data += struct.pack(">h", -1) * 23
    p += write_varint(len(data)) + data
    p += write_varint(0)                      # block entities
    p += write_varint(0) * 6                  # light
    return bytes(p)


async def fake_server(reader, writer, received, protocol):
    # 读取握手
    from core.varint import read_packet
    buf = bytearray()
    while True:
        buf.extend(await asyncio.wait_for(reader.readexactly(1), timeout=5))
        pid, payload, size = read_packet(buf, 0, -1)
        if pid != -1 and size:
            break
    received["handshake_payload"] = payload

    # Set Compression 探测：单个 VarInt
    writer.write(frame(0x03, write_varint(32), -1))
    await writer.drain()

    # Login Success 探测：UUID + name
    writer.write(frame(0x02, bytes(range(16)) + write_string("Prober") + write_varint(0), 32))
    await writer.drain()

    await read_one(reader, 32, received, "login_ack")

    # 配置阶段空包（Finish Configuration 探测）
    writer.write(frame(0x03, b"", 32))
    await writer.drain()
    await read_one(reader, 32, received, "config_ack")

    # 区块包（探测）
    payload = build_chunk_payload(11, 22)
    received["chunk_payload"] = payload
    writer.write(frame(0x2E, payload, 32))
    await writer.drain()

    await asyncio.sleep(0.5)
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


async def fake_client(port, protocol, forwarded):
    from core.varint import read_packet
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    hs = write_varint(protocol) + write_string("probe.example.com") + struct.pack(">H", 25565) + write_varint(2)
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
            pid, payload, size = read_packet(buf, 0, -1 if got == 0 else 32)
            if pid == -1 or not size:
                break
            forwarded.append(bytes(buf[:size]))
            buf = buf[size:]
            got += 1

    writer.write(frame(0x03, b"", 32))
    await writer.drain()

    buf = bytearray()
    while True:
        data = await asyncio.wait_for(reader.read(65536), timeout=5)
        if not data:
            break
        buf.extend(data)
        pid, payload, size = read_packet(buf, 0, 32)
        if pid != -1 and size:
            forwarded.append(bytes(buf[:size]))
            break

    writer.write(frame(0x03, b"", 32))
    await writer.drain()

    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + 2
    while asyncio.get_event_loop().time() < deadline:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=0.7)
        except asyncio.TimeoutError:
            break
        if not data:
            break
        buf.extend(data)
        while True:
            pid, payload, size = read_packet(buf, 0, 32)
            if pid == -1 or not size:
                break
            forwarded.append(bytes(buf[:size]))
            buf = buf[size:]

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def run_case(protocol):
    print(f"\n-- 协议版本 {protocol}（无精确 ID 表，纯探测）--")
    with tempfile.TemporaryDirectory() as tmp:
        received = {}
        forwarded = []

        server = await asyncio.start_server(
            lambda r, w: fake_server(r, w, received, protocol), "127.0.0.1", 0
        )
        target_port = server.sockets[0].getsockname()[1]

        proxy = MinecraftProxy(
            local_host="127.0.0.1", local_port=0,
            target_host="127.0.0.1", target_port=target_port,
            world_dir=tmp, debug=False,
            allow_download=False,
        )
        await proxy.start()
        await asyncio.wait_for(fake_client(proxy.port, protocol, forwarded), timeout=20)

        for _ in range(40):
            await asyncio.sleep(0.05)
            if list(Path(tmp).rglob("*.mca")):
                break
        await asyncio.sleep(0.3)

        check(f"[{protocol}] 握手被解析", "handshake_payload" in received)

        region = Path(tmp) / "region" / "r.0.0.mca"
        check(f"[{protocol}] 区块落盘成功", region.exists(),
              str(list(Path(tmp).rglob("*.mca"))))

        if region.exists() and "chunk_payload" in received:
            expected = frame(0x2E, received["chunk_payload"], 32)
            check(f"[{protocol}] 字节透明转发", any(f == expected for f in forwarded))

            raw = region.read_bytes()
            idx = (11 % 32) + ((22 % 32) * 32)
            loc = struct.unpack_from(">I", raw, idx * 4)[0]
            check(f"[{protocol}] 位置表正确", loc != 0)
            if loc:
                import zlib
                body = raw[(loc >> 8) * 4096:]
                ln = struct.unpack_from(">I", body, 0)[0]
                root, _ = nbt.parse_named_tag(zlib.decompress(body[5:5 + ln - 1]), 0)
                check(f"[{protocol}] xPos/zPos 正确",
                      root["xPos"] == 11 and root["zPos"] == 22)

        await proxy.shutdown()
        server.close()
        await server.wait_closed()


async def main():
    print("== 版本自适应测试（结构探测路径）==")
    for proto in (770, 9999):
        await run_case(proto)
    print(f"\n===== 自适应测试: {PASS} 通过, {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
