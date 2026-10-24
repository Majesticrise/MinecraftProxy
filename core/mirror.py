"""
镜像模式服务端：接收 mc-mirror-agent 从 Minecraft 客户端进程内镜像出的明文数据帧。

为什么需要它
------------
正版验证（online-mode=true）服务器的流量在客户端内是 AES 加密的，字节代理
（proxy.py）无法旁路解析。mc-mirror-agent（javaagent）在客户端 netty 管道
「解密之后、包解码之前」的位置抄送**明文裸帧**到这里；本模块只负责把帧喂给
ProtocolParser——状态机、区块解析、注册表、落盘管线与代理模式完全共用。

镜像协议（agent -> 本程序，TCP，由 agent 主动连接）
--------------------------------------------------
    连接建立后 agent 先发:  b"MCMR" + 版本号(1B, 当前 = 1)
    之后每条消息:           [方向 1B][帧长 4B 大端][帧字节]

        方向 0 = 服务器 -> 客户端（clientbound）
        方向 1 = 客户端 -> 服务器（serverbound）

    帧字节 = 明文裸帧 `[VarInt 包ID][载荷]`（agent 侧已完成解密与解压，
    无长度前缀——那分别是 splitter / decompressor 的职责，发生在抽头之前）。

会话模型：一个 agent 连接对应客户端与一个服务器的连接生命周期；玩家退出
服务器即断开（会话结束、区块落盘、生成 level.dat）。多人菜单里的服务器列表
ping 也会产生短连接，走完 STATUS 状态后没有区块，静默忽略。
"""

from __future__ import annotations

import asyncio

from .chunk_extractor import ChunkExtractor
from .logger import setup_logger
from .protocol import ConnectionState, ProtocolParser
from .varint import VarInt

logger = setup_logger(__name__)

MIRROR_MAGIC = b"MCMR"
MIRROR_VERSION = 1

# 单帧上限（区块包最大约 16MB，留足余量；防御异常数据）
MAX_FRAME_BYTES = 32 * 1024 * 1024
# 关闭时等待单个会话收尾的上限（drain 写盘 + level.dat）
SESSION_SHUTDOWN_TIMEOUT = 15.0
# 启动后多久还没收到 agent 连接就提示一次 JVM 参数（引导）
CONNECT_HINT_DELAY = 20.0


