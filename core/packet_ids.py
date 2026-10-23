"""
数据包 ID 自动适配（版本自适应）

背景
----
Minecraft Java 版的数据包 ID 会随协议版本变化，官方文档明确说明
「硬编码 ID 是强烈不推荐的」。因此本模块提供两级解析：

1. **版本表**：对已知协议版本给出精确的 ID 映射（目前内置 777 = 26.3，
   数据取自 minecraft.wiki 官方协议页）。
2. **结构探测（structural probing）**：不依赖版本号，直接根据负载的
   二进制结构判断包身份。这对本代理关心的包（压缩、区块、光照、
   方块实体、状态切换）完全可行，因此**任何版本都能自动适配**。

解析结果会被缓存，未命中的 ID 只探测一次，保证转发性能。
"""

from __future__ import annotations

from .chunk_format import (
    parse_block_entity_data_packet,
    parse_chunk_data_packet,
    parse_update_light_packet,
)
from .datatypes import PacketBuffer, read_varint_bytes

# --------------------------------------------------------------------------
# 状态 / 方向
# --------------------------------------------------------------------------
STATE_HANDSHAKE = "HANDSHAKE"
STATE_STATUS = "STATUS"
STATE_LOGIN = "LOGIN"
STATE_CONFIGURATION = "CONFIGURATION"
STATE_PLAY = "PLAY"

DIR_CLIENTBOUND = "CLIENTBOUND"
DIR_SERVERBOUND = "SERVERBOUND"


# --------------------------------------------------------------------------
# 协议版本 -> 游戏版本（尽力而为，仅用于日志显示）
#
# 数据来源: https://minecraft.wiki/w/Protocol_version
# 同一协议号对应多个游戏版本时，用 " / " 连接（它们协议兼容）。
# --------------------------------------------------------------------------
PROTOCOL_VERSION_NAMES: dict[int, str] = {
    47: "1.8.9",
    754: "1.16.5",
    758: "1.18.2",
    763: "1.20 / 1.20.1",
    764: "1.20.2",
    765: "1.20.3 / 1.20.4",
    766: "1.20.5 / 1.20.6",
    767: "1.21 / 1.21.1",
    768: "1.21.2 / 1.21.3",
    769: "1.21.4",
    770: "1.21.5",
    771: "1.21.6",
    772: "1.21.7 / 1.21.8",
    773: "1.21.9 / 1.21.10",
    774: "1.21.11",
    777: "26.3",
}

# 从该版本起引入了 Configuration 状态（Login -> Configuration -> Play）
CONFIGURATION_STATE_MIN_VERSION = 764


