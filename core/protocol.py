"""
Minecraft 协议解析模块（版本自适应）

核心思想
--------
不再硬编码任何数据包 ID。握手包中携带的 protocol_version 会被用于
加载对应的 ID 表（packet_ids.PacketIdResolver），对于没有精确表的版本
则退化为「结构探测」，因此可以适配任意服务端版本。

本模块只做「识别 + 状态机 + 区块解析」，永不修改被转发的字节流。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import nbt
from .biome_registry import BiomeRegistry, build_biome_registry
from .block_registry import (
    DEFAULT_DATA_VERSION,
    PROTOCOL_DATA_VERSION,
    BlockStateRegistry,
    load_registry,
)
from .chunk_format import (
    ParsedChunk,
    build_chunk_nbt,
    compress_chunk_nbt,
    parse_block_entity_data_packet,
    parse_caves_chunk_data_packet,
    parse_chunk_data_packet,
    parse_flattened_chunk_data_packet,
    parse_legacy_chunk_data_packet,
    parse_legacy_map_chunk_bulk_packet,
    parse_unload_chunk_packet,
    parse_update_light_packet,
)
from .datatypes import PacketBuffer, write_string, write_varint
from .logger import setup_logger
from .packet_ids import (
    DIR_CLIENTBOUND,
    DIR_SERVERBOUND,
    PROTOCOL_VERSION_NAMES,
    PacketIdResolver,
)

logger = setup_logger(__name__)

# 各维度的区块节数（世界高度 / 16）
DIMENSION_SECTION_COUNTS: dict[str, int] = {
    "minecraft:overworld": 24,
    "minecraft:the_nether": 16,
    "minecraft:the_end": 16,
}
DIMENSION_MIN_SECTION_Y: dict[str, int] = {
    "minecraft:overworld": -4,
    "minecraft:the_nether": 0,
    "minecraft:the_end": 0,
}
DEFAULT_SECTION_COUNT = 24
DEFAULT_MIN_SECTION_Y = -4

# 待配对方块实体的区块数量上限（防止对应区块始终不来导致内存累积）
MAX_PENDING_CHUNKS = 8192
# 单个区块缓存的最大方块实体数（防止同一区块被反复发 BE 包时无界增长）
MAX_PENDING_ENTITIES_PER_CHUNK = 4096

# 协议版本分段（决定区块线格式家族）
#
# 数据来源: PrismarineJS minecraft-data protocolVersions.json
#   735 = 1.16, 751 = 1.16.2, 753 = 1.16.3, 754 = 1.16.5
#   755 = 1.17, 756 = 1.17.1
#   757 = 1.18 / 1.18.1, 758 = 1.18.2
VERSION_LEGACY_1_8 = 47
VERSION_FLATTENED_MIN = 735       # 1.16：带 groundUp + bitMap(varint)
VERSION_FLATTENED_1_16_MAX = 754  # 1.16.5：最后一个带 groundUp 的版本
VERSION_FLATTENED_1_17_MAX = 756  # 1.17.1：无 groundUp，bitMap 是 i64[]varint
VERSION_CAVES_MIN = 757           # 1.18 / 1.18.1：洞穴与山崖（区块头重写）
VERSION_CAVES_NO_FLUID_MAX = 769  # 1.21.4：section 内无 fluid_count


def chunk_format_family(protocol_version: int | None) -> str:
    """把协议版本归入区块线格式家族"""
    if protocol_version is None:
        return "modern"
    if protocol_version <= VERSION_LEGACY_1_8:
        return "legacy"
    if VERSION_FLATTENED_MIN <= protocol_version <= VERSION_FLATTENED_1_17_MAX:
        return "flattened"
    return "modern"


def sections_for_protocol(protocol_version: int | None, name: str) -> tuple[int, int]:
    """按协议版本与维度确定 (节数, 最低节 Y)。

    1.8 ~ 1.17：固定 16 节 / min_section_y=0（世界高度 256）
    1.18+：由维度决定（主世界 24 节 / min_section_y=-4）
    """
    if protocol_version is not None and protocol_version < VERSION_CAVES_MIN:
        return 16, 0
    return sections_for_dimension(name)


# --------------------------------------------------------------------------
# 状态与数据结构
# --------------------------------------------------------------------------
class ConnectionState(Enum):
    """连接状态"""
    HANDSHAKE = 0
    STATUS = 1
    LOGIN = 2
    CONFIGURATION = 3
    PLAY = 4


class PacketDirection(Enum):
    """数据包方向"""
    CLIENTBOUND = 0  # 服务器 -> 客户端
    SERVERBOUND = 1  # 客户端 -> 服务器


@dataclass
class ChunkData:
    """区块数据（data 为 zlib 压缩后的 Anvil 区块 NBT）"""
    x: int
    z: int
    data: bytes
    block_entities: list[dict] = field(default_factory=list)
    heightmaps: dict[str, Any] = field(default_factory=dict)
    light: dict[str, Any] | None = None


@dataclass
class LightData:
    """光照数据结构"""
    chunk_x: int
    chunk_z: int
    sky_light_mask: list[int]
    block_light_mask: list[int]
    empty_sky_light_mask: list[int]
    empty_block_light_mask: list[int]
    sky_light_arrays: list[bytes]
    block_light_arrays: list[bytes]


DIMENSION_IDS = {
    "minecraft:overworld": 0,
    "minecraft:the_nether": -1,
    "minecraft:the_end": 1,
}

_KNOWN_DIMENSION_HINTS = ("overworld", "nether", "end", "the_end")


def _dimension_key(name: str) -> str:
    """把维度名规整为已知识别的键；未知维度返回原名"""
    if not name:
        return "minecraft:overworld"
    lowered = name.lower()
    for hint, key in (("nether", "minecraft:the_nether"),
                      ("the_end", "minecraft:the_end"),
                      ("end", "minecraft:the_end"),
                      ("overworld", "minecraft:overworld")):
        if hint in lowered:
            return key
    return name


def sections_for_dimension(name: str) -> tuple[int, int]:
    """返回 (节数, 最低节 Y)。未知维度按主世界处理。"""
    key = _dimension_key(name)
    return (
        DIMENSION_SECTION_COUNTS.get(key, DEFAULT_SECTION_COUNT),
        DIMENSION_MIN_SECTION_Y.get(key, DEFAULT_MIN_SECTION_Y),
    )


# 旧版（1.8 ~ 1.15）维度整数 ID -> 维度名
_LEGACY_DIMENSION_NAMES = {
    -1: "minecraft:the_nether",
    0: "minecraft:overworld",
    1: "minecraft:the_end",
}


def _dimension_name_from_id(dimension_id: int) -> str:
    """把旧版维度整数 ID 转为维度名；未知 ID（模组）用 dimension_<n> 占位"""
    return _LEGACY_DIMENSION_NAMES.get(dimension_id, f"dimension_{dimension_id}")


def _handler_name(packet_name: str) -> str:
    """把包名转换为处理方法名，例如 'Chunk Data and Update Light' -> '_on_chunk_data_and_update_light'"""
    cleaned = packet_name.lower()
    for ch in "()/-":
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.replace(" ", "_")
    return "_on_" + cleaned


# --------------------------------------------------------------------------
# 协议解析器
# --------------------------------------------------------------------------
class ProtocolParser:
    """
    版本自适应的协议解析器。

    数据包格式: [VarInt 长度] [VarInt 数据包ID] [载荷...]
    """

    def __init__(
        self,
        debug: bool = False,
        world_dir: str = ".",
        block_map: str | None = None,
        allow_download: bool = True,
    ):
        self.debug = debug
        self.state = ConnectionState.HANDSHAKE
        self.player_name: str | None = None
        self.dimension: int = 0
        self.dimension_name: str = "minecraft:overworld"
        self.is_joined = False
        self.compression_threshold: int = -1

        # 版本自适应
        self.protocol_version: int | None = None
        self.resolver = PacketIdResolver(debug=debug)

        # 方块注册表（决定导出调色板是否为真实方块名）
        self.world_dir = world_dir
        self.block_map = block_map
        self.allow_download = allow_download
        self.registry: BlockStateRegistry = BlockStateRegistry()
        self._registry_loaded_for: int | None = None
        self.data_version: int = DEFAULT_DATA_VERSION

        # 生物群系注册表（决定导出的草地/水色是否正确）
        # 1.8 / 1.16 用内置数字 ID 表（握手时装载）；
        # 1.18+ 等 Registry Data 或 Join Game 的 Registry Codec 下发。
        self.biome_registry: BiomeRegistry = BiomeRegistry()

        # 当前维度的世界高度（由 Join Game / Respawn / Registry Data 决定）
        self.section_count: int = DEFAULT_SECTION_COUNT
        self.min_section_y: int = DEFAULT_MIN_SECTION_Y
        # 区块线格式家族（legacy / flattened / modern），由协议版本决定
        self.chunk_family: str = "modern"

        # 缓存
        self.pending_block_entities: dict[tuple[int, int], list[dict[str, Any]]] = {}

        # 回调
        self.on_chunk_complete: Callable[[ChunkData, Any], Any] | None = None
        # 任务登记器：用于把区块写入协程托管给提取器，会话结束时统一 drain
        self.task_tracker: Callable[[Any], Any] | None = None

        # 握手改写
        self.real_host: str | None = None
        self.real_port: int | None = None

        # 统计
        self.counters: dict[str, int] = {}

    # ------------------------------------------------------------------ 工具
    def _count(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def set_real_address(self, host: str, port: int) -> None:
        self.real_host = host
        self.real_port = port

    def _resolve(self, direction: str, packet_id: int, payload: bytes) -> str | None:
        return self.resolver.resolve(self.state.name, direction, packet_id, payload)

    def _dispatch(self, name: str, payload: bytes) -> None:
        handler = getattr(self, _handler_name(name), None)
        if handler is not None:
            handler(payload)
        elif self.debug:
            logger.debug(f"无处理器（已忽略）: {name}")

    # ------------------------------------------------------------- 握手改写
    def _modify_handshake_payload(self, original_payload: bytes) -> bytes:
        """
        把握手包中的服务器地址改写为真实目标地址。

        重要：BungeeCord / Velocity 的「传统转发」会把
        `真实IP\\0UUID\\0properties` 塞进 host 字段。直接整体替换会抹掉这些
        数据，导致后端服务器拒绝连接。因此这里只替换 **第一段**（真实主机名），
        其余以 \\0 分隔的后缀原样保留。
        """
        if not self.real_host or not self.real_port:
            return original_payload
        try:
            buf = PacketBuffer(original_payload)
            proto_ver = buf.read_varint()
            original_host = buf.read_string(255)
            buf.read_ushort()
            next_state = buf.read_varint()
        except Exception:
            logger.warning("握手包解析失败，使用原包转发")
            return original_payload

        # 保留 BungeeCord/Velocity 转发后缀
        suffix = ""
        if "\x00" in original_host:
            suffix = original_host[original_host.index("\x00"):]

        new_host = self.real_host + suffix
        new_payload = bytearray()
        new_payload += write_varint(proto_ver)
        new_payload += write_string(new_host)
        new_payload += self.real_port.to_bytes(2, "big")
        new_payload += write_varint(next_state)
        return bytes(new_payload)

    # ------------------------------------------------------ 客户端<->服务端
    def process_clientbound(self, packet_id: int, payload: bytes) -> bytes | None:
        """处理 服务器->客户端 数据包；始终返回 None（不修改转发内容）"""
        try:
            name = self._resolve(DIR_CLIENTBOUND, packet_id, payload)
            if name is not None:
                self._count(name)
                self._dispatch(name, payload)
            elif self.debug:
                logger.debug(
                    f"未识别的客户端包 state={self.state.name} id=0x{packet_id:02X} len={len(payload)}"
                )
        except Exception as exc:
            logger.debug(f"处理客户端包失败 (id=0x{packet_id:02X}): {exc}")
        return None

    def process_serverbound(self, packet_id: int, payload: bytes) -> bytes | None:
        """处理 客户端->服务器 数据包；仅在握手阶段可能需要改写"""
        try:
            if self.state == ConnectionState.HANDSHAKE:
                if packet_id == 0x00:
                    self._parse_handshake(payload)
                    modified = self._modify_handshake_payload(payload)
                    if modified != payload:
                        logger.debug("已改写握手包中的服务器地址")
                    return modified
                return None

            name = self._resolve(DIR_SERVERBOUND, packet_id, payload)
            if name is not None:
                self._count(name)
                self._dispatch(name, payload)
            elif self.debug:
                logger.debug(
                    f"未识别的服务端包 state={self.state.name} id=0x{packet_id:02X} len={len(payload)}"
                )
        except Exception as exc:
            logger.debug(f"处理服务端包失败 (id=0x{packet_id:02X}): {exc}")
        return None

    # ------------------------------------------------------------ 状态处理
    def _parse_handshake(self, payload: bytes) -> None:
        buf = PacketBuffer(payload)
        protocol_version = buf.read_varint()
        server_addr = buf.read_string()
        server_port = buf.read_ushort()
        next_state = buf.read_varint()

        self.protocol_version = protocol_version
        self.resolver.set_protocol_version(protocol_version)
        self.chunk_family = chunk_format_family(protocol_version)
        # 1.18 之前世界高度固定为 256（16 节 / min_y=0）
        if protocol_version < VERSION_CAVES_MIN:
            self.section_count, self.min_section_y = 16, 0

        try:
            self.state = ConnectionState(next_state)
        except ValueError:
            self.state = ConnectionState.HANDSHAKE

        version_name = PROTOCOL_VERSION_NAMES.get(protocol_version, "未知版本")
        mode = "精确 ID 表" if self.resolver.has_exact_table else "结构探测"
        logger.info(
            f"客户端版本: 协议 {protocol_version} ({version_name}) -> 目标 {server_addr}:{server_port} "
            f"[ID 解析: {mode}]"
        )

        # 加载方块注册表（决定导出世界里的方块是否正确）
        self._ensure_registry(protocol_version)

        # 旧版（1.8 / 1.16 / 1.17）的生物群系 ID 是硬编码的，用内置表；
        # 1.18+ 的生物群系注册表是动态的，等 Registry Data / Registry Codec。
        self.biome_registry = build_biome_registry(protocol_version)

    def _ensure_registry(self, protocol_version: int) -> None:
        """按协议版本加载方块注册表（只加载一次）"""
        if self._registry_loaded_for == protocol_version and self.registry.loaded:
            return
        self._registry_loaded_for = protocol_version
        self.data_version = PROTOCOL_DATA_VERSION.get(protocol_version, DEFAULT_DATA_VERSION)
        try:
            self.registry = load_registry(
                protocol_version,
                self.world_dir,
                explicit=self.block_map,
                allow_download=self.allow_download,
            )
        except Exception as exc:
            logger.error(f"加载方块注册表失败: {exc}")
            self.registry = BlockStateRegistry()
        if not self.registry.loaded:
            logger.warning("方块注册表不可用，导出的方块将退化为空气占位")

    def _on_login_start(self, payload: bytes) -> None:
        try:
            buf = PacketBuffer(payload)
            self.player_name = buf.read_string(16)
            logger.info(f"玩家 '{self.player_name}' 正在登录")
        except Exception:
            pass

    def _on_login_acknowledged(self, payload: bytes) -> None:
        if self.state != ConnectionState.LOGIN:
            return
        if self.resolver.uses_configuration_state():
            self.state = ConnectionState.CONFIGURATION
            logger.info("登录已确认，进入 CONFIGURATION 状态")
        else:
            self.state = ConnectionState.PLAY
            logger.info("登录已确认，进入 PLAY 状态")

    def _on_acknowledge_configuration(self, payload: bytes) -> None:
        if self.state == ConnectionState.CONFIGURATION:
            self.state = ConnectionState.PLAY
            logger.info("配置已完成，进入 PLAY 状态")

    def _on_finish_configuration(self, payload: bytes) -> None:
        if self.state == ConnectionState.CONFIGURATION:
            self.state = ConnectionState.PLAY
            logger.info("收到 Finish Configuration，进入 PLAY 状态")

    def _on_start_configuration(self, payload: bytes) -> None:
        self.state = ConnectionState.CONFIGURATION
        logger.info("服务器请求重新配置，进入 CONFIGURATION 状态")

    def _on_set_compression(self, payload: bytes) -> None:
        try:
            buf = PacketBuffer(payload)
            threshold = buf.read_varint()
        except Exception:
            logger.warning("Set Compression 包解析失败")
            return
        self.compression_threshold = threshold
        logger.info(f"服务器设置压缩阈值: {threshold}")

    def _on_login_success(self, payload: bytes) -> None:
        """Login Success: UUID(16) + String name + Prefixed Array of properties + Optional Session ID"""
        try:
            buf = PacketBuffer(payload)
            uuid_str = buf.read_uuid()
            username = buf.read_string(16)
            prop_count = 0
            if buf.remaining > 0:
                prop_count = buf.read_varint()
                for _ in range(prop_count):
                    buf.read_string()
                    buf.read_string()
                    if buf.remaining > 0 and buf.read_bool():
                        buf.read_string()
            logger.info(f"登录成功: {username} (UUID: {uuid_str}, 属性: {prop_count})")
        except Exception:
            logger.info("登录成功（响应格式无法完整解析，已忽略）")

    def _on_login(self, payload: bytes) -> None:
        """PLAY 状态的 Login 包（Join Game）"""
        self.is_joined = True
        self._try_parse_dimension(payload)
        logger.info(f"进入 PLAY 状态 (维度: {self.dimension_name})")

    _on_login_play = _on_login

    def _try_parse_dimension(self, payload: bytes) -> None:
        """尽力解析 Join Game 中的维度名，失败时保持默认（主世界）"""
        if self.chunk_family == "legacy":
            self._try_parse_dimension_legacy(payload)
            return
        # 1.16 ~ 1.20.1：生物群系注册表随 Join Game 的 Registry Codec 下发
        self._try_parse_registry_codec(payload)
        try:
            buf = PacketBuffer(payload)
            buf.read_int()          # Entity ID
            buf.read_bool()         # Is hardcore
            count = buf.read_varint()
            if not (0 <= count <= 64):
                return
            for _ in range(count):
                buf.read_identifier()
            buf.read_varint()       # Max players
            buf.read_varint()       # View distance
            buf.read_varint()       # Simulation distance
            buf.read_bool()         # Reduced debug info
            buf.read_bool()         # Enable respawn screen
            buf.read_bool()         # Do limited crafting

            dimension_name = None
            for layout in ("identifier", "varint"):
                snapshot = buf.offset
                try:
                    if layout == "identifier":
                        buf.read_identifier()
                    else:
                        buf.read_varint()
                    candidate = buf.read_identifier()
                    if any(hint in candidate.lower() for hint in _KNOWN_DIMENSION_HINTS):
                        dimension_name = candidate
                        break
                except Exception:
                    pass
                buf.offset = snapshot

            if dimension_name:
                self.dimension_name = dimension_name
                key = _dimension_key(dimension_name)
                self.dimension = DIMENSION_IDS.get(key, 0)
                self.section_count, self.min_section_y = sections_for_protocol(
                    self.protocol_version, dimension_name
                )
                logger.debug(
                    f"维度 {dimension_name}: {self.section_count} 节, "
                    f"最低节 Y={self.min_section_y}"
                )
        except Exception:
            pass

    def _try_parse_dimension_legacy(self, payload: bytes) -> None:
        """
        解析 1.8 的 Join Game（Login）包。

        结构: Int entityId, UByte gameMode, Byte dimension(i8),
              UByte difficulty, UByte maxPlayers, String levelType, Bool reducedDebugInfo
        其中 dimension 是整数（-1 下界 / 0 主世界 / 1 末地）。
        """
        try:
            buf = PacketBuffer(payload)
            buf.read_int()          # Entity ID
            buf.read_ubyte()        # Game mode
            dimension = buf.read_byte()  # 维度 ID（有符号）
            self.dimension = dimension
            self.dimension_name = _dimension_name_from_id(dimension)
            self.section_count, self.min_section_y = 16, 0
            logger.debug(f"1.8 维度 ID={dimension} -> {self.dimension_name}")
        except Exception:
            pass

    # Registry Codec 里我们关心的键（1.16 ~ 1.20.1 的 Join Game 携带）
    _CODEC_KEYS = ("minecraft:worldgen/biome", "minecraft:dimension_type")

    def _try_parse_registry_codec(self, payload: bytes) -> None:
        """
        从 Join Game 里提取 **Registry Codec**（1.16 ~ 1.20.1）。

        1.20.2 起注册表改由 CONFIGURATION 阶段的 `Registry Data` 包下发，
        不再出现在 Join Game 里；这里找不到就静默返回。

        包结构（1.16~1.20.1）:
            Int entityId, Bool hardcore, Byte gamemode, Byte prevGamemode,
            VarInt worldCount + Identifier[], NBT registryCodec, Identifier dimension,
            ...

        由于各版本的 `worldCount` 有无不一致（1.16.2 起才有），
        这里**不按固定偏移解析**，而是用字节特征定位 NBT 根：
        在载荷里查找 `\\x0a\\x00\\x00`（TAG_Compound + 空名字）且随后
        能解析出 `minecraft:worldgen/biome` 的位置。这样与版本无关。
        """
        if self.biome_registry.is_network and self.biome_registry.count > 0:
            return
        codec = self._find_registry_codec(payload)
        if codec is None:
            return
        try:
            biomes = codec.get("minecraft:worldgen/biome")
            names = self._extract_biome_names(biomes)
            if names:
                self.biome_registry.set_names(names, "Join Game Registry Codec")
                logger.debug(
                    f"生物群系注册表: {len(names)} 项 "
                    f"[{self.biome_registry.source}]"
                )
        except Exception as exc:
            logger.debug(f"解析 Registry Codec 失败: {exc}")

    def _find_registry_codec(self, payload: bytes) -> dict[str, Any] | None:
        """用字节特征定位 Registry Codec 的 NBT 根标签"""
        marker = b"\x0a\x00\x00"
        start = 0
        while True:
            idx = payload.find(marker, start)
            if idx < 0:
                return None
            start = idx + 1
            try:
                value, end = nbt.parse_named_tag(payload, idx)
            except Exception:
                continue
            if not isinstance(value, dict):
                continue
            # 必须像 Registry Codec（含我们关心的键之一），且解析长度合理
            if end - idx < 32:
                continue
            if any(k in value for k in self._CODEC_KEYS):
                return value
        return None

    @staticmethod
    def _extract_biome_names(node: Any) -> list[str]:
        """
        从 `minecraft:worldgen/biome` 节点提取群系名（顺序即 ID）。

        该节点是 `{type: "minecraft:worldgen/biome", value: {<name>: {...}}}`，
        其中 `value` 是 Compound，**键的顺序即注册表顺序**。
        部分版本可能直接是 `{<name>: {...}}`，两种都兼容。
        """
        if not isinstance(node, dict):
            return []
        value = node.get("value")
        container = value if isinstance(value, dict) else node
        names = [k for k in container if isinstance(k, str)]
        return [n for n in names if n and not n.startswith("type")]

    # ------------------------------------------------------------ 区块处理
    def _on_chunk_data_and_update_light(self, payload: bytes) -> None:
        parsed = self._parse_chunk_payload(payload)
        if parsed is not None:
            self._emit_chunk(parsed)
        elif self.debug:
            logger.debug(
                f"区块包解析失败 (协议 {self.protocol_version}, 家族 {self.chunk_family}, "
                f"节数 {self.section_count}, 负载 {len(payload)} 字节)"
            )

    def _parse_chunk_payload(self, payload: bytes) -> ParsedChunk | None:
        """按区块格式家族分派到对应的解析器"""
        family = self.chunk_family
        if family == "legacy":
            return parse_legacy_chunk_data_packet(payload, registry=self.registry)
        if family == "flattened":
            # 1.16 与 1.17 的区块头不同：1.17 去掉了 groundUp，bitMap 改为 i64[]varint
            is_1_17 = (
                self.protocol_version is not None
                and self.protocol_version > VERSION_FLATTENED_1_16_MAX
            )
            return parse_flattened_chunk_data_packet(
                payload,
                section_count=self.section_count,
                min_section_y=self.min_section_y,
                has_ground_up=not is_1_17,
                bitmap_bitset=is_1_17,
            )
        if self.protocol_version is not None and self.protocol_version <= VERSION_CAVES_NO_FLUID_MAX:
            # 1.18 ~ 1.21.4：section 内无 fluid_count
            return parse_caves_chunk_data_packet(
                payload,
                section_count=self.section_count,
                min_section_y=self.min_section_y,
            )
        return parse_chunk_data_packet(
            payload,
            section_count=self.section_count,
            min_section_y=self.min_section_y,
        )

    def _on_chunk_data(self, payload: bytes) -> None:
        self._on_chunk_data_and_update_light(payload)

    def _on_map_chunk_bulk(self, payload: bytes) -> None:
        """1.8 Map Chunk Bulk：一次下发多个区块列"""
        chunks = parse_legacy_map_chunk_bulk_packet(payload, registry=self.registry)
        for parsed in chunks:
            self._emit_chunk(parsed)
        if chunks:
            logger.debug(f"Map Chunk Bulk: 解析出 {len(chunks)} 个区块")

    def _on_level_chunk_with_light(self, payload: bytes) -> None:
        self._on_chunk_data_and_update_light(payload)

    def _on_registry_data(self, payload: bytes) -> None:
        """
        解析 Registry Data（配置阶段）。

        注意：`minecraft:block` 属于硬编码注册表，服务端不会下发；
        这里主要用来提取 **维度类型** 的高度信息，以便正确定位区块节。
        包结构（1.16+）: Registry ID(Identifier)
                       + Entries(Prefixed Array of Entry ID + Optional NBT)

        其中每个 entry 的值是 **`anonymousNbt`**（类型字节 + 负载，**无名字**，
        数据源 PrismarineJS `pc/<ver>/protocol.json` 的 `packet_registry_data`
        → `{"name": "value", "type": ["option", "anonymousNbt"]}`）。
        用 `parse_named_tag` 读会因把负载首字节当名字长度而报「字符串内容越界」，
        必须用 `parse_anonymous_tag`。
        """
        try:
            buf = PacketBuffer(payload)
            registry_id = buf.read_identifier()
            count = buf.read_varint()
            if count < 0 or count > 65536:
                return
            is_biome = registry_id == "minecraft:worldgen/biome"
            biome_names: list[str] = []
            if registry_id == "minecraft:dimension_type" or is_biome:
                for _ in range(count):
                    entry_name = buf.read_identifier()
                    if buf.read_bool():
                        value, end = nbt.parse_anonymous_tag(buf.data, buf.offset)
                        buf.offset = end
                        if registry_id == "minecraft:dimension_type":
                            self._apply_dimension_type(entry_name, value)
                        else:
                            # 生物群系注册表：**列表顺序即 ID**
                            biome_names.append(entry_name)
                    elif is_biome:
                        # 无 NBT 负载的条目同样占用一个 ID
                        biome_names.append(entry_name)
            if is_biome and biome_names:
                # 只在没有更完整的注册表时覆盖（同一次连接通常只发一次）
                if self.biome_registry.count < len(biome_names):
                    self.biome_registry.set_names(biome_names, "Registry Data")
                logger.debug(
                    f"生物群系注册表: {len(biome_names)} 项 "
                    f"[{self.biome_registry.source}]"
                )
            if self.debug:
                logger.debug(f"Registry Data: {registry_id} ({count} 项)")
        except Exception as exc:
            logger.debug(f"解析 Registry Data 失败: {exc}")

    def _apply_dimension_type(self, name: str, value: Any) -> None:
        """从 dimension_type 定义中提取高度，缓存到 section 尺寸表"""
        try:
            if not isinstance(value, dict):
                return
            height = value.get("height")
            min_y = value.get("min_y")
            if isinstance(height, int) and 16 <= height <= 4096 and height % 16 == 0:
                key = _dimension_key(name)
                DIMENSION_SECTION_COUNTS[key] = height // 16
                if isinstance(min_y, int):
                    DIMENSION_MIN_SECTION_Y[key] = min_y // 16
                logger.debug(
                    f"维度 {name}: 高度 {height} (min_y={min_y}) -> "
                    f"{height // 16} 节, 最低节 Y={min_y // 16 if isinstance(min_y, int) else '?'}"
                )
        except Exception:
            pass

    def _on_respawn(self, payload: bytes) -> None:
        """Respawn: 切换维度后需要更新世界高度"""
        if self.chunk_family == "legacy":
            self._on_respawn_legacy(payload)
            return
        try:
            buf = PacketBuffer(payload)
            # Dimension Type(VarInt) + Dimension Name(Identifier) + ...
            buf.read_varint()
            dimension_name = buf.read_identifier()
            self.dimension_name = dimension_name
            self.dimension = DIMENSION_IDS.get(_dimension_key(dimension_name), self.dimension)
            self.section_count, self.min_section_y = sections_for_protocol(
                self.protocol_version, dimension_name
            )
            logger.info(f"切换维度: {dimension_name} ({self.section_count} 节)")
        except Exception:
            pass

    def _on_respawn_legacy(self, payload: bytes) -> None:
        """1.8 Respawn: Int dimension(i32), UByte difficulty, UByte gamemode, String levelType"""
        try:
            buf = PacketBuffer(payload)
            dimension = buf.read_int()
            self.dimension = dimension
            self.dimension_name = _dimension_name_from_id(dimension)
            self.section_count, self.min_section_y = 16, 0
            logger.info(f"1.8 切换维度 ID={dimension} -> {self.dimension_name}")
        except Exception:
            pass

    def _on_update_light(self, payload: bytes) -> None:
        light = parse_update_light_packet(payload)
        if light is not None:
            logger.debug(f"光照更新: ({light.x}, {light.z})")

    def _on_block_entity_data(self, payload: bytes) -> None:
        entity = parse_block_entity_data_packet(payload)
        if entity is None:
            return
        key = (entity["chunk_x"], entity["chunk_z"])
        bucket = self.pending_block_entities.setdefault(key, [])
        if len(bucket) < MAX_PENDING_ENTITIES_PER_CHUNK:
            bucket.append(entity)
        # 上限保护：若对应区块始终不来，不能无限累积
        while len(self.pending_block_entities) > MAX_PENDING_CHUNKS:
            oldest = next(iter(self.pending_block_entities))
            if oldest == key:
                break
            self.pending_block_entities.pop(oldest, None)
        logger.debug(
            f"方块实体数据: ({entity['x']}, {entity['y']}, {entity['z']}) type={entity['type_id']}"
        )

    def _on_unload_chunk(self, payload: bytes) -> None:
        coords = parse_unload_chunk_packet(payload)
        if coords is None:
            return
        chunk_x, chunk_z = coords
        self.pending_block_entities.pop((chunk_x, chunk_z), None)
        logger.debug(f"区块被卸载: ({chunk_x}, {chunk_z})")

    def _on_disconnect(self, payload: bytes) -> None:
        try:
            buf = PacketBuffer(payload)
            text = buf.read_string(262144)
        except Exception:
            text = "<无法解析>"
        logger.warning(f"服务器断开连接: {text[:200]}")

    def _on_empty_configuration_packet(self, payload: bytes) -> None:
        """
        配置阶段的空负载包（Finish Configuration / Reset Chat 等）。
        无精确 ID 表时无法区分，二者都不影响本代理的状态机
        （状态切换由服务端包 Acknowledge Configuration 驱动），故仅记录。
        """
        if self.debug:
            logger.debug(f"配置阶段空负载包（当前状态: {self.state.name}）")

    # ------------------------------------------------------------ 区块输出
    def _emit_chunk(self, parsed: ParsedChunk) -> None:
        key = (parsed.x, parsed.z)
        pending = self.pending_block_entities.pop(key, [])

        block_entities: list[dict[str, Any]] = []
        for entity in list(parsed.block_entities) + list(pending):
            if "x" in entity:
                x, y, z = entity["x"], entity["y"], entity["z"]
            else:
                x = parsed.x * 16 + entity.get("local_x", 0)
                y = entity.get("y", 0)
                z = parsed.z * 16 + entity.get("local_z", 0)
            block_entities.append({
                "x": x,
                "y": y,
                "z": z,
                "type_id": entity.get("type_id", -1),
                "nbt": entity.get("nbt"),
                "nbt_bytes": entity.get("nbt_bytes", b""),
            })

        try:
            parsed.block_entities = block_entities
            raw_nbt = build_chunk_nbt(
                parsed,
                registry=self.registry,
                data_version=self.data_version,
                biome_registry=self.biome_registry,
                # 归一化输出（1.8 / 1.16 / 1.17 走 DataVersion 2975）时，
                # 把旧版群系名映射到 1.18+ 的等价名，否则 1.18.2 读不出来
                normalize_biomes=self.data_version <= 2975,
            )
        except Exception as exc:
            logger.warning(f"构建区块 ({parsed.x}, {parsed.z}) NBT 失败: {exc}")
            return

        chunk = ChunkData(
            x=parsed.x,
            z=parsed.z,
            data=compress_chunk_nbt(raw_nbt),
            block_entities=block_entities,
            heightmaps=parsed.heightmaps,
        )

        logger.debug(f"捕获区块 ({parsed.x}, {parsed.z})，NBT {len(raw_nbt)} 字节")

        if self.on_chunk_complete is not None:
            try:
                # 传维度名而非 ID，便于按维度分目录（支持模组维度）
                result = self.on_chunk_complete(chunk, self.dimension_name)
                if asyncio.iscoroutine(result):
                    # 登记任务：会话结束前会 drain，避免丢数据
                    if self.task_tracker is not None:
                        self.task_tracker(result)
                    else:
                        asyncio.create_task(result)
            except Exception:
                logger.exception("区块回调执行失败")

    # -------------------------------------------------------------- 诊断
    def describe(self) -> str:
        parts = [self.resolver.describe()]
        parts.append(self.registry.describe())
        parts.append(
            f"当前维度: {self.dimension_name} "
            f"({self.section_count} 节, 最低节 Y={self.min_section_y}, DataVersion={self.data_version})"
        )
        return "\n".join(parts)
