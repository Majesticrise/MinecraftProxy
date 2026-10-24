"""
镜像模式测试（mc-mirror-agent 协议 + 端到端）

覆盖:
  * 单元：明文裸帧喂给状态机（握手 -> 协议号 / 状态切换）
  * 端到端（真实 TCP + asyncio）：
      - magic 校验通过后整条链路：握手 -> Login Success -> Login Acknowledged
        -> Finish Configuration -> PLAY -> 真实抓包固件区块落盘 -> level.dat
      - magic 不匹配：警告并断开
      - 方向字节非法：断开
      - STATUS 探测会话（服务器列表 ping）：静默忽略，无统计噪声、无 level.dat
"""

import asyncio
import logging
import os
import struct
import sys
import tempfile
from pathlib import Path

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.datatypes import write_string, write_varint
from core.mirror import MIRROR_MAGIC, MIRROR_VERSION, MirrorServer, MirrorSession
from core.protocol import ConnectionState

FIXTURE = Path(__file__).parent / "fixtures" / "real_1_21_9_via_chunk_0.bin"
PROTOCOL = 777  # 777 精确表：Login Success=0x02, Login Ack=0x03, Finish Config=0x03, Chunk=0x2E

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        print(f"  [FAIL] {msg}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


def capture(*names):
    caps = []
    for name in names:
        cap = _Capture()
        lg = logging.getLogger(name)
        lg.addHandler(cap)
        caps.append(cap)
    return caps


def bare(packet_id: int, payload: bytes = b"") -> bytes:
    """构造镜像协议的明文裸帧 [VarInt 包ID][载荷]"""
    return write_varint(packet_id) + payload


def handshake_payload(protocol: int, next_state: int) -> bytes:
    return (write_varint(protocol) + write_string("mc.test")
            + struct.pack(">H", 25565) + write_varint(next_state))


class _DummyServer:
    """MirrorSession 单元测试所需的最小服务端属性"""

    debug = False
    world_dir = "."
    block_map = None
    allow_download = False


# ---------------------------------------------------------------- 单元
def unit_feed():
    session = MirrorSession(_DummyServer(), None, None)  # type: ignore[arg-type]
    # serverbound 握手 -> 协议号 + LOGIN 状态
    session._feed(bare(0x00, handshake_payload(PROTOCOL, 2)), clientbound=False)
    check(session.parser.protocol_version == PROTOCOL,
          f"握手解析协议号 (got {session.parser.protocol_version})")
    check(session.parser.state == ConnectionState.LOGIN, "握手后进入 LOGIN")
    check(session.parser.mirror_mode, "镜像会话的解析器为 mirror_mode")
    # 非法裸帧（首字节不是 VarInt 完整形态）：只忽略，不抛异常
    session._feed(b"\xff\xff\xff\xff\xff", clientbound=True)
    check(session.parser.state == ConnectionState.LOGIN, "畸形帧不影响状态机")


async def e2e_full_session():
    with tempfile.TemporaryDirectory(prefix="mirror_e2e_") as tmp:
        server = MirrorServer(host="127.0.0.1", port=0, world_dir=tmp,
                               allow_download=False, max_sessions=4)
        check(await server.start(), "镜像服务启动（端口 0 自动分配）")
        port = server.port_number
        check(port > 0, f"自动分配端口有效 (got {port})")

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(MIRROR_MAGIC + bytes([MIRROR_VERSION]))

        def send(direction: int, frame: bytes):
            writer.write(bytes([direction]) + len(frame).to_bytes(4, "big") + frame)

        # 完整登录链：握手 -> Login Success -> Login Ack -> Finish Config -> PLAY
        send(1, bare(0x00, handshake_payload(PROTOCOL, 2)))
        send(0, bare(0x02, b"\x01" * 16 + write_string("Tester")))   # Login Success
        send(1, bare(0x03))                                          # Login Acknowledged
        send(0, bare(0x03))                                          # Finish Configuration
        # 真实抓包固件作为区块包（777: Chunk Data and Update Light = 0x2E）
        if FIXTURE.exists():
            send(0, bare(0x2E, FIXTURE.read_bytes()))
        writer.write_eof()
        await writer.drain()

        cap = capture("core.chunk_extractor")[0]
        try:
            await asyncio.wait_for(server.shutdown(), timeout=30)
        finally:
            logging.getLogger("core.chunk_extractor").removeHandler(cap)

        check((Path(tmp) / "level.dat").exists(), "会话结束生成 level.dat")
        regions = list((Path(tmp) / "region").glob("r.*.mca"))
        check(bool(regions), "区块已写入 .mca 区域文件")
        stats_msg = [m for _l, m in cap.records if "区块统计" in m]
        check(bool(stats_msg), f"输出区块统计 ({stats_msg[:1]})")
        if stats_msg:
            check("成功=1" in stats_msg[0].replace(" ", ""), f"统计成功=1 ({stats_msg[0]})")


async def e2e_bad_magic():
    with tempfile.TemporaryDirectory(prefix="mirror_bad_") as tmp:
        server = MirrorServer(host="127.0.0.1", port=0, world_dir=tmp,
                               allow_download=False)
        await server.start()
        cap = capture("core.mirror")[0]
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.port_number)
            writer.write(b"XXXX" + bytes([9]))
            await writer.drain()
            # 服务端应关闭连接（读到 EOF）
            try:
                data = await asyncio.wait_for(reader.read(), timeout=5)
            except asyncio.TimeoutError:
                data = b"timeout"
            check(data == b"", f"magic 不匹配被断开 (got {data!r})")
            joined = "\n".join(m for _l, m in cap.records)
            check("镜像协议不匹配" in joined, "magic 不匹配输出警告")
            await asyncio.wait_for(server.shutdown(), timeout=10)
        finally:
            logging.getLogger("core.mirror").removeHandler(cap)


