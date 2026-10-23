"""验证 ProtocolParser 在多版本下的区块分派（不联网）"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.nbt as nbt
from core.datatypes import write_string, write_varint
from core.protocol import ConnectionState, ProtocolParser, chunk_format_family

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


def make_handshake(protocol, host="example.com", port=25565, state=2):
    return write_varint(protocol) + write_string(host) + struct.pack(">H", port) + write_varint(state)


def test_family_and_geometry():
    print("\n== ProtocolParser 版本家族与几何 ==")
    for proto, expected_family, expected_sections, expected_miny in [
        (47, "legacy", 16, 0),
        (754, "flattened", 16, 0),
        (758, "modern", 24, -4),
        (770, "modern", 24, -4),
    ]:
        parser = ProtocolParser(debug=False, world_dir=".", allow_download=False)
        parser._parse_handshake(make_handshake(proto))
        check(f"[{proto}] 家族 = {expected_family}",
              parser.chunk_family == expected_family, f"got {parser.chunk_family}")
        check(f"[{proto}] 节数 = {expected_sections}",
              parser.section_count == expected_sections, f"got {parser.section_count}")
        check(f"[{proto}] min_y = {expected_miny}",
              parser.min_section_y == expected_miny, f"got {parser.min_section_y}")
        check(f"[{proto}] 进入 LOGIN 状态", parser.state == ConnectionState.LOGIN)


def test_legacy_join_game():
    print("\n== 1.8 Join Game 维度解析 ==")
    parser = ProtocolParser(debug=False, world_dir=".", allow_download=False)
    parser._parse_handshake(make_handshake(47))
    # Join Game: i32 entityId, u8 gameMode, i8 dimension, u8 difficulty, u8 maxPlayers,
    #            string levelType, bool reducedDebugInfo
    payload = struct.pack(">i", 1) + bytes([0]) + struct.pack(">b", -1) + bytes([1, 20])
    payload += write_string("default") + bytes([0])
    parser._on_login(payload)
    check("维度 = 下界", parser.dimension_name == "minecraft:the_nether", f"got {parser.dimension_name}")
    check("维度 ID = -1", parser.dimension == -1, f"got {parser.dimension}")
    check("节数 = 16", parser.section_count == 16)


def test_legacy_respawn():
    print("\n== 1.8 Respawn 维度解析 ==")
    parser = ProtocolParser(debug=False, world_dir=".", allow_download=False)
    parser._parse_handshake(make_handshake(47))
    # Respawn: i32 dimension, u8 difficulty, u8 gamemode, string levelType
    payload = struct.pack(">i", 1) + bytes([1, 0]) + write_string("default")
    parser._on_respawn(payload)
    check("维度 = 末地", parser.dimension_name == "minecraft:the_end", f"got {parser.dimension_name}")


def main():
    print("== ProtocolParser 多版本分派测试 ==")
    test_family_and_geometry()
    test_legacy_join_game()
    test_legacy_respawn()
    print(f"\n===== 分派测试: {PASS} 通过, {FAIL} 失败 =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