# --------------------------------------------------------------------------
# 精确版本表（仅收录经过核对的版本）
# --------------------------------------------------------------------------
# 数据来源: https://minecraft.wiki/w/Java_Edition_protocol/Packets （协议 777）
VERSION_TABLES: dict[int, dict[str, dict[str, int]]] = {
    # ---------------------------------------------------------------- 1.8.9
    # 数据来源: PrismarineJS minecraft-data pc/1.8/proto.yml
    # 注意: 1.8 的区块包格式与 1.9+ 完全不同，必须依赖精确表（无法结构探测）
    47: {
        "LOGIN_CLIENTBOUND": {
            "Disconnect": 0x00,
            "Encryption Request": 0x01,
            "Login Success": 0x02,
            "Set Compression": 0x03,
        },
        "LOGIN_SERVERBOUND": {
            "Login Start": 0x00,
            "Encryption Response": 0x01,
        },
        "PLAY_CLIENTBOUND": {
            "Keep Alive": 0x00,
            "Login (play)": 0x01,
            "Chat Message": 0x02,
            "Set Time": 0x03,
            "Set Default Spawn Position": 0x05,
            "Set Health": 0x06,
            "Respawn": 0x07,
            "Synchronize Player Position": 0x08,
            "Chunk Data": 0x21,
            "Map Chunk Bulk": 0x26,
            "Block Entity Data": 0x35,
            "Disconnect": 0x40,
            "Set Compression": 0x46,
        },
        "PLAY_SERVERBOUND": {
            "Keep Alive": 0x00,
            "Chat Message": 0x01,
            "Client Information": 0x15,
            "Client Status": 0x16,
        },
    },
    # --------------------------------------------------------------- 1.16.5
    # 数据来源: PrismarineJS minecraft-data pc/1.16.2/proto.yml（同协议号 754）
    754: {
        "LOGIN_CLIENTBOUND": {
            "Disconnect": 0x00,
            "Encryption Request": 0x01,
            "Login Success": 0x02,
            "Set Compression": 0x03,
            "Login Plugin Request": 0x04,
        },
        "LOGIN_SERVERBOUND": {
            "Login Start": 0x00,
            "Encryption Response": 0x01,
            "Login Plugin Response": 0x02,
        },
        "PLAY_CLIENTBOUND": {
            "Block Entity Data": 0x09,
            "Disconnect": 0x19,
            "Unload Chunk": 0x1C,
            "Keep Alive": 0x1F,
            "Chunk Data": 0x20,
            "Update Light": 0x23,
            "Login (play)": 0x24,
            "Respawn": 0x39,
        },
        "PLAY_SERVERBOUND": {
            "Confirm Teleportation": 0x00,
            "Client Information": 0x05,
            "Keep Alive": 0x10,
        },
    },
    # --------------------------------------------------------------- 1.18.2
    # 数据来源: PrismarineJS minecraft-data pc/1.18/proto.yml（同协议号 758）
    758: {
        "LOGIN_CLIENTBOUND": {
            "Disconnect": 0x00,
            "Encryption Request": 0x01,
            "Login Success": 0x02,
            "Set Compression": 0x03,
            "Login Plugin Request": 0x04,
        },
        "LOGIN_SERVERBOUND": {
            "Login Start": 0x00,
            "Encryption Response": 0x01,
            "Login Plugin Response": 0x02,
        },
        "PLAY_CLIENTBOUND": {
            "Block Entity Data": 0x0A,
            "Disconnect": 0x1A,
            "Unload Chunk": 0x1D,
            "Keep Alive": 0x21,
            "Chunk Data and Update Light": 0x22,
            "Update Light": 0x25,
            "Login (play)": 0x26,
            "Respawn": 0x3D,
        },
        "PLAY_SERVERBOUND": {
            "Confirm Teleportation": 0x00,
            "Client Information": 0x05,
            "Keep Alive": 0x0F,
        },
    },
    # --------------------------------------------------------- 1.21.9 / 1.21.10
    # 数据来源: PrismarineJS minecraft-data pc/1.21.9/protocol.json（协议号 773）
    # 注意: ViaVersion/ViaBackwards 只翻译包 ID 与状态机，**不翻译区块负载**，
    # 因此线路上的 ID 就是标准 1.21.9 表，但区块内容可能是旧核心格式
    # （见 chunk_format._parse_sections_adaptive）。
    773: {
        "LOGIN_CLIENTBOUND": {
            "Disconnect": 0x00,
            "Encryption Begin": 0x01,
            "Login Success": 0x02,
            "Set Compression": 0x03,
            "Login Plugin Request": 0x04,
            "Cookie Request": 0x05,
        },
        "LOGIN_SERVERBOUND": {
            "Login Start": 0x00,
            "Encryption Begin": 0x01,
            "Login Plugin Response": 0x02,
            "Login Acknowledged": 0x03,
            "Cookie Response": 0x04,
        },
        "CONFIGURATION_CLIENTBOUND": {
            "Cookie Request": 0x00,
            "Plugin Message": 0x01,
            "Disconnect": 0x02,
            "Finish Configuration": 0x03,
            "Keep Alive": 0x04,
            "Ping": 0x05,
            "Reset Chat": 0x06,
            "Registry Data": 0x07,
            "Remove Resource Pack": 0x08,
            "Add Resource Pack": 0x09,
            "Store Cookie": 0x0A,
            "Transfer": 0x0B,
            "Feature Flags": 0x0C,
            "Tags": 0x0D,
            "Select Known Packs": 0x0E,
            "Custom Report Details": 0x0F,
            "Server Links": 0x10,
            "Clear Dialog": 0x11,
            "Show Dialog": 0x12,
            "Code Of Conduct": 0x13,
        },
        "CONFIGURATION_SERVERBOUND": {
            "Settings": 0x00,
            "Cookie Response": 0x01,
            "Plugin Message": 0x02,
            "Acknowledge Configuration": 0x03,
            "Keep Alive": 0x04,
            "Pong": 0x05,
            "Resource Pack Receive": 0x06,
            "Select Known Packs": 0x07,
            "Custom Click Action": 0x08,
            "Accept Code Of Conduct": 0x09,
        },
        "PLAY_CLIENTBOUND": {
            "Bundle Delimiter": 0x00,
            "Spawn Entity": 0x01,
            "Animation": 0x02,
            "Statistics": 0x03,
            "Acknowledge Player Digging": 0x04,
            "Block Break Animation": 0x05,
            "Block Entity Data": 0x06,
            "Block Action": 0x07,
            "Block Change": 0x08,
            "Boss Bar": 0x09,
            "Difficulty": 0x0A,
            "Chunk Batch Finished": 0x0B,
            "Chunk Batch Start": 0x0C,
            "Chunk Biomes": 0x0D,
            "Clear Titles": 0x0E,
            "Tab Complete": 0x0F,
            "Declare Commands": 0x10,
            "Close Window": 0x11,
            "Window Items": 0x12,
            "Craft Progress Bar": 0x13,
            "Set Slot": 0x14,
            "Cookie Request": 0x15,
            "Set Cooldown": 0x16,
            "Chat Suggestions": 0x17,
            "Plugin Message": 0x18,
            "Damage Event": 0x19,
            "Debug Block Value": 0x1A,
            "Debug Chunk Value": 0x1B,
            "Debug Entity Value": 0x1C,
            "Debug Event": 0x1D,
            "Debug Sample": 0x1E,
            "Hide Message": 0x1F,
            "Disconnect": 0x20,
            "Profileless Chat": 0x21,
            "Entity Status": 0x22,
            "Sync Entity Position": 0x23,
            "Explosion": 0x24,
            "Unload Chunk": 0x25,
            "Game State Change": 0x26,
            "Game Test Highlight Pos": 0x27,
            "Open Horse Window": 0x28,
            "Hurt Animation": 0x29,
            "Initialize World Border": 0x2A,
            "Keep Alive": 0x2B,
            "Chunk Data and Update Light": 0x2C,
            "World Event": 0x2D,
            "World Particles": 0x2E,
            "Update Light": 0x2F,
            "Login (play)": 0x30,
            "Map": 0x31,
            "Trade List": 0x32,
            "Rel Entity Move": 0x33,
            "Entity Move Look": 0x34,
            "Move Minecart": 0x35,
            "Entity Look": 0x36,
            "Vehicle Move": 0x37,
            "Open Book": 0x38,
            "Open Window": 0x39,
            "Open Sign Entity": 0x3A,
            "Ping": 0x3B,
            "Ping Response": 0x3C,
            "Craft Recipe Response": 0x3D,
            "Abilities": 0x3E,
            "Player Chat": 0x3F,
            "End Combat Event": 0x40,
            "Enter Combat Event": 0x41,
            "Death Combat Event": 0x42,
            "Player Remove": 0x43,
            "Player Info": 0x44,
            "Face Player": 0x45,
            "Position": 0x46,
            "Player Rotation": 0x47,
            "Recipe Book Add": 0x48,
            "Recipe Book Remove": 0x49,
            "Recipe Book Settings": 0x4A,
            "Entity Destroy": 0x4B,
            "Remove Entity Effect": 0x4C,
            "Reset Score": 0x4D,
            "Remove Resource Pack": 0x4E,
            "Add Resource Pack": 0x4F,
            "Respawn": 0x50,
            "Entity Head Rotation": 0x51,
            "Multi Block Change": 0x52,
            "Select Advancement Tab": 0x53,
            "Server Data": 0x54,
            "Action Bar": 0x55,
            "World Border Center": 0x56,
            "World Border Lerp Size": 0x57,
            "World Border Size": 0x58,
            "World Border Warning Delay": 0x59,
            "World Border Warning Reach": 0x5A,
            "Camera": 0x5B,
            "Update View Position": 0x5C,
            "Update View Distance": 0x5D,
            "Set Cursor Item": 0x5E,
            "Spawn Position": 0x5F,
            "Scoreboard Display Objective": 0x60,
            "Entity Metadata": 0x61,
            "Attach Entity": 0x62,
            "Entity Velocity": 0x63,
            "Entity Equipment": 0x64,
            "Experience": 0x65,
            "Update Health": 0x66,
            "Held Item Slot": 0x67,
            "Scoreboard Objective": 0x68,
            "Set Passengers": 0x69,
            "Set Player Inventory": 0x6A,
            "Teams": 0x6B,
            "Scoreboard Score": 0x6C,
            "Simulation Distance": 0x6D,
            "Set Title Subtitle": 0x6E,
            "Update Time": 0x6F,
            "Set Title Text": 0x70,
            "Set Title Time": 0x71,
            "Entity Sound Effect": 0x72,
            "Sound Effect": 0x73,
            "Start Configuration": 0x74,
            "Stop Sound": 0x75,
            "Store Cookie": 0x76,
            "System Chat": 0x77,
            "Playerlist Header": 0x78,
            "Nbt Query Response": 0x79,
            "Collect": 0x7A,
            "Entity Teleport": 0x7B,
            "Test Instance Block Status": 0x7C,
            "Set Ticking State": 0x7D,
            "Step Tick": 0x7E,
            "Transfer": 0x7F,
            "Advancements": 0x80,
            "Entity Update Attributes": 0x81,
            "Entity Effect": 0x82,
            "Declare Recipes": 0x83,
            "Tags": 0x84,
            "Set Projectile Power": 0x85,
            "Custom Report Details": 0x86,
            "Server Links": 0x87,
            "Tracked Waypoint": 0x88,
            "Clear Dialog": 0x89,
            "Show Dialog": 0x8A,
        },
        "PLAY_SERVERBOUND": {
            "Confirm Teleportation": 0x00,
            "Query Block Nbt": 0x01,
            "Select Bundle Item": 0x02,
            "Set Difficulty": 0x03,
            "Change Gamemode": 0x04,
            "Message Acknowledgement": 0x05,
            "Chat Command": 0x06,
            "Chat Command Signed": 0x07,
            "Chat Message": 0x08,
            "Chat Session Update": 0x09,
            "Chunk Batch Received": 0x0A,
            "Client Command": 0x0B,
            "Tick End": 0x0C,
            "Settings": 0x0D,
            "Tab Complete": 0x0E,
            "Configuration Acknowledged": 0x0F,
            "Enchant Item": 0x10,
            "Window Click": 0x11,
            "Close Window": 0x12,
            "Set Slot State": 0x13,
            "Cookie Response": 0x14,
            "Plugin Message": 0x15,
            "Debug Subscription Request": 0x16,
            "Edit Book": 0x17,
            "Query Entity Nbt": 0x18,
            "Use Entity": 0x19,
            "Generate Structure": 0x1A,
            "Keep Alive": 0x1B,
            "Lock Difficulty": 0x1C,
            "Position": 0x1D,
            "Position Look": 0x1E,
            "Look": 0x1F,
            "Flying": 0x20,
            "Vehicle Move": 0x21,
            "Steer Boat": 0x22,
            "Pick Item From Block": 0x23,
            "Pick Item From Entity": 0x24,
            "Ping Request": 0x25,
            "Craft Recipe Request": 0x26,
            "Abilities": 0x27,
            "Block Dig": 0x28,
            "Entity Action": 0x29,
            "Player Input": 0x2A,
            "Player Loaded": 0x2B,
            "Pong": 0x2C,
            "Recipe Book": 0x2D,
            "Displayed Recipe": 0x2E,
            "Name Item": 0x2F,
            "Resource Pack Receive": 0x30,
            "Advancement Tab": 0x31,
            "Select Trade": 0x32,
            "Set Beacon Effect": 0x33,
            "Held Item Slot": 0x34,
            "Update Command Block": 0x35,
            "Update Command Block Minecart": 0x36,
            "Set Creative Slot": 0x37,
            "Update Jigsaw Block": 0x38,
            "Update Structure Block": 0x39,
            "Set Test Block": 0x3A,
            "Update Sign": 0x3B,
            "Arm Animation": 0x3C,
            "Spectate": 0x3D,
            "Test Instance Block Action": 0x3E,
            "Block Place": 0x3F,
            "Use Item": 0x40,
            "Custom Click Action": 0x41,
        },
    },
    777: {
        "LOGIN_CLIENTBOUND": {
            "Disconnect": 0x00,
            "Encryption Request": 0x01,
            "Login Success": 0x02,
            "Set Compression": 0x03,
            "Login Plugin Request": 0x04,
            "Cookie Request": 0x05,
        },
        "LOGIN_SERVERBOUND": {
            "Login Start": 0x00,
            "Encryption Response": 0x01,
            "Login Plugin Response": 0x02,
            "Login Acknowledged": 0x03,
            "Cookie Response": 0x04,
        },
        "CONFIGURATION_CLIENTBOUND": {
            "Cookie Request": 0x00,
            "Plugin Message": 0x01,
            "Disconnect": 0x02,
            "Finish Configuration": 0x03,
            "Keep Alive": 0x04,
            "Ping": 0x05,
            "Reset Chat": 0x06,
            "Registry Data": 0x07,
            "Remove Resource Pack": 0x08,
            "Add Resource Pack": 0x09,
            "Post Effects": 0x0A,
            "Store Cookie": 0x0B,
            "Transfer": 0x0C,
            "Feature Flags": 0x0D,
            "Update Tags": 0x0E,
            "Known Packs": 0x0F,
            "Custom Report Details": 0x10,
            "Server Links": 0x11,
            "Clear Dialog": 0x12,
            "Show Dialog": 0x13,
            "Code of Conduct": 0x14,
        },
        "CONFIGURATION_SERVERBOUND": {
            "Client Information": 0x00,
            "Cookie Response": 0x01,
            "Plugin Message": 0x02,
            "Acknowledge Configuration": 0x03,
            "Keep Alive": 0x04,
            "Pong": 0x05,
            "Resource Pack Response": 0x06,
            "Known Packs": 0x07,
            "Custom Click Action": 0x08,
            "Accept Code of Conduct": 0x09,
        },
        "PLAY_CLIENTBOUND": {
            "Bundle Delimiter": 0x00,
            "Block Entity Data": 0x06,
            "Chunk Batch Finished": 0x0B,
            "Chunk Batch Start": 0x0C,
            "Chunk Biomes": 0x0D,
            "Disconnect": 0x20,
            "Keep Alive": 0x2D,
            "Chunk Data and Update Light": 0x2E,
            "Update Light": 0x31,
            "Login (play)": 0x32,
            "Set Center Chunk": 0x60,
            "Unload Chunk": 0x26,
            "Start Configuration": 0x78,
            "Respawn": 0x54,
            "Synchronize Player Position": 0x49,
            "Set Default Spawn Position": 0x63,
            "Set Time": 0x73,
        },
        "PLAY_SERVERBOUND": {
            "Confirm Teleportation": 0x00,
            "Chat Message": 0x09,
            "Chunk Batch Received": 0x0B,
            "Client Information": 0x0E,
            "Acknowledge Configuration": 0x10,
            "Keep Alive": 0x1C,
            "Set Player Position": 0x1E,
            "Set Player Position and Rotation": 0x1F,
            "Set Player Rotation": 0x20,
            "Player Action": 0x29,
            "Use Item On": 0x42,
            "Use Item": 0x43,
        },
    },
}


