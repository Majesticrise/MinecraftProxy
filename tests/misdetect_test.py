"""误判测试：常见客户端包不应被识别为区块相关包"""

import os
import sys

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import struct

from core.chunk_format import (
    parse_block_entity_data_packet,
    parse_chunk_data_packet,
    parse_unload_chunk_packet,
    parse_update_light_packet,
)
from core.datatypes import write_string, write_varint

CASES = {
    "Set Time(long,long)": struct.pack(">qq", 12000, 12000),
    "Keep Alive(long)": struct.pack(">q", 123456789),
    "Sync Position(3d,2f,byte,varint)": (
        struct.pack(">ddd", 1.0, 64.0, 2.0)
        + struct.pack(">ff", 0.0, 0.0)
        + bytes([0])
        + write_varint(1)
    ),
    "Set Center Chunk(2 varint)": write_varint(10) + write_varint(-5),
    "Chunk Batch Start(empty)": b"",
    "Chunk Batch Finished(float)": struct.pack(">f", 0.5),
    "System Chat(string)": write_string('{"text":"hello"}'),
    "Disconnect(string)": write_string('{"text":"bye"}'),
    "Respawn-ish(4 ints)": struct.pack(">iiii", 1, 2, 3, 4),
    "Boss Bar(16 byte uuid + varint)": bytes(16) + write_varint(0),
    "Player Info(16 uuid + bool)": bytes(16) + bytes([1]),
    "Game Event(ubyte + float)": bytes([13]) + struct.pack(">f", 0.0),
    "Set Health(3 floats + varint)": struct.pack(">fff", 20.0, 20.0, 5.0) + write_varint(0),
    "Entity Velocity(varint + 3 shorts)": write_varint(1) + struct.pack(">hhh", 0, 0, 0),
    "Open Screen(varint + window + json)": write_varint(1) + write_varint(0) + write_string('{"text":"Chest"}'),
}

bad = 0
for name, payload in CASES.items():
    hits = []
    if parse_chunk_data_packet(payload) is not None:
        hits.append("chunk")
    if parse_update_light_packet(payload) is not None:
        hits.append("light")
    if parse_block_entity_data_packet(payload) is not None:
        hits.append("be")
    if parse_unload_chunk_packet(payload) is not None:
        hits.append("unload")
    status = ",".join(hits) if hits else "none"
    if hits:
        bad += 1
        print(f"  [MISDETECT] {name:40s} -> {status}")
    else:
        print(f"  [OK]        {name:40s} -> none")

print(f"\n误判: {bad} / {len(CASES)}")
raise SystemExit(1 if bad else 0)
