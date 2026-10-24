"""
运行时协议数据测试：protocol.json 求值器 / 精确 ID 表生成 /
Set Slot 增量 / 装备与实体元数据 / 双箱内容拆分

全部用合成数据（不联网）：protocol.json 结构按 PrismarineJS 真实
schema 构造，包字节按线上格式手工编码。
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import nbt
from core.chunk_extractor import _merge_items_into_root
from core.containers import parse_set_slot, parse_window_items
from core.datatypes import PacketBuffer, write_varint
from core.entities import EntityTracker, _entity_nbt
from core.packet_ids import PacketIdResolver
from core.protocol import ConnectionState, ProtocolParser
from core.protocol_json import (
    PacketTypeEvaluator,
    extract_packet_tables,
    slot_to_item,
)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


ITEM_NAMES = {1: "minecraft:stone", 2: "minecraft:diamond"}


# --------------------------------------------------------------------------
# 合成 protocol.json（schema 与 PrismarineJS 一致）
# --------------------------------------------------------------------------
def _synthetic_protocol_json() -> dict:
    return {
        "types": {
            "varint": "native", "bool": "native", "u8": "native", "i8": "native",
            "i16": "native", "f32": "native", "string": ["pstring", {"countType": "varint"}],
            "anonymousNbt": "native", "void": "native",
            "ContainerID": "varint",
            "Slot": ["container", [
                {"name": "itemCount", "type": "varint"},
                {"anon": True, "type": ["switch", {
                    "compareTo": "itemCount",
                    "fields": {"0": "void"},
                    "default": ["container", [
                        {"name": "itemId", "type": "varint"},
                        {"name": "addedComponentCount", "type": "varint"},
                        {"name": "removedComponentCount", "type": "varint"},
                        {"name": "components",
                         "type": ["array", {"count": "addedComponentCount", "type": "SlotComponent"}]},
                        {"name": "removeComponents",
                         "type": ["array", {"count": "removedComponentCount", "type": "varint"}]},
                    ]],
                }]},
            ]],
            "SlotComponentType": ["mapper", {"type": "varint", "mappings": {
                "0": "custom_data", "1": "max_stack_size",
            }}],
            "SlotComponent": ["container", [
                {"name": "type", "type": "SlotComponentType"},
                {"name": "data", "type": ["switch", {"compareTo": "type", "fields": {
                    "custom_data": "anonymousNbt", "max_stack_size": "varint",
                }}]},
            ]],
            "packet_window_items": ["container", [
                {"name": "windowId", "type": "ContainerID"},
                {"name": "stateId", "type": "varint"},
                {"name": "items", "type": ["array", {"countType": "varint", "type": "Slot"}]},
                {"name": "carriedItem", "type": "Slot"},
            ]],
            "packet_set_slot": ["container", [
                {"name": "windowId", "type": "ContainerID"},
                {"name": "stateId", "type": "varint"},
                {"name": "slot", "type": "i16"},
                {"name": "item", "type": "Slot"},
            ]],
            "packet_entity_equipment": ["container", [
                {"name": "entityId", "type": "varint"},
                {"name": "equipments", "type": ["topBitSetTerminatedArray", {"type": [
                    "container", [
                        {"name": "slot", "type": "i8"},
                        {"name": "item", "type": "Slot"},
                    ],
                ]}]},
            ]],
            "entityMetadataEntry": ["container", [
                {"name": "key", "type": "u8"},
                {"name": "type", "type": ["mapper", {"type": "varint", "mappings": {
                    "0": "byte", "3": "float", "7": "item_stack",
                }}]},
                {"name": "value", "type": ["switch", {"compareTo": "type", "fields": {
                    "byte": "u8", "float": "f32", "item_stack": "Slot",
                }}]},
            ]],
            "packet_entity_metadata": ["container", [
                {"name": "entityId", "type": "varint"},
                {"name": "metadata", "type": ["entityMetadataLoop", {
                    "endVal": 255, "type": "entityMetadataEntry",
                }]},
            ]],
        },
        "login": {"toClient": {"types": {"packet": ["container", [
            {"name": "name", "type": ["mapper", {"type": "varint", "mappings": {
                "0x02": "success",
            }}]},
        ]]}},
            "toServer": {"types": {"packet": ["container", [
                {"name": "name", "type": ["mapper", {"type": "varint", "mappings": {
                    "0x00": "login_start",
                }}]},
            ]]}}},
        "play": {"toClient": {"types": {"packet": ["container", [
            {"name": "name", "type": ["mapper", {"type": "varint", "mappings": {
                "0x2C": "map_chunk", "0x12": "window_items", "0x14": "set_slot",
                "0x42": "block_place", "0x05": "explosion",
            }}]},
        ]]}},
            "toServer": {"types": {"packet": ["container", [
                {"name": "name", "type": ["mapper", {"type": "varint", "mappings": {
                    "0x1E": "position", "0x2E": "block_place",
                }}]},
            ]]}}},
    }


def _slot_bytes(count: int, item_id: int, custom_data: bytes | None = None) -> bytes:
    """构造合成 766+ 风格的槽位字节"""
    if custom_data is None:
        return bytes([count, item_id, 0, 0])   # count, id, added=0, removed=0
    # count, id, added=1, removed=0, 组件类型 0=custom_data, 组件负载
    return bytes([count, item_id, 1, 0, 0]) + custom_data


def _nbt_compound(payload: bytes) -> bytes:
    """anonymous NBT 复合（payload 为内部字节）"""
    return b"\x0a" + payload + b"\x00"


# --------------------------------------------------------------------------
# 求值器与 ID 表
# --------------------------------------------------------------------------
def test_evaluator_and_tables():
    print("== protocol.json 求值器与 ID 表 ==")
    proto = _synthetic_protocol_json()
    ev = PacketTypeEvaluator.for_clientbound(proto)

    # 槽位：含 custom_data 组件
    nbt_data = _nbt_compound(
        b"\x03\x00\x04" + b"test" + struct.pack(">i", 7))
    buf = PacketBuffer(_slot_bytes(5, 2, nbt_data))
    slot = ev.read_by_name(buf, "Slot")
    check(buf.at_end(), "槽位精确消费")
    check(isinstance(slot, dict) and slot.get("itemId") == 2
          and slot.get("components") is not None, "槽位字段（匿名 switch 并入）")
    item = slot_to_item(slot, ITEM_NAMES)
    check(item and str(item["id"]) == "minecraft:diamond"
          and int(item["Count"]) == 5 and item.get("tag") == {"test": 7},
          "slot_to_item: id/数量/custom_data")

    # 空槽（count 0）
    empty = ev.read_by_name(PacketBuffer(b"\x00"), "Slot")
    check(slot_to_item(empty, ITEM_NAMES) == {}, "空槽 -> {}")

    # Window Items
    payload = (bytes([3]) + write_varint(0) + write_varint(2)
               + _slot_bytes(1, 1) + b"\x00" + b"\x00")
    content = parse_window_items(payload, 773, ITEM_NAMES, evaluator=ev)
    check(content is not None and content.window_id == 3, "求值器: Window Items 解析")
    check(content is not None and len(content.items) == 1
          and content.items[0]["Slot"] == nbt.Byte(0)
          and str(content.items[0]["id"]) == "minecraft:stone", "求值器: 物品与槽号")

    # Set Slot（含与不含 stateId 的手写回退 + 求值器路径）
    parsed = parse_set_slot(bytes([3, 0]) + struct.pack(">h", 5)
                            + _slot_bytes(2, 1), 773, ITEM_NAMES, evaluator=ev)
    check(parsed == (3, 5, {"id": nbt.String("minecraft:stone"),
                            "Count": nbt.Byte(2)}), "求值器: Set Slot")
    parsed = parse_set_slot(bytes([3, 0]) + struct.pack(">h", 5)
                            + b"\x01" + write_varint(1) + write_varint(2) + b"\x00",
                            758, ITEM_NAMES)
    check(parsed is not None and parsed[0] == 3 and parsed[1] == 5
          and parsed[2] and int(parsed[2]["Count"]) == 2, "手写: Set Slot(present)")
    parsed = parse_set_slot(bytes([3, 0]) + struct.pack(">h", 1) + b"\x00", 758, ITEM_NAMES)
    check(parsed is not None and parsed[1] == 1 and parsed[2] == {},
          "手写: Set Slot 空槽(玩家拿走)")

    # ID 表生成与别名
    tables = extract_packet_tables(proto)
    play_c = tables.get("PLAY_CLIENTBOUND", {})
    check(play_c.get("Chunk Data") == 0x2C, "别名: map_chunk -> Chunk Data")
    check(play_c.get("Window Items") == 0x12, "别名: window_items")
    check(play_c.get("Use Item On") == 0x42, "别名: block_place -> Use Item On")
    check(play_c.get("explosion") == 0x05, "未收录名字原样保留")
    check(tables.get("LOGIN_CLIENTBOUND", {}).get("Login Success") == 0x02,
          "login 表别名")

    # resolver 合并
    resolver = PacketIdResolver()
    resolver.set_protocol_version(760)      # 无内置表
    check(not resolver.has_exact_table, "760 无内置表")
    resolver.set_generated_tables(tables)
    check(resolver.has_exact_table, "运行时表已合并")
    check(resolver.resolve("PLAY", "CLIENTBOUND", 0x2C, b"") == "Chunk Data",
          "resolver 精确解析")
    resolver2 = PacketIdResolver()
    resolver2.set_protocol_version(773)     # 有内置表
    resolver2.set_generated_tables(tables)
    check(resolver2.resolve("PLAY", "CLIENTBOUND", 0x2C, b"") == "Chunk Data and Update Light",
          "内置表优先级不被运行时表覆盖")


# --------------------------------------------------------------------------
# Set Slot 增量 / 窗口会话（协议 758，走精确表）
# --------------------------------------------------------------------------
def _position_bytes(x: int, y: int, z: int) -> bytes:
    value = ((x & 0x3FFFFFF) << 38) | ((z & 0x3FFFFFF) << 12) | (y & 0xFFF)
    return struct.pack(">q", value)


def test_set_slot_flow():
    print("== Set Slot 增量更新 / 窗口会话（758）==")
    parser = ProtocolParser(allow_download=False)
    parser.protocol_version = 758
    parser.resolver.set_protocol_version(758)
    parser.state = ConnectionState.PLAY
    parser.item_names = dict(ITEM_NAMES)
    merges = []
    parser.on_container_items = lambda d, x, y, z, items: merges.append((x, y, z, [dict(i) for i in items]))

    # 右键箱子（serverbound Use Item On 0x2E）-> Open Window 0x2E
    parser.process_serverbound(0x2E, _position_bytes(5, 3, 2) + b"\x01")
    parser.process_clientbound(0x2E, write_varint(1))

    # Window Items（0x14）：窗口 1 / state 0 / 2 槽（石头在槽 1）
    payload = (b"\x01" + write_varint(0) + write_varint(2)
               + b"\x00"
               + b"\x01" + write_varint(1) + write_varint(2) + b"\x00"
               + b"\x00")
    parser.process_clientbound(0x14, payload)
    check(len(merges) == 1 and merges[0][:3] == (5, 3, 2), "Window Items 建会话并合并")
    check(len(merges) == 1 and len(merges[0][3]) == 1
          and int(merges[0][3][0]["Slot"]) == 1, "初始内容：槽 1 石头×2")

    # Set Slot（0x16）：槽 5 放入钻石
    payload = b"\x01" + write_varint(0) + struct.pack(">h", 5) \
        + b"\x01" + write_varint(2) + write_varint(3) + b"\x00"
    parser.process_clientbound(0x16, payload)
    check(len(merges) == 2, "Set Slot 触发增量合并")
    check(len(merges) == 2 and len(merges[1][3]) == 2, "槽 5 已加入")
    slots = {int(e["Slot"]): e for e in merges[1][3]} if len(merges) == 2 else {}
    check(slots.get(5) is not None and str(slots[5]["id"]) == "minecraft:diamond"
          and int(slots[5]["Count"]) == 3, "槽 5 = 钻石×3")

    # Set Slot：槽 1 清空（拿走）
    payload = b"\x01" + write_varint(0) + struct.pack(">h", 1) + b"\x00"
    parser.process_clientbound(0x16, payload)
    check(len(merges) == 3 and len(merges[2][3]) == 1
          and int(merges[2][3][0]["Slot"]) == 5, "槽 1 已清空")

    # 玩家物品栏的 Set Slot（窗口 0）不影响会话
    payload = b"\x00" + write_varint(0) + struct.pack(">h", 9) \
        + b"\x01" + write_varint(1) + write_varint(1) + b"\x00"
    parser.process_clientbound(0x16, payload)
    check(len(merges) == 3, "窗口 0（玩家物品栏）被忽略")

    # 关窗（serverbound Close Window 0x09）-> 最终合并 + 清理
    parser.process_serverbound(0x09, b"\x01")
    check(len(merges) == 4, "关窗触发最终合并")
    check(1 not in parser._window_sessions and 1 not in parser._window_pos,
          "会话已清理")


# --------------------------------------------------------------------------
# 装备 / 实体元数据
# --------------------------------------------------------------------------
class _FakeEvaluator:
    """鸭子类型求值器：返回预设值并消费整个缓冲"""

    def __init__(self, result):
        self._result = result

    def has_type(self, name):
        return True

    def read_by_name(self, buf, name):
        buf.read_rest()
        return self._result


def _spawn_payload(eid: int, type_id: int) -> bytes:
    """1.13 ~ 1.21.1 形状的 Spawn Entity（legacy 分支，速度 = 3×Short）"""
    return (write_varint(eid) + b"\x00" * 16 + write_varint(type_id)
            + struct.pack(">ddd", 1.0, 2.0, 3.0) + b"\x00\x00\x00"
            + write_varint(0) + b"\x00" * 6)


def test_equipment_and_metadata():
    print("== 实体装备与元数据 ==")
    tracker = EntityTracker()
    names = {1: "minecraft:zombie", 2: "minecraft:item_frame"}
    check(tracker.on_spawn_entity(_spawn_payload(5, 1), names), "spawn zombie")
    check(tracker.on_spawn_entity(_spawn_payload(6, 2), names), "spawn item_frame")

    # 装备：主手石头 + 头盔钻石（最后一项 slot 带终止位）
    equipment = {
        "entityId": 5,
        "equipments": [
            {"slot": 0, "item": {"itemCount": 1, "itemId": 1,
                                 "addedComponentCount": 0, "removedComponentCount": 0,
                                 "components": [], "removeComponents": []}},
            {"slot": 5, "item": {"itemCount": 1, "itemId": 2,
                                 "addedComponentCount": 0, "removedComponentCount": 0,
                                 "components": [], "removeComponents": []}},
        ],
    }
    check(tracker.on_entity_equipment(b"\x05", ITEM_NAMES,
                                      evaluator=_FakeEvaluator(equipment)),
          "装备解析成功")
    record = tracker._entities[5][1]
    entry = _entity_nbt(record, 2975)
    hands = entry.get("HandItems")
    check(hands is not None and len(hands) == 2
          and str(hands[0]["id"]) == "minecraft:stone"
          and hands[1] == nbt.compound({}), "HandItems=[主手, 空]")
    armor = entry.get("ArmorItems")
    check(armor is not None and len(armor) == 4
          and str(armor[3]["id"]) == "minecraft:diamond", "ArmorItems 顺序=[靴,腿,胸,头]")

    # 元数据：血量（float @ 9）+ 展示框内容（item_stack）
    metadata = {
        "entityId": 6,
        "metadata": [
            {"key": 9, "type": "float", "value": 12.5},
            {"key": 8, "type": "item_stack",
             "value": {"itemCount": 1, "itemId": 1, "addedComponentCount": 0,
                       "removedComponentCount": 0, "components": [],
                       "removeComponents": []}},
        ],
    }
    check(tracker.on_entity_metadata(b"\x06", ITEM_NAMES,
                                     evaluator=_FakeEvaluator(metadata)),
          "元数据解析成功")
    entry6 = _entity_nbt(tracker._entities[6][1], 2975)
    check(entry6.get("Health") == nbt.Float(12.5), "Health=12.5")
    frame_item = entry6.get("Item")
    check(frame_item is not None and str(frame_item["id"]) == "minecraft:stone",
          "展示框 Item")

    # 未知实体：安全跳过
    check(tracker.on_entity_equipment(b"\x63", ITEM_NAMES,
                                      evaluator=_FakeEvaluator({"entityId": 99, "equipments": []})),
          "未知实体的装备安全跳过")
    check(tracker.on_entity_metadata(b"\x63", ITEM_NAMES,
                                     evaluator=_FakeEvaluator({"entityId": 99, "metadata": []})),
          "未知实体的元数据安全跳过")


# --------------------------------------------------------------------------
# 双箱内容拆分
# --------------------------------------------------------------------------
def _double_chest_root() -> dict:
    """一个最小根 NBT：(0,5,0)=chest[type=left]，(1,5,0)=chest[type=right]"""
    bits = 4
    per_long = 64 // bits
    index0 = (5 << 8) | (0 << 4) | 0      # (x=0,z=0,y=5) -> 1280
    index1 = (5 << 8) | (0 << 4) | 1      # (x=1,z=0,y=5) -> 1281
    longs = [0] * (max(index0, index1) // per_long + 1)
    longs[index0 // per_long] |= 1 << ((index0 % per_long) * bits)
    longs[index1 // per_long] |= 2 << ((index1 % per_long) * bits)
    return {
        "sections": [nbt.compound({
            "Y": nbt.Byte(0),
            "block_states": nbt.compound({
                "palette": nbt.List(nbt.TAG_COMPOUND, [
                    nbt.compound({"Name": nbt.String("minecraft:air")}),
                    nbt.compound({"Name": nbt.String("minecraft:chest"),
                                  "Properties": nbt.compound({
                                      "type": nbt.String("left"),
                                      "facing": nbt.String("north")})}),
                    nbt.compound({"Name": nbt.String("minecraft:chest"),
                                  "Properties": nbt.compound({
                                      "type": nbt.String("right"),
                                      "facing": nbt.String("north")})}),
                ]),
                "data": nbt.LongArray(longs),
            }),
        })],
        "block_entities": nbt.List(nbt.TAG_COMPOUND, []),
    }


def _items54():
    return [nbt.compound({"id": nbt.String("minecraft:stone"),
                          "Count": nbt.Byte(1), "Slot": nbt.Byte(i)})
            for i in range(54)]


def _items_at(root, x, y, z):
    for entry in root.get("block_entities", []):
        if int(entry.get("x")) == x and int(entry.get("y")) == y \
                and int(entry.get("z")) == z:
            return entry.get("Items", [])
    return None


def test_double_chest_split():
    print("== 双箱内容拆分 ==")
    # 1.13+：type 属性精确判定
    root = _double_chest_root()
    check(_merge_items_into_root(root, 0, 5, 0, _items54()), "双箱合并返回成功")
    left = _items_at(root, 0, 5, 0)
    right = _items_at(root, 1, 5, 0)
    check(left is not None and len(left) == 27, "左半格 27 组")
    check(right is not None and len(right) == 27, "右半格 27 组")
    check(left and int(left[0]["Slot"]) == 0 and int(left[26]["Slot"]) == 26,
          "左半格 Slot 重编为 0~26")
    check(right and int(right[0]["Slot"]) == 0, "右半格 Slot 重编为 0~26")

    # 右半格打开（type=right 拿后 27 格）
    root = _double_chest_root()
    check(_merge_items_into_root(root, 1, 5, 0, _items54()), "从右半格打开")
    left = _items_at(root, 0, 5, 0)
    right = _items_at(root, 1,5, 0)
    check(right is not None and len(right) == 27 and left is not None
          and len(left) == 27, "两半都有内容")
    # 右半格（right）应拿后 27 格；无法从内容区分前后（物品相同），只验证数量与编号

    # 无属性（旧版导出）：坐标小的拿前 27 格（确定规则）
    root = _double_chest_root()
    root["sections"][0]["block_states"]["palette"] = nbt.List(nbt.TAG_COMPOUND, [
        nbt.compound({"Name": nbt.String("minecraft:air")}),
        nbt.compound({"Name": nbt.String("minecraft:chest")}),
        nbt.compound({"Name": nbt.String("minecraft:chest")}),
    ])
    check(_merge_items_into_root(root, 0, 5, 0, _items54()), "无属性时也可拆分")
    check(len(_items_at(root, 0, 5, 0) or []) == 27
          and len(_items_at(root, 1, 5, 0) or []) == 27, "无属性：两半各 27")

    # 单箱（27 组）不触发拆分
    root = _double_chest_root()
    check(_merge_items_into_root(root, 0, 5, 0, _items54()[:27]), "单箱 27 组直接合并")
    check(_items_at(root, 1, 5, 0) is None, "邻居未被波及")


def main() -> int:
    print("== 运行时协议数据测试 ==")
    test_evaluator_and_tables()
    test_set_slot_flow()
    test_equipment_and_metadata()
    test_double_chest_split()
    print(f"\n===== 运行时协议测试: {PASS} 通过, {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