def table_key(state: str, direction: str) -> str:
    return f"{state}_{direction}"


# 同一 packet_id 结构探测的最大重试次数（避免每个包都跑探测器）
MAX_PROBE_ATTEMPTS = 8


# --------------------------------------------------------------------------
# 结构探测器
# --------------------------------------------------------------------------
def _looks_like_identifier(text: str) -> bool:
    if not text or len(text) > 128:
        return False
    if ":" not in text:
        return False
    namespace, _, path = text.partition(":")
    if not namespace or not path:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-. /")
    return all(c in allowed for c in text.lower())


def _looks_like_player_name(text: str) -> bool:
    if not text or len(text) > 16:
        return False
    return all(c.isalnum() or c == "_" for c in text)


def detect_login_clientbound(payload: bytes) -> str | None:
    # Set Compression: 负载恰好是一个 VarInt
    value, used = read_varint_bytes(payload, 0)
    if used and used == len(payload) and -1 <= value <= 2_097_151:
        return "Set Compression"

    # Login Success: UUID(16) + String name + Prefixed Array of properties
    if len(payload) >= 17:
        name_len, n = read_varint_bytes(payload, 16)
        if n and 1 <= name_len <= 16 and 16 + n + name_len <= len(payload):
            try:
                name = payload[16 + n:16 + n + name_len].decode("ascii")
            except UnicodeDecodeError:
                name = None
            if name and _looks_like_player_name(name):
                return "Login Success"

    # Disconnect: JSON 文本
    try:
        buf = PacketBuffer(payload)
        text = buf.read_string(262144)
        if text.strip().startswith(("{", '"')) and buf.at_end():
            return "Disconnect"
    except Exception:
        pass
    return None


