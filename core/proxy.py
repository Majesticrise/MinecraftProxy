"""
Minecraft 代理核心模块

实现 TCP 数据转发与协议拦截。

关键原则
--------
**代理必须是字节透明的**：服务器下发 Set Compression 之后，双向流量都会
使用压缩帧格式（[长度][数据长度][zlib(包ID+载荷)]）。任何「解压再重新
编码」的做法都会破坏帧结构并导致客户端掉线。因此本模块只做：

    [读取完整数据包] -> [解析副本用于提取信息] -> [原样转发原始字节]

解析永远作用在数据的副本上，转发的永远是缓冲区里的原始切片。
"""

from __future__ import annotations

import asyncio
import socket

from .chunk_extractor import ChunkExtractor
from .logger import setup_logger
from .protocol import ProtocolParser
from .varint import VarInt, read_packet

logger = setup_logger(__name__)

READ_BUFFER_SIZE = 262144
MAX_BUFFER_SIZE = 64 * 1024 * 1024   # 单个数据包上限保护
# 关闭时等待单个会话收尾的上限（drain 写盘 + level.dat）
SESSION_SHUTDOWN_TIMEOUT = 15.0


def _enable_tcp_nodelay(writer: asyncio.StreamWriter | None) -> None:
    """开启 TCP_NODELAY：游戏数据包很小，禁掉 Nagle 可显著降低延迟"""
    if writer is None:
        return
    try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass

# read_packet 的返回约定:
#   packet_size > 0, packet_id >= -1  -> 帧完整（-1 表示载荷不可解析）
#   packet_size == 0                 -> 数据不完整，需要等待更多字节
PACKET_UNPARSABLE = -2               # 帧有效但载荷无法解析（如解压失败）


