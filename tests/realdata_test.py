"""
真实数据回归测试：ViaVersion 只翻译包 ID/状态机、不翻译区块负载。

背景
----
实测服务器（协议 773 / 客户端 1.21.10 + ViaFabric）握手报 1.21.9，
包 ID 也是标准 1.21.9 的（map_chunk=0x2C），但**区块负载仍是旧核心格式**：
  * 16 节（世界高度 256）而非 1.18+ 的 24 节
  * section 内没有 fluid_count（1.21.5+ 才有）
  * 数据数组**没有** VarInt 长度前缀（1.21.5+ 才移除）

因此仅凭协议号选布局必然失败。修复方案是
`chunk_format._parse_sections_adaptive`：按「必须精确消费全部字节」
反推真实布局。

固件取自真实服务器抓包（`tests/fixtures/real_1_21_9_via_chunk_*.bin`），
每个文件都是 Chunk Data and Update Light 包的**负载**（不含包 ID）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chunk_format import build_chunk_nbt, parse_chunk_data_packet  # noqa: E402
from core.packet_ids import (  # noqa: E402
    DIR_CLIENTBOUND,
    STATE_PLAY,
    PacketIdResolver,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# (文件名, 期望 x, 期望 z, 期望节数, 期望非空节数, 期望方块实体数)
FIXTURES = [
    ("real_1_21_9_via_chunk_0.bin", 0, 0, 16, 5, 7),
    ("real_1_21_9_via_chunk_1.bin", 1, 0, 16, 4, 2),
    ("real_1_21_9_via_chunk_2.bin", 1, 1, 16, 4, 0),
]

PASS = 0
FAIL = 0


def check(label, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {extra}")


def load(name):
    with open(os.path.join(FIXTURE_DIR, name), "rb") as handle:
        return handle.read()


def test_real_via_chunks():
    print("\n== 真实数据：ViaVersion 未翻译负载（协议 773）==")
    # 按协议声明（24 节 / 有 fluid_count / 有数据长度前缀）解析
    for name, ex, ez, esec, enonempty, ebe in FIXTURES:
        payload = load(name)
        parsed = parse_chunk_data_packet(
            payload, section_count=24, min_section_y=-4,
            has_fluid_count=True, has_data_length=False, has_nbt_heightmaps=False,
        )
        check(f"{name} 解析成功", parsed is not None)
        if parsed is None:
            continue
        check(f"{name} 坐标 ({ex},{ez})", (parsed.x, parsed.z) == (ex, ez),
              f"got ({parsed.x},{parsed.z})")
        check(f"{name} 反推节数={esec}", len(parsed.sections) == esec,
              f"got {len(parsed.sections)}")
        check(f"{name} 世界高度归零（min_section_y=0）", parsed.min_section_y == 0,
              f"got {parsed.min_section_y}")
        nonempty = sum(1 for s in parsed.sections if s.block_count > 0)
        check(f"{name} 非空节={enonempty}", nonempty == enonempty,
              f"got {nonempty}")
        check(f"{name} 方块实体={ebe}", len(parsed.block_entities) == ebe,
              f"got {len(parsed.block_entities)}")
        # 方块实体 NBT 必须真正解析出内容
        for entity in parsed.block_entities:
            if entity.get("nbt") is not None:
                break
        else:
            check(f"{name} 方块实体 NBT 非空", not ebe)


def test_773_exact_table():
    print("\n== 协议 773 精确 ID 表 ==")
    resolver = PacketIdResolver()
    resolver.set_protocol_version(773)
    check("773 有精确表", resolver.has_exact_table)

    expected = {
        ("PLAY", "CLIENTBOUND", "Chunk Data and Update Light"): 0x2C,
        ("PLAY", "CLIENTBOUND", "Update Light"): 0x2F,
        ("PLAY", "CLIENTBOUND", "Login (play)"): 0x30,
        ("PLAY", "CLIENTBOUND", "Unload Chunk"): 0x25,
        ("PLAY", "CLIENTBOUND", "Respawn"): 0x50,
        ("PLAY", "CLIENTBOUND", "Block Entity Data"): 0x06,
        ("PLAY", "CLIENTBOUND", "Start Configuration"): 0x74,
        ("PLAY", "CLIENTBOUND", "Bundle Delimiter"): 0x00,
        ("PLAY", "SERVERBOUND", "Confirm Teleportation"): 0x00,
        ("PLAY", "SERVERBOUND", "Chunk Batch Received"): 0x0A,
        ("PLAY", "SERVERBOUND", "Client Command"): 0x0B,
        ("PLAY", "SERVERBOUND", "Keep Alive"): 0x1B,
        ("PLAY", "SERVERBOUND", "Position"): 0x1D,
        ("PLAY", "SERVERBOUND", "Position Look"): 0x1E,
        ("PLAY", "SERVERBOUND", "Look"): 0x1F,
        ("PLAY", "SERVERBOUND", "Flying"): 0x20,
        ("CONFIGURATION", "CLIENTBOUND", "Registry Data"): 0x07,
        ("CONFIGURATION", "CLIENTBOUND", "Finish Configuration"): 0x03,
        ("CONFIGURATION", "SERVERBOUND", "Acknowledge Configuration"): 0x03,
        ("LOGIN", "CLIENTBOUND", "Set Compression"): 0x03,
        ("LOGIN", "SERVERBOUND", "Login Acknowledged"): 0x03,
    }
    for (state, direction, name), pid in expected.items():
        got = resolver.id_for(state, direction, name)
        check(f"773 {state}/{direction} {name} = 0x{pid:02X}", got == pid,
              f"got {got}")

    # 与 777（26.3）的差异必须被正确区分
    check("773 map_chunk(0x2C) != 777 map_chunk(0x2E)",
          resolver.id_for("PLAY", "CLIENTBOUND", "Chunk Data and Update Light") != 0x2E)


def test_resolve_real_chunk():
    print("\n== 真实区块包经 resolver 识别 ==")
    resolver = PacketIdResolver()
    resolver.set_protocol_version(773)
    payload = load(FIXTURES[0][0])
    name = resolver.resolve(STATE_PLAY, DIR_CLIENTBOUND, 0x2C, payload)
    check("0x2C -> Chunk Data and Update Light",
          name == "Chunk Data and Update Light", f"got {name}")


def test_adaptive_rejects_garbage():
    print("\n== 自适应解析不得误判 ==")
    # 全零负载、随机短包都不应被当成区块
    for label, payload in (
        ("空负载", b""),
        ("2 字节", b"\x00\x01"),
        ("16 字节零", b"\x00" * 16),
        ("64 字节随机", bytes(range(64))),
    ):
        check(f"{label} 不被识别为区块",
              parse_chunk_data_packet(payload, section_count=24) is None)


def test_build_nbt():
    print("\n== 真实区块 -> Anvil NBT ==")
    from core.block_registry import load_registry
    try:
        registry = load_registry(773, ".", allow_download=False)
    except Exception:
        registry = None
    for name, *_ in FIXTURES:
        payload = load(name)
        parsed = parse_chunk_data_packet(payload, section_count=24, min_section_y=-4)
        if parsed is None:
            check(f"{name} 可构建 NBT", False, "解析失败")
            continue
        raw = build_chunk_nbt(parsed, registry, data_version=4556)
        check(f"{name} NBT 非空", len(raw) > 1000, f"got {len(raw)}")
        import core.nbt as nbt
        root, end = nbt.parse_named_tag(raw, 0)
        check(f"{name} NBT 可回读", end == len(raw), f"end={end} len={len(raw)}")
        check(f"{name} 节 Y 从 0 开始",
              root["sections"][0]["Y"] == 0,
              f"got {root['sections'][0]['Y']}")


def main():
    print("=" * 72)
    print("  真实数据回归测试（ViaVersion 未翻译区块负载）")
    print("=" * 72)
    test_773_exact_table()
    test_resolve_real_chunk()
    test_real_via_chunks()
    test_adaptive_rejects_garbage()
    test_build_nbt()
    print("\n" + "=" * 72)
    print(f"  真实数据回归: {PASS} 通过, {FAIL} 失败")
    print("=" * 72)
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