def detect_login_serverbound(payload: bytes) -> str | None:
    if len(payload) == 0:
        return "Login Acknowledged"
    try:
        buf = PacketBuffer(payload)
        name = buf.read_string(16)
        if _looks_like_player_name(name):
            return "Login Start"
    except Exception:
        pass
    return None


def detect_configuration_clientbound(payload: bytes) -> str | None:
    if len(payload) == 0:
        # Reset Chat 与 Finish Configuration 都是空负载，靠版本表区分
        return "Empty Configuration Packet"
    return None


def detect_configuration_serverbound(payload: bytes) -> str | None:
    if len(payload) == 0:
        return "Acknowledge Configuration"
    return None


def _detect_play_join_game(payload: bytes) -> bool:
    """Login (play) 包：Int entity id + Bool hardcore + Prefixed Array of Identifier"""
    if len(payload) < 10:
        return False
    try:
        buf = PacketBuffer(payload)
        buf.read_int()
        buf.read_bool()
        count = buf.read_varint()
        if not (1 <= count <= 64):
            return False
        for _ in range(count):
            if not _looks_like_identifier(buf.read_identifier()):
                return False
        return buf.remaining >= 16
    except Exception:
        return False


def detect_play_clientbound(payload: bytes) -> str | None:
    if parse_chunk_data_packet(payload) is not None:
        return "Chunk Data and Update Light"
    if parse_update_light_packet(payload) is not None:
        return "Update Light"
    if parse_block_entity_data_packet(payload) is not None:
        return "Block Entity Data"
    if _detect_play_join_game(payload):
        return "Login (play)"
    return None