class MirrorSession:
    """一次 agent 连接 = 客户端与某个服务器的一次连接"""

    def __init__(self, server: MirrorServer, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.client_addr = None
        # 会话完全结束（含区块落盘）后置位，MirrorServer.shutdown 靠它确认
        self.finished = asyncio.Event()
        self.parser = ProtocolParser(
            debug=server.debug,
            world_dir=server.world_dir,
            block_map=server.block_map,
            allow_download=server.allow_download,
            mirror_mode=True,
        )
        self.extractor = ChunkExtractor(server.world_dir, debug=server.debug)
        self.parser.on_chunk_complete = self.extractor.save_chunk
        self.parser.task_tracker = self.extractor.track_write
        self.parser.on_container_items = self.extractor.update_container_items

    # ------------------------------------------------------------------ 运行
    async def run(self) -> None:
        try:
            self.client_addr = self.writer.get_extra_info("peername")
        except Exception:
            pass
        try:
            if not await self._check_magic():
                return
            logger.info(f"mc-mirror-agent 已连接 ({self.client_addr})")
            await self._loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"镜像会话异常: {exc}", exc_info=self.server.debug)
        finally:
            await self._finish()
            await self._close()

    async def _check_magic(self) -> bool:
        try:
            head = await self.reader.readexactly(5)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            logger.debug("agent 连接在握手前断开（可能是端口探测）")
            return False
        magic, version = head[:4], head[4]
        if magic != MIRROR_MAGIC or version != MIRROR_VERSION:
            logger.warning(
                f"镜像协议不匹配（收到 {magic!r} v{version}，"
                f"期望 {MIRROR_MAGIC!r} v{MIRROR_VERSION}）——"
                f"请确认 mc-mirror-agent 与本程序版本一致"
            )
            return False
        return True

    async def _loop(self) -> None:
        while True:
            try:
                header = await self.reader.readexactly(5)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            direction = header[0]
            length = int.from_bytes(header[1:5], "big")
            if length <= 0 or length > MAX_FRAME_BYTES:
                logger.warning(f"镜像帧长度非法 ({length})，断开会话")
                return
            try:
                frame = await self.reader.readexactly(length)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            if direction == 0:
                self._feed(frame, clientbound=True)
            elif direction == 1:
                self._feed(frame, clientbound=False)
            else:
                logger.warning(f"镜像帧方向非法 ({direction})，断开会话")
                return

    def _feed(self, frame: bytes, clientbound: bool) -> None:
        """把明文裸帧 [VarInt 包ID][载荷] 喂给协议状态机"""
        try:
            packet_id, used = VarInt.read(frame, 0)
        except ValueError:
            logger.debug(f"裸帧包头不是合法 VarInt ({len(frame)} 字节)，忽略")
            return
        payload = frame[used:]
        if clientbound:
            self.parser.process_clientbound(packet_id, payload)
        else:
            # 镜像模式不改写任何内容（握手改写只服务于代理模式的转发）
            self.parser.process_serverbound(packet_id, payload)

    # ------------------------------------------------------------------ 收尾
    async def _finish(self) -> None:
        try:
            await self.extractor.drain()
        except Exception as exc:
            logger.error(f"镜像会话写盘收尾失败: {exc}")
        stats = self.extractor.stats
        meaningful = (
            stats["chunks_saved"] > 0
            or stats["chunks_failed"] > 0
            or self.parser.state
            in (ConnectionState.LOGIN, ConnectionState.CONFIGURATION, ConnectionState.PLAY)
        )
        if meaningful:
            self.extractor.data_version = self.parser.data_version
            await self.parser.finalize_captures(
                self.server.world_dir, self.extractor)
            await self.extractor.shutdown()
        else:
            logger.debug("STATUS 探测会话（无区块），不输出统计")
        self.finished.set()
        logger.info(f"镜像会话结束 ({self.client_addr})")

    async def _close(self) -> None:
        writer = self.writer
        if writer is not None and not writer.is_closing():
            try:
                writer.close()
            except Exception:
                pass
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def wait_closed(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self.finished.wait()
        else:
            try:
                await asyncio.wait_for(self.finished.wait(), timeout)
            except asyncio.TimeoutError:
                logger.warning(f"等待镜像会话 {self.client_addr} 收尾超时（{timeout}s）")


class MirrorServer:
    """镜像模式服务器：等待 agent 连接，每个连接一个会话"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25566,
        world_dir: str = ".",
        debug: bool = False,
        block_map: str | None = None,
        allow_download: bool = True,
        max_sessions: int = 8,
    ):
        self.host = host
        self.port = port
        self.world_dir = world_dir
        self.debug = debug
        self.block_map = block_map
        self.allow_download = allow_download
        self.max_sessions = max_sessions
        self.server: asyncio.Server | None = None
        self.sessions: list[MirrorSession] = []
        self._ever_connected = False

    # ------------------------------------------------------------------ 会话
    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self.sessions) >= self.max_sessions:
            peer = writer.get_extra_info("peername")
            logger.warning(f"镜像连接数已达上限 {self.max_sessions}，拒绝 {peer}")
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return
        self._ever_connected = True
        session = MirrorSession(self, reader, writer)
        self.sessions.append(session)
        try:
            await session.run()
        finally:
            session.finished.set()
            if session in self.sessions:
                self.sessions.remove(session)

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> bool:
        try:
            self.server = await asyncio.start_server(
                self.handle, host=self.host, port=self.port
            )
        except Exception as exc:
            logger.error(f"启动镜像服务失败: {exc}")
            return False
        addr = self.server.sockets[0].getsockname()
        self.port = int(addr[1])
        logger.info(f"镜像服务已启动: {addr[0]}:{addr[1]}，等待 mc-mirror-agent 连接")
        return True

    @property
    def port_number(self) -> int:
        server = self.server
        if server is None or not server.sockets:
            return -1
        return int(server.sockets[0].getsockname()[1])

    async def run(self) -> None:
        if not await self.start():
            return
        server = self.server
        if server is None:
            return

        async def _hint() -> None:
            await asyncio.sleep(CONNECT_HINT_DELAY)
            if not self._ever_connected:
                logger.info(
                    f"尚未收到 agent 连接。若游戏已在运行，请确认启动器 JVM 参数包含：\n"
                    f"    -javaagent:<mc-mirror-agent.jar 路径>={self.host}:{self.port}\n"
                    f"（加参数后需重启游戏；离线验证服务器也可用代理模式，无需 agent）"
                )

        hint_task = asyncio.create_task(_hint(), name="mirror-hint")
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            hint_task.cancel()

    async def shutdown(self) -> None:
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
        sessions = list(self.sessions)
        for session in sessions:
            await session._close()
        if sessions:
            await asyncio.gather(
                *(s.wait_closed(timeout=SESSION_SHUTDOWN_TIMEOUT) for s in sessions),
                return_exceptions=True,
            )
        logger.info("镜像服务已停止")