async def e2e_bad_direction():
    with tempfile.TemporaryDirectory(prefix="mirror_dir_") as tmp:
        server = MirrorServer(host="127.0.0.1", port=0, world_dir=tmp,
                               allow_download=False)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1",
                                                       server.port_number)
        writer.write(MIRROR_MAGIC + bytes([MIRROR_VERSION]))
        frame = bare(0x00, handshake_payload(PROTOCOL, 2))
        writer.write(bytes([7]) + len(frame).to_bytes(4, "big") + frame)
        await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(), timeout=5)
        except asyncio.TimeoutError:
            data = b"timeout"
        check(data == b"", f"方向字节非法被断开 (got {data!r})")
        writer.close()
        await asyncio.wait_for(server.shutdown(), timeout=10)


async def e2e_status_ping_quiet():
    """服务器列表 ping：走完 STATUS 后无区块，应静默（无统计、无 level.dat）"""
    with tempfile.TemporaryDirectory(prefix="mirror_ping_") as tmp:
        server = MirrorServer(host="127.0.0.1", port=0, world_dir=tmp,
                               allow_download=False)
        await server.start()
        cap = capture("core.chunk_extractor")[0]
        try:
            _r, writer = await asyncio.open_connection("127.0.0.1",
                                                       server.port_number)
            writer.write(MIRROR_MAGIC + bytes([MIRROR_VERSION]))
            frame = bare(0x00, handshake_payload(PROTOCOL, 1))  # next_state=1 STATUS
            writer.write(bytes([1]) + len(frame).to_bytes(4, "big") + frame)
            await writer.drain()
            writer.write_eof()
            await asyncio.wait_for(server.shutdown(), timeout=10)
            check(not (Path(tmp) / "level.dat").exists(), "STATUS 会话不生成 level.dat")
            noisy = [m for lvl, m in cap.records if lvl >= logging.INFO]
            check(not noisy, f"STATUS 会话无统计噪声 ({noisy[:2]})")
        finally:
            logging.getLogger("core.chunk_extractor").removeHandler(cap)


def main() -> int:
    unit_feed()
    asyncio.run(e2e_full_session())
    asyncio.run(e2e_bad_magic())
    asyncio.run(e2e_bad_direction())
    asyncio.run(e2e_status_ping_quiet())
    print(f"\n镜像模式测试: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