def detect_play_serverbound(payload: bytes) -> str | None:
    # Confirm Teleportation: 单个 VarInt
    value, used = read_varint_bytes(payload, 0)
    if used and used == len(payload) and 0 <= value <= 2_097_151:
        return "Confirm Teleportation"
    # Chunk Batch Received: 单个 Float
    if len(payload) == 4:
        return "Chunk Batch Received"
    return None


_DETECTORS = {
    (STATE_LOGIN, DIR_CLIENTBOUND): detect_login_clientbound,
    (STATE_LOGIN, DIR_SERVERBOUND): detect_login_serverbound,
    (STATE_CONFIGURATION, DIR_CLIENTBOUND): detect_configuration_clientbound,
    (STATE_CONFIGURATION, DIR_SERVERBOUND): detect_configuration_serverbound,
    (STATE_PLAY, DIR_CLIENTBOUND): detect_play_clientbound,
    (STATE_PLAY, DIR_SERVERBOUND): detect_play_serverbound,
}


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------
class PacketIdResolver:
    """
    根据协议版本与包结构解析数据包名称。

    用法::

        resolver.set_protocol_version(777)
        name = resolver.resolve("PLAY", "CLIENTBOUND", 0x2E, payload)
    """

    def __init__(self, debug: bool = False):
        self.debug = debug
        self.protocol_version: int | None = None
        self.version_name: str = "未知"
        # table:    名称 -> ID
        # _by_name: ID   -> 名称（用于按 ID 反查）
        self.table: dict[str, dict[str, int]] = {}
        self._by_name: dict[str, dict[int, str]] = {}
        self._cache: dict[tuple[str, str, int], str | None] = {}
        self._probe_attempts: dict[tuple[str, str, int], int] = {}
        self.stats: dict[str, int] = {}
        self.unidentified: dict[tuple[str, str, int], int] = {}

    # -------------------------------------------------------------- 版本设置
    def set_protocol_version(self, version: int) -> None:
        if version == self.protocol_version:
            return
        self.protocol_version = version
        self.version_name = PROTOCOL_VERSION_NAMES.get(version, f"协议 {version}")
        self.table = VERSION_TABLES.get(version, {})
        self._by_name = {
            key: {pid: name for name, pid in ids.items()}
            for key, ids in self.table.items()
        }
        self._cache.clear()
        self._probe_attempts.clear()

    @property
    def has_exact_table(self) -> bool:
        return bool(self.table)

    def uses_configuration_state(self) -> bool:
        if self.protocol_version is None:
            return True
        return self.protocol_version >= CONFIGURATION_STATE_MIN_VERSION

    # ------------------------------------------------------------------ 查询
    def id_for(self, state: str, direction: str, name: str) -> int | None:
        """按名称查询 ID（仅版本表可用时有效）"""
        return self.table.get(table_key(state, direction), {}).get(name)

    def resolve(
        self,
        state: str,
        direction: str,
        packet_id: int,
        payload: bytes,
    ) -> str | None:
        """
        返回包名称；无法识别时返回 None。
        优先使用版本表，其次进行结构探测（探测结果会被缓存）。
        """
        cache_key = (state, direction, packet_id)
        if cache_key in self._cache:
            return self._cache[cache_key]

        name: str | None = None

        # 1) 版本表
        if self.table:
            name = self._by_name.get(table_key(state, direction), {}).get(packet_id)
            if name is not None:
                self._cache[cache_key] = name
                self.stats[name] = self.stats.get(name, 0) + 1
                return name

        # 2) 结构探测。
        #
        # 注意：**不能**把「探测失败」永久固化。同一个 packet_id 在不同时刻
        # 负载完全不同（例如同一 id 先出现小包、后出现大区块包）。若首次探测
        # 失败就再也不重试，真正的区块包会被永久漏掉。
        # 因此这里只在探测**成功**时缓存，失败最多重试 MAX_PROBE_ATTEMPTS 次。
        attempts = self._probe_attempts.get(cache_key, 0)
        if attempts < MAX_PROBE_ATTEMPTS:
            self._probe_attempts[cache_key] = attempts + 1
            detector = _DETECTORS.get((state, direction))
            if detector is not None:
                try:
                    name = detector(payload)
                except Exception:
                    name = None
                if name is not None:
                    self._cache[cache_key] = name
                    self.stats[name] = self.stats.get(name, 0) + 1
                    return name
                self.unidentified[cache_key] = self.unidentified.get(cache_key, 0) + 1

        return None

    # ------------------------------------------------------------------ 诊断
    def describe(self) -> str:
        lines = [
            f"协议版本: {self.protocol_version} ({self.version_name})",
            f"精确 ID 表: {'可用' if self.has_exact_table else '不可用（将使用结构探测）'}",
        ]
        if self.stats:
            lines.append("已识别数据包:")
            for name, count in sorted(self.stats.items(), key=lambda kv: -kv[1])[:12]:
                lines.append(f"  - {name}: {count}")
        return "\n".join(lines)
