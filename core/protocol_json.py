"""
PrismarineJS protocol.json 的运行时解析。

两个用途：
1. **精确 ID 表生成** —— 按 protocol.json 生成「包名 -> ID」表，
   免去逐版本手工维护 VERSION_TABLES（下载一次即缓存，断网可用）。
2. **通用类型求值器** —— protocol.json 把 Slot / 实体元数据 / 装备等
   线结构定义成一棵类型树（container/array/switch/mapper/...）。
   `PacketTypeEvaluator` 按树读包，**按版本精确**：1.20.5+ 的物品组件
   （80+ 种）、实体元数据里的粒子等全部自动覆盖，新版本零维护。

数据来源: https://github.com/PrismarineJS/minecraft-data
（data/pc/<ver>/protocol.json；1.8 起所有版本同为「mapper mappings」结构）
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import nbt
from .datatypes import PacketBuffer
from .logger import setup_logger

logger = setup_logger(__name__)

_DOWNLOAD_BASE = (
    "https://raw.githubusercontent.com/PrismarineJS/minecraft-data/master"
    "/data/pc/{ver}/protocol.json"
)

# 协议号 -> Prismarine 目录（**目录的协议号必须精确匹配**：包 ID 在相邻
# 正式版之间也会移位。2026-10 逐目录核实存在；未列出的协议号返回 None，
# 调用方回退内置表/结构探测）。
# 注意：777（26.3）PrismarineJS 尚无数据目录（26.1=775、26.2=776 都不是 777），
# 内置精确表照常生效，只是拿不到求值器（装备/元数据降级跳过）。
PROTOCOL_JSON_DIRS: dict[int, str] = {
    47: "1.8",
    107: "1.9",
    109: "1.9.2",
    210: "1.10",
    315: "1.11",
    316: "1.11.2",
    335: "1.12",
    338: "1.12.1",
    340: "1.12.2",
    393: "1.13",
    404: "1.13.2",
    480: "1.14.4",
    578: "1.15.2",
    735: "1.16",
    754: "1.16.2",
    755: "1.17",
    756: "1.17.1",
    757: "1.18",
    758: "1.18.2",
    759: "1.19",
    760: "1.19.2",
    761: "1.19.3",
    762: "1.19.4",
    763: "1.20",
    764: "1.20.2",
    766: "1.20.5",
    767: "1.21.1",
    768: "1.21.3",
    769: "1.21.4",
    770: "1.21.5",
    771: "1.21.6",
    772: "1.21.8",
    773: "1.21.9",
    774: "1.21.11",
}


def download_protocol_json(
    protocol_version: int | None,
    data_dir: str | Path,
    allow_download: bool = True,
) -> dict[str, Any] | None:
    """加载（必要时下载并缓存）该协议版本的 protocol.json。失败返回 None。"""
    if protocol_version is None:
        return None
    version_dir = PROTOCOL_JSON_DIRS.get(protocol_version)
    if version_dir is None:
        return None

    base = Path(data_dir)
    shared = _shared_data_dir()
    for candidate in (shared / f"protocol_{protocol_version}.json",
                      base / f"protocol_{protocol_version}.json"):
        if candidate.exists():
            return _load_file(candidate)
    if not allow_download:
        return None

    url = _DOWNLOAD_BASE.format(ver=version_dir)
    # 首连会同步下载（约几秒），打出进度避免「卡住不动」的错觉
    logger.info(f"正在下载 protocol.json（{version_dir}，首次运行需要联网）...")
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MinecraftProxy"})
        raw = urllib.request.urlopen(request, timeout=15).read()
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
    except Exception as exc:
        logger.debug(f"下载 protocol.json（{version_dir}）失败: {exc}")
        return None

    dest = shared / f"protocol_{protocol_version}.json"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
    except Exception:
        try:
            base.mkdir(parents=True, exist_ok=True)
            (base / f"protocol_{protocol_version}.json").write_bytes(raw)
        except Exception:
            pass
    logger.info(
        f"已生成协议 {protocol_version} 的运行时 ID 表 "
        f"[protocol.json/{version_dir}，耗时 {time.monotonic() - started:.1f} 秒]"
    )
    return obj


def _load_file(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_bytes())
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _shared_data_dir() -> Path:
    from .block_registry import _shared_data_dir as block_shared_dir
    return block_shared_dir()


# --------------------------------------------------------------------------
# 精确 ID 表生成
# --------------------------------------------------------------------------
# Prismarine 包名 -> 本项目包名（未列出的名字原样保留，仅用于日志显示）。
# 同一个包在不同版本目录里的名字不同（1.16/1.18 用老命名），都要收录。
_PRISMARINE_ALIASES: dict[str, str] = {
    # 区块 / 方块
    "map_chunk": "Chunk Data",
    "map_chunk_bulk": "Map Chunk Bulk",
    "unload_chunk": "Unload Chunk",
    "tile_entity_data": "Block Entity Data",
    "block_action": "Block Action",
    # 登录 / 维度
    "login": "Login (play)",
    "respawn": "Respawn",
    "position": "Position",
    "position_look": "Position Look",
    "success": "Login Success",
    "compression": "Set Compression",
    "encryption_begin": "Encryption Request",
    "login_start": "Login Start",
    # 容器
    "window_items": "Window Items",
    "set_slot": "Set Slot",
    "open_window": "Open Window",
    "open_screen": "Open Screen",
    "close_window": "Close Window",
    # 实体
    "spawn_entity": "Spawn Entity",
    "spawn_entity_living": "Spawn Entity",
    "named_entity_spawn": "Spawn Player",
    "spawn_player": "Spawn Player",
    "entity_destroy": "Entity Destroy",
    "destroy_entities": "Entity Destroy",
    "entity_teleport": "Entity Teleport",
    "entity_metadata": "Entity Metadata",
    "entity_equipment": "Entity Equipment",
    # 地图 / 玩家操作
    "map": "Map",
    "use_item": "Use Item",
    "block_place": "Use Item On",
    "block_placement": "Use Item On",
    "use_item_on": "Use Item On",
    "set_player_position": "Set Player Position",
    "set_player_position_and_rotation": "Set Player Position and Rotation",
}


def extract_packet_tables(
    proto_json: dict[str, Any],
) -> dict[str, dict[str, int]]:
    """
    从 protocol.json 提取「表键 -> {本项目包名: ID}」。

    表键与 packet_ids.table_key 的四个组合一致：
    LOGIN_CLIENTBOUND / LOGIN_SERVERBOUND / PLAY_CLIENTBOUND / PLAY_SERVERBOUND
    （CONFIGURATION 阶段的包不影响本代理状态机，不提取）
    """
    tables: dict[str, dict[str, int]] = {}
    for state, state_key in (("login", "LOGIN"), ("play", "PLAY")):
        for direction, dir_key in (("toClient", "CLIENTBOUND"), ("toServer", "SERVERBOUND")):
            section = proto_json.get(state, {}).get(direction, {})
            mappings = _packet_mappings(section)
            if not mappings:
                continue
            table: dict[str, int] = {}
            for packet_id, prismarine_name in mappings.items():
                name = _PRISMARINE_ALIASES.get(prismarine_name, prismarine_name)
                # 同名映射到不同 ID 时保留较小 ID（重命名期同名包不可信，探测兜底）
                if name not in table or packet_id < table[name]:
                    table[name] = packet_id
            tables[f"{state_key}_{dir_key}"] = table
    return tables


def _packet_mappings(section: dict[str, Any]) -> dict[int, str]:
    """取一个方向上的 {id: prismarine 包名}；两种历史 schema 都支持。"""
    types = section.get("types", {})
    packet = types.get("packet")
    if isinstance(packet, list):
        # 新 schema: ["container", [{"name":"name","type":["mapper",{mappings}]}, ...]]
        for field in packet[1]:
            if isinstance(field, dict) and field.get("name") == "name":
                type_def = field.get("type")
                if isinstance(type_def, list) and type_def[0] == "mapper":
                    raw = type_def[1].get("mappings", {})
                    return {_id(key): str(value) for key, value in raw.items()}
    elif isinstance(packet, dict):
        # 旧 schema: {"name":"mapper","types":{"0":[{name,params}],...}}
        out: dict[int, str] = {}
        for key, value in packet.get("types", {}).items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, list) and value:
                out[index] = str(value[0].get("name", ""))
        return out
    return {}


def _id(key: Any) -> int:
    text = str(key)
    return int(text, 16) if text.lower().startswith("0x") else int(text)


# --------------------------------------------------------------------------
# 通用类型求值器
# --------------------------------------------------------------------------
class ProtocolTypeError(Exception):
    pass


class PacketTypeEvaluator:
    """
    按 protocol.json 的类型树从 PacketBuffer 读值。

    用法::

        ev = PacketTypeEvaluator.for_clientbound(proto_json)
        value = ev.read_by_name(buf, "packet_entity_equipment")
    """

    def __init__(self, types: dict[str, Any]):
        self._types = types

    @classmethod
    def for_clientbound(cls, proto_json: dict[str, Any],
                        state: str = "play") -> PacketTypeEvaluator:
        """合并「全局类型 + 该方向类型」作用域（方向定义优先）"""
        merged = dict(proto_json.get("types", {}))
        section = proto_json.get(state, {}).get("toClient", {})
        merged.update(section.get("types", {}))
        return cls(merged)

    @classmethod
    def for_serverbound(cls, proto_json: dict[str, Any],
                        state: str = "play") -> PacketTypeEvaluator:
        merged = dict(proto_json.get("types", {}))
        section = proto_json.get(state, {}).get("toServer", {})
        merged.update(section.get("types", {}))
        return cls(merged)

    # -------------------------------------------------------------- 对外接口
    def read_by_name(self, buf: PacketBuffer, type_name: str,
                     context: dict[str, Any] | None = None) -> Any:
        """按类型名读值（找不到定义时抛 ProtocolTypeError）"""
        definition = self._types.get(type_name)
        if definition is None:
            raise ProtocolTypeError(f"protocol.json 未定义类型: {type_name}")
        return self.read(buf, definition, context or {})

    def has_type(self, type_name: str) -> bool:
        return type_name in self._types

    def read(self, buf: PacketBuffer, definition: Any,
             context: dict[str, Any] | None = None) -> Any:
        """按类型定义读值。definition 可能是类型名或 [结构, 参数] 列表。"""
        if isinstance(definition, str):
            return self._read_named(buf, definition, context or {})
        if isinstance(definition, list) and definition:
            kind = definition[0]
            params = definition[1] if len(definition) > 1 else {}
            return self._read_composite(buf, kind, params, context or {})
        raise ProtocolTypeError(f"未知类型定义: {definition!r}")

    # -------------------------------------------------------------- 名字引用
    def _read_named(self, buf: PacketBuffer, name: str,
                    context: dict[str, Any]) -> Any:
        if name == "void":
            return None
        definition = self._types.get(name)
        if definition == "native":
            return self._read_native(buf, name)
        if definition is None:
            # 名字本身就是原生类型（少数文件不写 "native" 包装）
            if name in _NATIVE_NAMES:
                return self._read_native(buf, name)
            raise ProtocolTypeError(f"protocol.json 未定义类型: {name}")
        return self.read(buf, definition, context)

    # -------------------------------------------------------------- 组合类型
    def _read_composite(self, buf: PacketBuffer, kind: str, params: Any,
                        context: dict[str, Any]) -> Any:
        if kind == "pstring":
            # string 常定义为 ["pstring", {"countType": "varint"}]（列表形式）
            count_type = params.get("countType", "varint") if isinstance(params, dict) else "varint"
            length = self.read(buf, count_type, context)
            if not isinstance(length, int) or length < 0 or length > 4 * 1024 * 1024:
                raise ProtocolTypeError(f"字符串长度非法: {length}")
            return buf.read(length).decode("utf-8", errors="replace")

        if kind == "container":
            result: dict[str, Any] = {}
            for field in params:
                name = field.get("name")
                value = self.read(buf, field.get("type"), result)
                if name:
                    result[name] = value
                elif isinstance(value, dict):
                    # 匿名字段（如 Slot 的 switch）：把内部字段并入当前容器，
                    # 否则 itemId / components 等全部丢失
                    result.update(value)
            return result

        if kind == "array":
            count = self._array_count(buf, params, context)
            if count is None:
                raise ProtocolTypeError("array 缺少 count/countType")
            if count < 0 or count > 65536:
                raise ProtocolTypeError(f"array 数量非法: {count}")
            element_type = params.get("type")
            return [self.read(buf, element_type, context) for _ in range(count)]

        if kind == "switch":
            if not isinstance(params, dict):
                raise ProtocolTypeError("switch 参数非法")
            key = context.get(params.get("compareTo", ""))
            fields = params.get("fields", {})
            target = None
            for candidate in self._switch_key_candidates(key):
                if str(candidate) in fields:
                    target = fields[str(candidate)]
                    break
            if target is None:
                default = params.get("default")
                if default is None:
                    return None
                return self.read(buf, default, context)
            return self.read(buf, target, context)

        if kind == "option":
            if buf.read_bool():
                return self.read(buf, params, context)
            return None

        if kind == "mapper":
            if not isinstance(params, dict):
                raise ProtocolTypeError("mapper 参数非法")
            raw = self.read(buf, params.get("type", "varint"), context)
            mappings = params.get("mappings", {})
            for candidate in (raw, str(raw), f"0x{raw:02X}"):
                if str(candidate) in mappings:
                    return mappings[str(candidate)]
            return raw

        if kind == "bitfield":
            value = buf.read_ulong()
            bits_out: dict[str, Any] = {}
            for bit in params:
                size = int(bit.get("size", 1))
                bits_out[bit.get("name", "")] = value & ((1 << size) - 1)
                value >>= size
            return bits_out

        if kind == "topBitSetTerminatedArray":
            # 元素首字段（i8）最高位为 1 表示最后一项（Set Equipment 等）
            out: list[Any] = []
            for _ in range(512):
                element = self.read(buf, params.get("type"), context)
                if not isinstance(element, dict) or not element:
                    break
                first_key = next(iter(element))
                raw_first = element[first_key]
                element[first_key] = int(raw_first) & 0x7F
                out.append(element)
                if int(raw_first) < 0:      # i8 符号位 = 终止标记
                    break
            return out

        if kind == "entityMetadataLoop":
            # 循环读 entry；结束符是**单字节** endVal（0xFF），先探测再消费
            entries: list[Any] = []
            entry_type = params.get("type")
            end_val = int(params.get("endVal", 0xFF))
            for _ in range(512):
                if buf.at_end():
                    break
                if buf.peek_ubyte() == end_val:
                    buf.read(1)
                    break
                entry = self.read(buf, entry_type, context)
                if not isinstance(entry, dict):
                    break
                entries.append(entry)
            return entries

        if kind == "buffer":
            if isinstance(params, dict):
                count = self._array_count(buf, params, context)
                if count is not None:
                    if count < 0 or count > 16 * 1024 * 1024:
                        raise ProtocolTypeError(f"buffer 长度非法: {count}")
                    return buf.read(count)
            return buf.read_rest()

        raise ProtocolTypeError(f"求值器不支持的结构: {kind}")

    @staticmethod
    def _switch_key_candidates(key: Any) -> list[Any]:
        """switch 的字段键是 JSON 字符串：true/false/十进制数字/包名"""
        if isinstance(key, bool):
            return ["true" if key else "false", key]
        return [key, str(key)]

    def _array_count(self, buf: PacketBuffer, params: dict[str, Any],
                     context: dict[str, Any]) -> int | None:
        count = params.get("count")
        if isinstance(count, int):
            return count
        if isinstance(count, str):
            # count 也可以是字段名引用（如 "addedComponentCount"）
            value = context.get(count)
            return int(value) if isinstance(value, int) else None
        count_type = params.get("countType")
        if count_type:
            value = self.read(buf, count_type, context)
            return int(value) if isinstance(value, int) else None
        return None

    # -------------------------------------------------------------- 原生类型
    def _read_native(self, buf: PacketBuffer, name: str) -> Any:
        if name in ("varint", "optvarint"):
            return buf.read_varint()
        if name == "varlong":
            return buf.read_varlong()
        if name == "bool":
            return buf.read_bool()
        if name == "u8":
            return buf.read_ubyte()
        if name == "i8":
            return buf.read_byte()
        if name == "u16":
            return buf.read_ushort()
        if name == "i16":
            return buf.read_short()
        if name == "u32":
            return buf.read_uint()
        if name == "i32":
            return buf.read_int()
        if name == "u64":
            return buf.read_ulong()
        if name == "i64":
            return buf.read_long()
        if name == "f32":
            return buf.read_float()
        if name == "f64":
            return buf.read_double()
        if name in ("string", "pstring"):
            return buf.read_string(262144)
        if name == "anonymousNbt":
            value, end = nbt.parse_anonymous_tag(buf.data, buf.offset)
            buf.offset = end
            return value
        if name in ("anonOptionalNbt", "optionalNbt"):
            if buf.peek_ubyte() == 0:
                buf.read(1)
                return None
            return self._read_native(buf, "anonymousNbt")
        if name == "restBuffer":
            return buf.read_rest()
        if name == "lpVec3":
            return [buf.read_varint() for _ in range(3)]
        if name == "uuid":
            return buf.read_uuid()
        if name == "position":
            return buf.read_position()
        raise ProtocolTypeError(f"不支持的原生类型: {name}")


_NATIVE_NAMES = {
    "varint", "varlong", "optvarint", "bool", "u8", "i8", "u16", "i16",
    "u32", "i32", "u64", "i64", "f32", "f64", "string", "pstring",
    "anonymousNbt", "anonOptionalNbt", "optionalNbt", "restBuffer",
    "lpVec3", "uuid", "position", "void",
}


# --------------------------------------------------------------------------
# Slot 值 -> Anvil 物品
# --------------------------------------------------------------------------
def slot_to_item(value: Any, item_names: dict[int, str]) -> dict[str, Any] | None:
    """
    把求值器读出的 Slot 值转成 Anvil 物品结构（与 containers._build_item 同构）。

    * 1.20.5+：{"itemCount": n, "itemId": id, "components": [...]}
    * 1.17 ~ 1.20.4：option 包着 {itemId, itemCount, nbtData}（None = 空槽）
    * 1.8 ~ 1.16：{itemId(-1=空), itemCount, nbtData}

    返回 {} 表示空槽；None 表示结构不认识。
    """
    if value is None:
        return {}          # option 风格的空槽
    if not isinstance(value, dict):
        return None
    if value.get("present") is False:
        return {}
    count = value.get("itemCount")
    item_id = value.get("itemId")
    if item_id is None:
        item_id = value.get("item")
    if item_id == -1 or count == 0:
        return {}          # itemCount=0（1.20.5+ 空槽）或 itemId=-1（旧风格空槽）
    if not isinstance(item_id, int) or not isinstance(count, int):
        return None

    entry: dict[str, Any] = {
        "id": nbt.String(item_names.get(item_id, f"minecraft:unknown_{item_id}")),
        "Count": nbt.Byte(max(1, min(64, count))),
    }
    nbt_value = value.get("nbtData")
    if isinstance(nbt_value, dict) and nbt_value:
        entry["tag"] = nbt.compound(nbt_value)
    components = value.get("components")
    if isinstance(components, list):
        # 1.20.5+：custom_data 的内层复合暂存为 tag；
        # 其余组件（名称/附魔等）磁盘格式与线上不同，暂不落盘（只保 id+数量）
        for component in components:
            if isinstance(component, dict) and component.get("type") == "custom_data":
                data = component.get("data")
                if isinstance(data, dict) and data:
                    entry["tag"] = nbt.compound(data)
                    break
    return entry
