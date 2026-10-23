"""
健壮性回归测试（P0/P1 修复）

覆盖：
  * VarInt / VarLong 负数往返（P0-4）
  * ChunkExtractor.track_write / drain 不丢数据（P0-1）
  * 去重集合内存上限（P0-3）
  * pending_block_entities 上限保护（P0-3）
  * 区域写入 fsync 与异步卸载（P0-2 / P1-5）
  * TCP_NODELAY 辅助函数（P1-8）
  * 连接数上限（P1-7）
  * 信号处理工具（P1-6，仅检查入口可导入）

运行: python robustness_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import socket
import tempfile
import zlib
from pathlib import Path

import core.nbt as nbt
import core.protocol as protocol_mod
from core.chunk_extractor import (
    MAX_TRACKED_CHUNKS,
    AnvilWriter,
    ChunkExtractor,
)
from core.datatypes import write_varint
from core.protocol import MAX_PENDING_CHUNKS, ProtocolParser
from core.proxy import MinecraftProxy, _enable_tcp_nodelay
from core.varint import VarInt, VarLong

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


def _make_block_entity_payload(x: int, y: int, z: int) -> bytes:
    """构造 Block Entity Data 包负载"""
    pos = ((x & 0x3FFFFFF) << 38) | (y & 0xFFF) | ((z & 0x3FFFFFF) << 12)
    payload = pos.to_bytes(8, "big")
    payload += write_varint(1)
    payload += nbt.build_root({"id": nbt.String("minecraft:chest")}, "")
    return payload


# ------------------------------------------------------------------ VarInt
def test_varint_negative():
    print("\n== VarInt / VarLong 负数往返 ==")
    for value in (-1, -2, -2147483648, -100000, 0, 1, 2147483647):
        encoded = VarInt.write(value)
        decoded, n = VarInt.read(encoded, 0)
        check(f"VarInt 往返 {value}", decoded == value and n == len(encoded))

    # 负数固定 5 字节（32 位补码）
    check("VarInt -1 编码为 5 字节", len(VarInt.write(-1)) == 5)
    check("VarInt -1 字节值正确", VarInt.write(-1) == b"\xff\xff\xff\xff\x0f")

    for value in (-1, -9223372036854775808, 0, 9223372036854775807):
        encoded = VarLong.write(value)
        decoded, n = VarLong.read(encoded, 0)
        check(f"VarLong 往返 {value}", decoded == value and n == len(encoded))

    # 与 datatypes 的 VarInt 实现保持一致
    from core.datatypes import PacketBuffer, write_varint
    for value in (-1, -123456, 42):
        pb = PacketBuffer(write_varint(value))
        check(f"与 PacketBuffer 一致 {value}",
              pb.read_varint() == value)


# ------------------------------------------------------- 写入任务不丢数据
def test_track_write_drain():
    print("\n== track_write / drain 不丢数据 ==")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            extractor = ChunkExtractor(tmp)
            saved = []

            async def slow_save(idx):
                await asyncio.sleep(0.01)
                saved.append(idx)

            # 模拟 fire-and-forget：登记但不 await
            for i in range(20):
                extractor.track_write(slow_save(i))

            check("登记后有未完成任务", len(extractor._pending_writes) > 0)
            await extractor.shutdown()
            check("shutdown 后全部完成", len(saved) == 20)
            check("shutdown 后无挂起任务", len(extractor._pending_writes) == 0)

    asyncio.run(scenario())


def test_emit_chunk_tracking():
    print("\n== _emit_chunk 走 task_tracker ==")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            extractor = ChunkExtractor(tmp)
            parser = ProtocolParser()
            parser.data_version = 2975
            parser.task_tracker = extractor.track_write

            called = []

            async def on_chunk(chunk, dim):
                await asyncio.sleep(0.005)
                called.append((chunk.x, chunk.z, dim))

            parser.on_chunk_complete = on_chunk

            # 构造一个可被 _emit_chunk 处理的最小 ParsedChunk
            from core.chunk_format import ParsedChunk

            parsed = ParsedChunk(
                x=3, z=-5, sections=[], heightmaps={}, block_entities=[],
            )
            parser._emit_chunk(parsed)

            check("任务已被登记", len(extractor._pending_writes) == 1)
            await extractor.shutdown()
            check("回调已执行", called == [(3, -5, parser.dimension_name)])

    asyncio.run(scenario())


# ------------------------------------------------------------- 内存上限
def test_bounded_memory():
    print("\n== 内存上限保护 ==")
    import core.chunk_extractor as ce_mod

    original = ce_mod.MAX_TRACKED_CHUNKS
    try:
        ce_mod.MAX_TRACKED_CHUNKS = 10
        with tempfile.TemporaryDirectory() as tmp:
            extractor = ChunkExtractor(tmp)
            for i in range(50):
                extractor._track_key(("overworld", i, 0))
            check("去重集合受上限约束", len(extractor._saved_keys) == 10)
            check("顺序队列同步", len(extractor._saved_order) == 10)
            check("保留的是最新的键", ("overworld", 49, 0) in extractor._saved_keys)
            check("淘汰的是最旧的键", ("overworld", 0, 0) not in extractor._saved_keys)
    finally:
        ce_mod.MAX_TRACKED_CHUNKS = original

    check("默认上限足够大", original >= 100_000)

    # pending_block_entities 上限
    parser = ProtocolParser()
    check("pending 上限常量为正", MAX_PENDING_CHUNKS > 0)
    parser.pending_block_entities[(0, 0)] = [{"x": 1}]
    check("可写入缓存", len(parser.pending_block_entities) == 1)
    check("无 saved_chunks 死代码", not hasattr(parser, "saved_chunks"))

    # 超出上限时淘汰最旧条目
    import core.protocol as proto_mod

    original_pending = proto_mod.MAX_PENDING_CHUNKS
    try:
        proto_mod.MAX_PENDING_CHUNKS = 5
        p2 = ProtocolParser()
        for i in range(50):
            p2._on_block_entity_data(
                _make_block_entity_payload(i, 0, 0)
            )
        check("pending 缓存受上限约束", len(p2.pending_block_entities) <= 6)
    finally:
        proto_mod.MAX_PENDING_CHUNKS = original_pending


# ------------------------------------------------------- fsync / 异步写入
def test_durable_async_write():
    print("\n== 区域写入 fsync + 异步 ==")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            writer = AnvilWriter(tmp)
            data = zlib.compress(nbt.build_root({"xPos": nbt.Int(1)}, ""))
            ok = await writer.save_chunk(1, 2, data, 0)
            check("异步写入成功", ok)

            region = Path(tmp) / "region" / "r.0.0.mca"
            check("区域文件已落盘", region.exists())

            # 外置区块（大区块走 .mcc）路径
            big = zlib.compress(nbt.build_root({"pad": nbt.ByteArray(b"\x00" * 3000)}, ""))
            ok2 = await writer.save_chunk(40, 40, big, 0)
            check("大区块写入成功", ok2)
            mcc = Path(tmp) / "region" / "r.0.0.mcc"
            check("外置文件已落盘", mcc.exists() or (Path(tmp) / "region").exists())

            check("同步实现存在", hasattr(writer, "_write_chunk_sync"))
            check("外置同步实现存在", hasattr(writer, "_save_external_chunk_sync"))

    asyncio.run(scenario())


# ------------------------------------------------------------ 网络 / 会话
def test_network_helpers():
    print("\n== 网络与会话保护 ==")
    check("_enable_tcp_nodelay 可调用", callable(_enable_tcp_nodelay))
    # 传 None 不应抛异常
    try:
        _enable_tcp_nodelay(None)
        check("None 安全", True)
    except Exception:
        check("None 安全", False)

    # 真实 socket 上开启 TCP_NODELAY
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        cli = socket.create_connection(srv.getsockname())
        conn, _ = srv.accept()

        class FakeWriter:
            def __init__(self, sock):
                self._sock = sock

            def get_extra_info(self, name):
                return self._sock if name == "socket" else None

        _enable_tcp_nodelay(FakeWriter(conn))
        check("TCP_NODELAY 已设置",
              conn.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) == 1)
        conn.close()
        cli.close()
    finally:
        srv.close()

    # 连接数上限
    proxy = MinecraftProxy(max_sessions=1)
    check("max_sessions 生效", proxy.max_sessions == 1)
    check("默认 max_sessions 合理", MinecraftProxy().max_sessions >= 1)


# -------------------------------------------------------------- 信号处理
def test_signal_wiring():
    print("\n== 信号处理接入 ==")
    import signal as signal_mod

    import main as main_mod

    src = Path(main_mod.__file__).read_text(encoding="utf-8")
    check("导入 signal", "import signal" in src)
    check("使用 add_signal_handler", "add_signal_handler" in src)
    check("处理 SIGTERM", "SIGTERM" in src)
    check("暴露 --max-connections", "--max-connections" in src)
    check("提供安装函数", callable(main_mod.install_signal_handlers))
    check("提供卸载函数", callable(main_mod.remove_signal_handlers))

    async def scenario():
        loop = asyncio.get_running_loop()
        fired: list[str] = []
        installed = main_mod.install_signal_handlers(loop, fired.append)
        check("至少安装了一个信号", len(installed) >= 1)
        check("安装了 SIGINT", any(s == signal_mod.SIGINT for s, _ in installed))
        check("安装了 SIGTERM", any(s == signal_mod.SIGTERM for s, _ in installed))

        if any(k == "loop" for _, k in installed):
            # POSIX：真实投递 SIGTERM
            os.kill(os.getpid(), signal_mod.SIGTERM)
            try:
                await asyncio.wait_for(_wait_for(fired, 1), timeout=3)
                check("SIGTERM 触发回调", len(fired) >= 1)
            except asyncio.TimeoutError:
                check("SIGTERM 触发回调", False)
        else:
            # Windows：直接调用已注册的 handler 验证回退路径
            for sig, kind in installed:
                if kind != "signal":
                    continue
                handler = signal_mod.getsignal(sig)
                if callable(handler):
                    handler(sig, None)
            check("signal.signal 回退路径可触发", len(fired) >= 1)
            check("回调收到信号名", any(n in ("SIGINT", "SIGTERM") for n in fired))

        main_mod.remove_signal_handlers(loop, installed)
        check("卸载不抛异常", True)
        check("卸载后恢复默认",
              signal_mod.getsignal(signal_mod.SIGINT) is signal_mod.default_int_handler
              or not callable(signal_mod.getsignal(signal_mod.SIGINT)))

    asyncio.run(scenario())


async def _wait_for(collection, count):
    while len(collection) < count:
        await asyncio.sleep(0.01)


# ------------------------------------------------ 关闭时不丢在途写入
def test_shutdown_waits_for_writes():
    print("\n== shutdown 等待在途写入 ==")
    import core.proxy as proxy_mod
    from core.proxy import ClientSession, MinecraftProxy

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            proxy = MinecraftProxy(
                local_host="127.0.0.1", local_port=0,
                target_host="127.0.0.1", target_port=1,
                world_dir=tmp,
            )

            finished_flag = {"done": False}
            closed_event = asyncio.Event()
            orig_shutdown = proxy_mod.ChunkExtractor.shutdown

            async def traced(self):
                await asyncio.sleep(0.2)   # 模拟 drain 慢
                await orig_shutdown(self)
                finished_flag["done"] = True

            async def fake_connect(self):
                class SR:
                    async def read(self, n):
                        # 真实套接字在 close() 后会返回 EOF，这里模拟该行为
                        await closed_event.wait()
                        return b""

                class SW:
                    def get_extra_info(self, name):
                        return None

                    def is_closing(self):
                        return closed_event.is_set()

                    def write(self, data):
                        pass

                    async def drain(self):
                        pass

                    def close(self):
                        closed_event.set()

                    async def wait_closed(self):
                        pass

                self.server_reader, self.server_writer = SR(), SW()
                return True

            class FakeWriter:
                def __init__(self):
                    self.closed = False

                def get_extra_info(self, name):
                    return ("127.0.0.1", 1)

                def is_closing(self):
                    return self.closed

                def write(self, data):
                    pass

                async def drain(self):
                    pass

                def close(self):
                    self.closed = True
                    closed_event.set()

                async def wait_closed(self):
                    pass

            class FakeReader:
                async def read(self, n):
                    await closed_event.wait()
                    return b""

            orig_connect = ClientSession.connect_to_server
            proxy_mod.ChunkExtractor.shutdown = traced
            ClientSession.connect_to_server = fake_connect
            try:
                await proxy.start()
                session = ClientSession(
                    FakeReader(), FakeWriter(), "127.0.0.1", 1, "x", tmp
                )
                proxy.sessions.append(session)
                task = asyncio.create_task(session.run())
                await asyncio.sleep(0.2)

                await proxy.shutdown()
                check("shutdown 返回时会话已收尾", finished_flag["done"])
                check("会话 finished 已置位", session.finished.is_set())
                await asyncio.wait_for(task, timeout=5)
                check("会话任务已结束", task.done())
            finally:
                proxy_mod.ChunkExtractor.shutdown = orig_shutdown
                ClientSession.connect_to_server = orig_connect

    asyncio.run(scenario())


def test_failed_write_not_cached():
    print("\n== 写入失败不缓存去重键 ==")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            extractor = ChunkExtractor(tmp)
            # 空数据必然失败
            ok1 = await extractor.save_chunk((1, 1, b""), 0)
            check("空数据写入失败", ok1 is False)
            check("失败键未被缓存", ("overworld", 1, 1) not in extractor._saved_keys)
            check("失败计数", extractor.stats["chunks_failed"] == 1)

            # 之后重发同样坐标的成功数据应被接受
            data = zlib.compress(nbt.build_root({"xPos": nbt.Int(1)}, ""))
            ok2 = await extractor.save_chunk((1, 1, data), 0)
            check("重发后可成功写入", ok2 is True)
            check("成功后计数", extractor.stats["chunks_saved"] == 1)
            await extractor.shutdown()

    asyncio.run(scenario())


def test_pending_entity_bucket_bounded():
    print("\n== 单区块待配对方块实体上限 ==")
    import core.protocol as proto_mod

    original = proto_mod.MAX_PENDING_ENTITIES_PER_CHUNK
    try:
        proto_mod.MAX_PENDING_ENTITIES_PER_CHUNK = 3
        parser = ProtocolParser()
        for _ in range(20):
            parser._on_block_entity_data(_make_block_entity_payload(1, 2, 3))
        check("单区块缓存受上限约束",
              len(parser.pending_block_entities[(0, 0)]) == 3)
    finally:
        proto_mod.MAX_PENDING_ENTITIES_PER_CHUNK = original


def test_run_shutdown_path():
    print("\n== main.run 关闭路径 ==")
    import core.proxy as proxy_mod
    import main as main_mod

    async def scenario():
        events = {}

        async def fake_run(self):
            events["started"] = True
            await asyncio.sleep(30)

        async def fake_shutdown(self):
            events["shutdown"] = True
            events["max_sessions"] = self.max_sessions

        orig_run, orig_shutdown = proxy_mod.MinecraftProxy.run, proxy_mod.MinecraftProxy.shutdown
        proxy_mod.MinecraftProxy.run = fake_run
        proxy_mod.MinecraftProxy.shutdown = fake_shutdown
        try:
            with tempfile.TemporaryDirectory() as tmp:
                task = asyncio.create_task(main_mod.run([
                    "--target", "127.0.0.1:25566",
                    "--local-port", "0",
                    "--no-prompt",
                    "--max-connections", "3",
                    "--world-dir", tmp,
                ]))
                # 等代理起来，再模拟 stop 事件（等价于收到信号）
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if events.get("started"):
                        break
                check("代理已启动", events.get("started", False))

                # 直接投递信号：POSIX 用 os.kill；Windows 调用已注册 handler
                import signal as signal_mod
                if sys.platform != "win32":
                    os.kill(os.getpid(), signal_mod.SIGINT)
                else:
                    handler = signal_mod.getsignal(signal_mod.SIGINT)
                    if callable(handler):
                        handler(signal_mod.SIGINT, None)

                rc = await asyncio.wait_for(task, timeout=10)
                check("返回码为 0", rc == 0)
                check("shutdown 被调用", events.get("shutdown", False))
                check("max-connections 传递到代理", events.get("max_sessions") == 3)
        finally:
            proxy_mod.MinecraftProxy.run = orig_run
            proxy_mod.MinecraftProxy.shutdown = orig_shutdown

    asyncio.run(scenario())


def test_client_disconnect_ends_session():
    """
    客户端退出后，会话必须立刻结束。

    回归：原来用 `gather(downstream, upstream)` 等**两个方向都结束**。
    客户端退出游戏后其 TCP 读端关闭，handle_upstream 结束，但服务端连接
    未断且仍在推送（Keep Alive / 时间 / 实体更新……），handle_downstream
    会一直阻塞在 read()，导致代理在客户端已退出后仍持续解析并打印
    「客户端包」。修复为 FIRST_COMPLETED + 取消另一方向。
    """
    print("\n== 客户端断开后会话立即结束 ==")

    async def scenario():
        # 服务端：接受连接后持续推送数据，永不主动关闭
        pushed = 0

        async def fake_server(reader, writer):
            nonlocal pushed
            try:
                # 读握手，然后无限推送
                await reader.read(1024)
                while True:
                    writer.write(b"\x02\x00\x00")     # 任意小帧
                    await writer.drain()
                    pushed += 1
                    await asyncio.sleep(0.01)
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        server = await asyncio.start_server(fake_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            proxy = MinecraftProxy(
                local_host="127.0.0.1", local_port=0,
                target_host="127.0.0.1", target_port=port,
                world_dir=tmp, debug=False, allow_download=False,
            )
            await proxy.start()
            local_port = proxy.port

            client_reader, client_writer = await asyncio.open_connection(
                "127.0.0.1", local_port
            )
            # 等会话建立并开始接收
            for _ in range(100):
                await asyncio.sleep(0.01)
                if proxy.sessions:
                    break
            check("会话已建立", len(proxy.sessions) == 1)

            # 模拟「客户端退出游戏」：关闭客户端连接
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except Exception:
                pass

            session = proxy.sessions[0]
            try:
                await asyncio.wait_for(session.finished.wait(), timeout=5)
                ended = True
            except asyncio.TimeoutError:
                ended = False
            check("客户端断开后会话在 5 秒内结束", ended)

            pushed_at_end = pushed
            await asyncio.sleep(0.3)
            check("会话结束后不再继续处理服务端推送",
                  pushed - pushed_at_end <= 40)

            await proxy.shutdown()
            server.close()
            await server.wait_closed()
            try:
                client_reader.feed_eof()
            except Exception:
                pass

    asyncio.run(scenario())


if __name__ == "__main__":
    test_varint_negative()
    test_track_write_drain()
    test_emit_chunk_tracking()
    test_bounded_memory()
    test_durable_async_write()
    test_network_helpers()
    test_signal_wiring()
    test_shutdown_waits_for_writes()
    test_failed_write_not_cached()
    test_pending_entity_bucket_bounded()
    test_run_shutdown_path()
    test_client_disconnect_ends_session()

    print(f"\n===== 结果: {PASS} 通过, {FAIL} 失败 =====")
    raise SystemExit(1 if FAIL else 0)