class ClientSession:
    """单个客户端 <-> 服务器 的代理会话"""

    def __init__(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target_host: str,
        target_port: int,
        target_addr_display: str | None,
        world_dir: str,
        debug: bool = False,
        block_map: str | None = None,
        allow_download: bool = True,
    ):
        self.client_reader = client_reader
        self.client_writer = client_writer
        self.target_host = target_host
        self.target_port = target_port
        self.target_addr_display = target_addr_display
        self.world_dir = world_dir
        self.debug = debug
        self.block_map = block_map
        self.allow_download = allow_download

        self.server_reader: asyncio.StreamReader | None = None
        self.server_writer: asyncio.StreamWriter | None = None

        self.protocol_parser = ProtocolParser(
            debug=debug,
            world_dir=world_dir,
            block_map=block_map,
            allow_download=allow_download,
        )
        self.chunk_extractor = ChunkExtractor(world_dir, debug=debug)

        self.is_running = True
        self.client_addr = None
        self.tasks: list[asyncio.Task] = []
        # 会话完全结束（含区块落盘 + level.dat）后置位；
        # MinecraftProxy.shutdown() 靠它确认“写盘已完成”
        self.finished = asyncio.Event()

    # ------------------------------------------------------------------ 关闭
    def stop(self) -> None:
        """请求结束会话（不阻塞）。真正的收尾在 run() 的 finally 中完成"""
        self.is_running = False

    async def wait_closed(self, timeout: float | None = None) -> None:
        """等待会话彻底结束（含区块写入 drain 与 level.dat 生成）"""
        if timeout is None:
            await self.finished.wait()
        else:
            try:
                await asyncio.wait_for(self.finished.wait(), timeout)
            except asyncio.TimeoutError:
                logger.warning(f"等待会话 {self.client_addr} 收尾超时（{timeout}s）")

    # ------------------------------------------------------------------ 连接
    async def connect_to_server(self) -> bool:
        display = self.target_addr_display or f"{self.target_host}:{self.target_port}"
        try:
            logger.info(f"正在连接目标服务器 {display}")
            self.server_reader, self.server_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
            _enable_tcp_nodelay(self.server_writer)
            logger.info("已连接到目标服务器")
            return True
        except Exception as exc:
            logger.error(f"连接目标服务器失败: {exc}")
            return False

    # ------------------------------------------------------------ 下游转发
    async def handle_downstream(self) -> None:
        """服务器 -> 客户端：原样转发，同时用副本做协议解析"""
        buffer = bytearray()

        while self.is_running and self.server_reader:
            try:
                data = await self.server_reader.read(READ_BUFFER_SIZE)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"读取服务器数据失败: {exc}")
                break

            if not data:
                logger.info("服务器连接已关闭")
                break

            buffer.extend(data)

            offset = 0
            while offset < len(buffer):
                threshold = self.protocol_parser.compression_threshold
                try:
                    packet_id, packet_payload, packet_size = read_packet(buffer, offset, threshold)
                except Exception as exc:
                    logger.debug(f"read_packet 异常: {exc}")
                    packet_id, packet_payload, packet_size = -1, b"", 0

                # 数据不完整，等待下一批
                if packet_size == 0:
                    break

                full_packet = bytes(buffer[offset:offset + packet_size])
                offset += packet_size

                # 解析副本（不修改转发内容）
                if packet_id >= 0:
                    try:
                        self.protocol_parser.process_clientbound(packet_id, packet_payload)
                    except Exception as exc:
                        logger.debug(f"解析客户端包出错 (id=0x{packet_id:02X}): {exc}")
                elif packet_id == PACKET_UNPARSABLE:
                    logger.debug(f"跳过不可解析的客户端包（{packet_size} 字节，仍原样转发）")

                # 原样转发
                if not self._write_to_client(full_packet):
                    break

            buffer = buffer[offset:]

            # 背压：确保写入缓冲区被排空，避免高负载下内存无限增长
            await self._drain(self.client_writer)

            if len(buffer) > MAX_BUFFER_SIZE:
                logger.error(f"缓冲区超过上限 ({len(buffer)} 字节)，数据包可能损坏，终止会话")
                break

        await self.close()

    # ------------------------------------------------------------ 上游转发
    async def handle_upstream(self) -> None:
        """客户端 -> 服务器：原样转发，仅在握手阶段可能改写"""
        buffer = bytearray()

        while self.is_running and self.client_reader:
            try:
                data = await self.client_reader.read(READ_BUFFER_SIZE)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"读取客户端数据失败: {exc}")
                break

            if not data:
                logger.info("客户端连接已关闭")
                break

            buffer.extend(data)

            offset = 0
            while offset < len(buffer):
                threshold = self.protocol_parser.compression_threshold
                try:
                    packet_id, packet_payload, packet_size = read_packet(buffer, offset, threshold)
                except Exception as exc:
                    logger.debug(f"read_packet 异常(上行): {exc}")
                    packet_id, packet_payload, packet_size = -1, b"", 0

                if packet_size == 0:
                    break

                full_packet = bytes(buffer[offset:offset + packet_size])
                offset += packet_size

                modified = None
                if packet_id >= 0:
                    try:
                        modified = self.protocol_parser.process_serverbound(packet_id, packet_payload)
                    except Exception as exc:
                        logger.debug(f"解析服务端包出错 (id=0x{packet_id:02X}): {exc}")
                elif packet_id == PACKET_UNPARSABLE:
                    logger.debug(f"跳过不可解析的服务端包（{packet_size} 字节，仍原样转发）")

                if modified is not None:
                    full_packet = self._build_uncompressed_packet(packet_id, modified)

                if not self._write_to_server(full_packet):
                    break

            buffer = buffer[offset:]

            # 背压：确保写入缓冲区被排空
            await self._drain(self.server_writer)

            if len(buffer) > MAX_BUFFER_SIZE:
                logger.error(f"上行缓冲区超过上限 ({len(buffer)} 字节)，终止会话")
                break

    # ------------------------------------------------------------ 转发工具
    @staticmethod
    async def _drain(writer: asyncio.StreamWriter | None) -> None:
        """排空写入缓冲区（背压）；失败时静默忽略，由下一次写入处理"""
        if writer is None or writer.is_closing():
            return
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            pass

    def _write_to_client(self, data: bytes) -> bool:
        if not self.client_writer or self.client_writer.is_closing():
            return False
        try:
            self.client_writer.write(data)
            return True
        except (ConnectionResetError, BrokenPipeError):
            logger.info("客户端连接已断开")
            self.is_running = False
            return False
        except Exception as exc:
            logger.error(f"转发给客户端失败: {exc}")
            self.is_running = False
            return False

    def _write_to_server(self, data: bytes) -> bool:
        if not self.server_writer or self.server_writer.is_closing():
            return False
        try:
            self.server_writer.write(data)
            return True
        except (ConnectionResetError, BrokenPipeError):
            logger.info("服务器连接已断开")
            self.is_running = False
            return False
        except Exception as exc:
            logger.error(f"转发给服务器失败: {exc}")
            self.is_running = False
            return False

    @staticmethod
    def _build_uncompressed_packet(packet_id: int, payload: bytes) -> bytes:
        """构建未压缩数据包（仅用于握手改写等极小场景）"""
        body = VarInt.write(packet_id) + payload
        return VarInt.write(len(body)) + body

    # ---------------------------------------------------------------- 会话
    async def run(self) -> None:
        if not await self.connect_to_server():
            logger.error("无法连接到目标服务器，关闭会话")
            await self.close()
            self.finished.set()
            return

        try:
            self.client_addr = self.client_writer.get_extra_info("peername")
            logger.info(f"客户端 {self.client_addr} 已连接")
        except Exception:
            pass

        self.protocol_parser.set_real_address(self.target_host, self.target_port)
        self.protocol_parser.on_chunk_complete = self.chunk_extractor.save_chunk
        # 让区块写入协程被提取器托管，会话结束前统一 drain（防丢数据）
        self.protocol_parser.task_tracker = self.chunk_extractor.track_write
        # 容器内容（打开过的箱子等）合并进方块实体
        self.protocol_parser.on_container_items = self.chunk_extractor.update_container_items

        downstream = asyncio.create_task(self.handle_downstream(), name="downstream")
        upstream = asyncio.create_task(self.handle_upstream(), name="upstream")
        self.tasks = [downstream, upstream]

        try:
            # 任一方向结束即终止会话。
            #
            # 不能用 gather(两者)：客户端退出游戏后，它的 TCP 读端关闭使
            # handle_upstream 结束，但服务端仍会继续推送（Keep Alive、时间、
            # 实体更新……）且其连接未断，handle_downstream 会一直阻塞在
            # read()，导致代理在客户端已退出后仍持续解析/打印「客户端包」。
            # FIRST_COMPLETED + 取消另一方向即可立即收尾。
            await asyncio.wait(
                {downstream, upstream}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            self.is_running = False
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            try:
                # 会话可能随时被外部打断：先把在途的区块写入跑完，
                # 再落盘地图/实体等附加捕获，最后生成 level.dat
                self.chunk_extractor.data_version = self.protocol_parser.data_version
                await self.protocol_parser.finalize_captures(
                    self.world_dir, self.chunk_extractor)
                await self.chunk_extractor.shutdown()
            except Exception as exc:
                logger.error(f"区块提取器收尾失败: {exc}", exc_info=True)
            finally:
                self.finished.set()
            await self.close()
            logger.info(f"会话结束 (客户端 {self.client_addr})")

    async def close(self) -> None:
        self.is_running = False
        for writer in (self.client_writer, self.server_writer):
            if writer is not None and not writer.is_closing():
                try:
                    writer.close()
                except Exception:
                    pass
        for writer in (self.client_writer, self.server_writer):
            if writer is not None:
                try:
                    await writer.wait_closed()
                except Exception:
                    pass


class MinecraftProxy:
    """代理服务器"""

    def __init__(
        self,
        local_host: str = "127.0.0.1",
        local_port: int = 25565,
        target_host: str = "localhost",
        target_port: int = 25566,
        target_addr_display: str | None = None,
        world_dir: str = "./downloaded_world",
        debug: bool = False,
        block_map: str | None = None,
        allow_download: bool = True,
        max_sessions: int = 8,
    ):
        self.local_host = local_host
        self.local_port = local_port
        self.target_host = target_host
        self.target_port = target_port
        self.target_addr_display = target_addr_display
        self.world_dir = world_dir
        self.debug = debug
        self.block_map = block_map
        self.allow_download = allow_download
        self.max_sessions = max_sessions
        self.server: asyncio.Server | None = None
        self.sessions: list[ClientSession] = []

    async def handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        # 连接数上限保护：超出的客户端直接拒绝，避免资源耗尽
        if len(self.sessions) >= self.max_sessions:
            peer = client_writer.get_extra_info("peername")
            logger.warning(
                f"连接数已达上限 {self.max_sessions}，拒绝 {peer}"
            )
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            return

        _enable_tcp_nodelay(client_writer)
        session = ClientSession(
            client_reader, client_writer,
            self.target_host, self.target_port,
            self.target_addr_display,
            self.world_dir, self.debug,
            block_map=self.block_map,
            allow_download=self.allow_download,
        )
        self.sessions.append(session)
        try:
            await session.run()
        finally:
            # 兜底：即使 run() 意外抛出，也要解除 shutdown() 的等待
            session.finished.set()
            if session in self.sessions:
                self.sessions.remove(session)

    async def start(self) -> bool:
        """仅启动监听（不阻塞），便于测试或由外部控制生命周期"""
        try:
            self.server = await asyncio.start_server(
                self.handle_client,
                host=self.local_host,
                port=self.local_port,
            )
        except Exception as exc:
            logger.error(f"启动代理服务器失败: {exc}")
            return False

        addr = self.server.sockets[0].getsockname()
        display = self.target_addr_display or f"{self.target_host}:{self.target_port}"
        logger.info(f"代理服务器已启动: {addr[0]}:{addr[1]}")
        logger.info(f"目标服务器: {display}")
        logger.info("请在 Minecraft 客户端中连接上述代理地址")
        return True

    @property
    def port(self) -> int:
        """当前监听端口（未启动时为 -1）"""
        server = self.server
        if server is None or not server.sockets:
            return -1
        sockname = server.sockets[0].getsockname()
        return int(sockname[1])

    async def run(self) -> None:
        if not await self.start():
            return

        server = self.server
        if server is None:
            return
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(f"代理服务器异常: {exc}")

    async def shutdown(self) -> None:
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass

        # 1) 请求所有会话停止，并关闭它们的套接字（尽快打断阻塞读）
        sessions = list(self.sessions)
        for session in sessions:
            session.stop()
        for session in sessions:
            await session.close()

        # 2) 等待会话真正收尾（drain 未完成写入 + 生成 level.dat），
        #    否则进程退出时会丢掉在途的区块数据
        if sessions:
            await asyncio.gather(
                *(s.wait_closed(timeout=SESSION_SHUTDOWN_TIMEOUT) for s in sessions),
                return_exceptions=True,
            )
        logger.info("代理服务器已停止")
