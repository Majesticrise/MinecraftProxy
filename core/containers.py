"""
容器内容捕获：把 Window Items / Set Slot 包里的物品写进世界的方块实体。

数据流（被动代理的固有限制）：
    玩家右键容器（serverbound Use Item On / Block Action 的开箱动画）
      -> 服务器发 Open Screen（分配窗口 id）+ Window Items（窗口内容）
         + Set Slot（增量更新，拿取/放入物品时）
      -> 本模块解析物品列表，交由 extractor 合并进该坐标的 block_entities
    玩家关窗（Close Window，双向都有）-> 最终内容再合并一次

因此「打开过」的容器才有内容——这是协议设计使然，所有被动式
world downloader 均如此。

槽位线格式（有 protocol.json 求值器时按版本树精确解析，覆盖 1.20.5+
组件格式；否则回退到内置三风格）：
  * 1.8（47）:        [Short blockId(-1=空)][UByte count][Short damage][可选NBT]
  * 1.13 ~ 1.16.5:    [VarInt itemId(-1=空)][UByte count][可选NBT]
  * 1.17 ~ 1.20.4:    windowItems 头多一个 stateId；槽位为
                       [Bool present][VarInt id][VarInt count][可选NBT]
  * 1.20.5+（766+）:  [VarInt count(0=空)][VarInt itemId]
                       [VarInt 组件数 + 组件][VarInt 移除组件数 + id]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import nbt
from .datatypes import PacketBuffer

# 协议分界
VERSION_SLOT_PRESENT_MIN = 755      # 1.17+：present bool 槽位
VERSION_STATE_ID_MIN = 756          # 1.17.1+：Window Items 带 stateId
VERSION_SET_SLOT_STATE_ID_MIN = 755  # 1.17+：Set Slot 带 stateId

# 「物品栏格子里有多少东西」超过这个就拒绝（防御异常数据）
MAX_SLOTS = 512


@dataclass
class WindowContent:
    """一次 Window Items 的解析结果（物品已转成 Anvil Items 结构）"""
    window_id: int
    items: list[dict[str, Any]] = field(default_factory=list)


def parse_window_items(
    payload: bytes,
    protocol_version: int | None,
    item_names: dict[int, str],
    evaluator: Any = None,
) -> WindowContent | None:
    """
    解析 Window Items 包；结构不符或版本不支持时返回 None（不抛异常）。

    * evaluator（protocol.json 求值器）可用时按版本类型树精确解析，
      覆盖 1.20.5+ 的物品组件格式；
    * protocol_version 未知（结构探测）时依次尝试三种内置槽位格式，
      以「精确消费全部字节」为准——与区块包的自适应是同一思路。
    """
    if evaluator is not None and evaluator.has_type("packet_window_items"):
        content = _parse_window_items_evaluated(payload, item_names, evaluator)
        if content is not None:
            return content
        return None

    if protocol_version is not None:
        styles = [_style_for(protocol_version)]
    else:
        styles = ["present", "classic", "legacy"]
    for style in styles:
        content = _parse_with_style(payload, protocol_version, item_names, style)
        if content is not None:
            return content
    return None


def _parse_window_items_evaluated(payload, item_names, evaluator) -> WindowContent | None:
    try:
        buf = PacketBuffer(payload)
        value = evaluator.read_by_name(buf, "packet_window_items")
        if not buf.at_end() or not isinstance(value, dict):
            return None
        window_id = int(value.get("windowId", -1))
        raw_items = value.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > MAX_SLOTS:
            return None
        items: list[dict[str, Any]] = []
        from .protocol_json import slot_to_item
        for slot_index, raw in enumerate(raw_items):
            entry = slot_to_item(raw, item_names)
            if entry is None:
                return None      # 结构不认识：整包放弃，绝不产生错位数据
            if entry:
                entry["Slot"] = nbt.Byte(slot_index)
                items.append(entry)
        return WindowContent(window_id=window_id, items=items)
    except Exception:
        return None


def parse_set_slot(
    payload: bytes,
    protocol_version: int | None,
    item_names: dict[int, str],
    evaluator: Any = None,
) -> tuple[int, int, dict[str, Any] | None] | None:
    """
    解析 Set Slot 包，返回 (window_id, slot_index, item)。

    item 为 {} 表示该槽被清空，None 表示无法解析。
    """
    if evaluator is not None and evaluator.has_type("packet_set_slot"):
        try:
            buf = PacketBuffer(payload)
            value = evaluator.read_by_name(buf, "packet_set_slot")
            if not buf.at_end() or not isinstance(value, dict):
                return None
            from .protocol_json import slot_to_item
            item = slot_to_item(value.get("item"), item_names)
            if item is None:
                return None
            return int(value.get("windowId", -1)), int(value.get("slot", -1)), item
        except Exception:
            return None

    # 内置格式：UByte windowId, [VarInt stateId(1.17+)], Short slot, slot
    for with_state in _state_id_candidates(protocol_version):
        if protocol_version is None:
            style_candidates: list[str] = ["present", "classic", "legacy"]
        else:
            style_candidates = [_style_for(protocol_version)]
        for style in style_candidates:
            try:
                buf = PacketBuffer(payload)
                window_id = buf.read_ubyte()
                if with_state:
                    buf.read_varint()
                slot_index = buf.read_short()
                if not (0 <= slot_index < MAX_SLOTS):
                    continue
                item = _read_slot(buf, style, item_names)
                if item is not None and buf.at_end():
                    return window_id, slot_index, item
            except Exception:
                continue
    return None


def _state_id_candidates(protocol_version: int | None) -> list[bool]:
    if protocol_version is None:
        return [False, True]
    if protocol_version >= VERSION_SET_SLOT_STATE_ID_MIN:
        return [True]
    return [False]


def _style_for(protocol_version: int) -> str:
    if protocol_version <= 47:
        return "legacy"
    if protocol_version < VERSION_SLOT_PRESENT_MIN:
        return "classic"
    return "present"


def _parse_with_style(payload, protocol_version, item_names,
                      style: str) -> WindowContent | None:
    try:
        buf = PacketBuffer(payload)
        window_id = buf.read_ubyte()
        if style == "legacy":
            count = buf.read_short()  # 1.8 的槽数是 Short
        else:
            if protocol_version is not None and protocol_version >= VERSION_STATE_ID_MIN:
                buf.read_varint()  # state id（1.17.1+）
            count = buf.read_varint()
        if count < 0 or count > MAX_SLOTS:
            return None
        items: list[dict[str, Any]] = []
        for slot_index in range(count):
            slot = _read_slot(buf, style, item_names)
            if slot is None:
                return None  # 结构不认识：整包放弃，绝不产生错位数据
            if slot:
                slot["Slot"] = nbt.Byte(slot_index)
                items.append(slot)
        return WindowContent(window_id=window_id, items=items)
    except Exception:
        return None


def _read_slot(
    buf: PacketBuffer,
    style: str,
    item_names: dict[int, str],
) -> dict[str, Any] | None:
    """读一个槽位。返回 {} 表示空槽，None 表示无法解析。"""
    try:
        if style == "legacy":
            # 1.8：Short blockId（-1 = 空）
            block_id = buf.read_short()
            if block_id == -1:
                return {}
            count = buf.read_ubyte()
            buf.read_short()  # damage
            value = _read_slot_nbt(buf)
            return _build_item(block_id, count, value, item_names)

        if style == "classic":
            # 1.13 ~ 1.16.5：VarInt itemId（-1 = 空）
            item_id = buf.read_varint()
            if item_id == -1:
                return {}
            count = buf.read_ubyte()
            value = _read_slot_nbt(buf)
            return _build_item(item_id, count, value, item_names)

        # present（1.17 ~ 1.20.4）：Bool present
        if not buf.read_bool():
            return {}
        item_id = buf.read_varint()
        count = buf.read_varint()
        value = _read_slot_nbt(buf)
        return _build_item(item_id, count, value, item_names)
    except Exception:
        return None


def _read_slot_nbt(buf: PacketBuffer) -> Any:
    """
    槽位的可选 NBT。线上格式随版本在「具名/匿名」之间摇摆
    （与方块实体 NBT 同一个坑，见 chunk_format._read_block_entity_nbt），
    首字节 0 = 无 NBT。
    """
    if buf.remaining <= 0 or buf.peek_ubyte() == nbt.TAG_END:
        buf.read(1)
        return None
    start = buf.offset
    try:
        value, end = nbt.parse_anonymous_tag(buf.data, start)
    except Exception:
        try:
            value, end = nbt.parse_named_tag(buf.data, start)
        except Exception as exc:
            raise ValueError(f"槽位 NBT 无法解析: {exc}") from exc
    buf.offset = end
    return value


def _build_item(
    item_id: int,
    count: int,
    nbt_value: Any,
    item_names: dict[int, str],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": nbt.String(item_names.get(item_id, f"minecraft:unknown_{item_id}")),
        "Count": nbt.Byte(max(1, min(64, count))),
    }
    if isinstance(nbt_value, dict) and nbt_value:
        entry["tag"] = nbt.compound(nbt_value)
    return entry


CONTAINER_BLOCK_SUFFIXES = (
    "chest", "barrel", "shulker_box", "furnace", "blast_furnace", "smoker",
    "dispenser", "dropper", "hopper", "brewing_stand", "lectern",
)


def container_entity_id(block_name: str | None) -> str | None:
    """方块名 -> 方块实体 id（只有真正的容器才返回非 None）"""
    if not block_name:
        return None
    base = block_name.split(":", 1)[-1]
    if base.endswith("_shulker_box"):
        return "minecraft:shulker_box"
    for suffix in CONTAINER_BLOCK_SUFFIXES:
        if base == suffix:
            return "minecraft:" + suffix
    return None
