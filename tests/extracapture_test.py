"""
附加捕获测试：容器内容 / 地图画 / 实体快照 / 玩家位置（出生点）

覆盖:
  * Window Items 三代槽位格式解析（1.8 / 1.13-1.16 / 1.17-1.20.4）+ 版本未知时自动试探
  * 容器内容两条合并路径：区块落盘前暂存合并、落盘后读回打补丁
  * 方块实体补建（从区块 NBT 反查方块名 -> 确认是容器才建）
  * Map Data 增量累积 -> data/map_<id>.dat（颜色索引 / 不覆盖已有）
  * 实体 spawn/teleport/destroy 状态机 -> entities/r.*.mca（NBT 结构逐项校验）
  * 玩家位置 -> level.dat 出生点
  * --doctor 自检可运行且退出码 0
"""

import asyncio
import gzip
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import nbt
from core.block_registry import load_registry
from core.chunk_extractor import ChunkExtractor
from core.chunk_format import ChunkSection, ParsedChunk, build_chunk_nbt, compress_chunk_nbt
from core.containers import container_entity_id, parse_window_items
from core.datatypes import write_varint
from core.entities import EntityTracker
from core.level_dat import ensure_world_metadata
from core.maps import MapCollector
from core.protocol import ProtocolParser

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


ITEM_NAMES = {1: "minecraft:stone", 2: "minecraft:diamond", 300: "minecraft:shulker_box"}


# ---------------------------------------------------------------- 容器解析
def test_window_items_parsing():
    # 1.17 ~ 1.20.4（present 格式，758）
    payload = (b"\x03" + write_varint(7) + write_varint(3)          # 窗口 3 / state 7 / 3 槽
               + b"\x00"                                             # 槽 0: 空
               + b"\x01" + write_varint(1) + write_varint(2) + b"\x00"   # 槽 1: 石头×2
               + b"\x00")                                            # 槽 2: 空
    content = parse_window_items(payload, 758, ITEM_NAMES)
    check(content is not None and content.window_id == 3, "present: 解析成功")
    check(content and len(content.items) == 1, "present: 仅 1 组物品")
    check(content and content.items[0]["Slot"] == nbt.Byte(1)
          and content.items[0]["id"] == nbt.String("minecraft:stone"),
          "present: 槽位号与物品名正确")

    # 1.13 ~ 1.16.5（classic 格式，754，无 state id）
    payload = (b"\x03" + write_varint(2)
               + write_varint(-1)                                    # 槽 0: 空
               + write_varint(2) + b"\x40" + b"\x00")                 # 槽 1: 钻石×64
    content = parse_window_items(payload, 754, ITEM_NAMES)
    check(content is not None and len(content.items) == 1
          and content.items[0]["id"] == nbt.String("minecraft:diamond")
          and content.items[0]["Count"] == nbt.Byte(64), "classic: 解析正确")

    # 1.8（legacy 格式：Short itemId / UByte count / Short damage）
    payload = (b"\x03" + struct.pack(">h", 2)
               + struct.pack(">h", -1)
               + struct.pack(">h", 1) + bytes([5]) + struct.pack(">h", 0) + b"\x00")
    content = parse_window_items(payload, 47, ITEM_NAMES)
    check(content is not None and len(content.items) == 1
          and content.items[0]["Count"] == nbt.Byte(5), "legacy: 解析正确")

    # 版本未知（结构探测场景）：三种格式自动试探
    content = parse_window_items(payload, None, ITEM_NAMES)
    check(content is not None, "未知版本: 自动试探成功")

    # 1.20.5+ 明确不支持
    check(parse_window_items(payload, 773, ITEM_NAMES) is None,
          "1.20.5+ 组件格式: 返回 None（已知限制）")

    # 容器类型识别
    check(container_entity_id("minecraft:chest") == "minecraft:chest", "箱子类型识别")
    check(container_entity_id("minecraft:red_shulker_box") == "minecraft:shulker_box",
          "染色潜影盒归一化")
    check(container_entity_id("minecraft:stone") is None, "非容器返回 None")


# ---------------------------------------------------------------- 合并路径
class _Chunk:
    def __init__(self, x, z, data):
        self.x, self.z, self.data = x, z, data
        self.block_entities = []


def _chest_chunk(registry):
    """一个 1 节全箱子方块的最小区块（min_section_y=0，16×16×16 全 chest）"""
    chest_state = registry.state_id_for_name("minecraft:chest")
    section = ChunkSection(
        block_count=4096, fluid_count=0,
        block_states=[0] * 4096, biomes=[0] * 64,
        block_palette=[chest_state], block_bits=0,
    )
    return ParsedChunk(x=0, z=0, heightmaps={}, sections=[section],
                       min_section_y=0, section_count=1)


def _items():
    return [nbt.compound({"id": nbt.String("minecraft:stone"),
                          "Count": nbt.Byte(2), "Slot": nbt.Byte(1)})]


async def _read_root(extractor, cx, cz, dim="minecraft:overworld", subdir="region"):
    raw = await extractor.anvil_writer.read_chunk(cx, cz, dim, subdir)
    if raw is None:
        return None
    root, _end = nbt.parse_named_tag(raw, 0)
    return root


def _find_be(root, x, y, z):
    for entry in root.get("block_entities", []):
        if int(entry.get("x")) == x and int(entry.get("y")) == y and int(entry.get("z")) == z:
            return entry
    return None


async def test_container_merge_pending():
    with tempfile.TemporaryDirectory(prefix="ctn_p_") as tmp:
        registry = load_registry(777, tmp, allow_download=False)
        check(registry.loaded, "方块注册表加载（777 本地缓存）")
        extractor = ChunkExtractor(tmp)
        raw = compress_chunk_nbt(build_chunk_nbt(_chest_chunk(registry), registry,
                                                 data_version=5023))
        # 先开箱（区块未保存 -> 暂存），区块再落盘 -> 合并
        ok = await extractor.update_container_items("minecraft:overworld", 5, 3, 2, _items())
        check(ok, "暂存路径返回成功")
        check(await extractor.save_chunk(_Chunk(0, 0, raw), "minecraft:overworld"),
              "区块落盘")
        root = await _read_root(extractor, 0, 0)
        entry = _find_be(root, 5, 3, 2) if root else None
        check(entry is not None, "方块实体已按坐标补建")
        if entry:
            check(str(entry.get("id")) == "minecraft:chest", "补建的实体类型是箱子")
            items = entry.get("Items", [])
            check(len(items) == 1 and str(items[0]["id"]) == "minecraft:stone"
                  and int(items[0]["Count"]) == 2, "物品内容写入正确")
        check(extractor.containers_captured == 1, "容器计数恰好一次")


async def test_container_patch_after_save():
    with tempfile.TemporaryDirectory(prefix="ctn_s_") as tmp:
        registry = load_registry(777, tmp, allow_download=False)
        extractor = ChunkExtractor(tmp)
        raw = compress_chunk_nbt(build_chunk_nbt(_chest_chunk(registry), registry,
                                                 data_version=5023))
        check(await extractor.save_chunk(_Chunk(0, 0, raw), "minecraft:overworld"),
              "区块先落盘")
        # 之后才开箱 -> 读回打补丁重写
        check(await extractor.update_container_items("minecraft:overworld", 5, 3, 2,
                                                     _items()), "补丁路径返回成功")
        root = await _read_root(extractor, 0, 0)
        entry = _find_be(root, 5, 3, 2) if root else None
        check(entry is not None and len(entry.get("Items", [])) == 1,
              "已保存区块被正确打补丁")


# ---------------------------------------------------------------- 地图画
def test_maps():
    collector = MapCollector()
    payload = (write_varint(7) + b"\x00" + b"\x01"      # mapId 7 / scale 0 / locked
               + b"\x00"                                 # 无图标
               + b"\x02" + b"\x01" + b"\x01" + b"\x02"   # columns 2 / rows 1 / x 1 / y 2
               + write_varint(2) + b"\xab\xcd")          # 2 字节颜色
    check(collector.on_map_data(payload), "Map Data 解析应用成功")
    check(not collector.on_map_data(b"\xff" * 20), "畸形 Map Data 被拒绝")

    with tempfile.TemporaryDirectory(prefix="map_") as tmp:
        written = collector.write_maps(tmp, 4325, "minecraft:overworld")
        check(written == 1, "写出 1 张地图")
        path = Path(tmp) / "data" / "map_7.dat"
        check(path.exists(), "data/map_7.dat 已生成")
        with gzip.open(path, "rb") as fh:
            root, _ = nbt.parse_named_tag(fh.read(), 0)
        data = root["data"]
        colors = data["colors"]
        check(len(colors) == 16384, f"色板 16384 字节 (got {len(colors)})")
        check(colors[2 * 128 + 1] == 0xAB and colors[2 * 128 + 2] == 0xCD,
              "颜色写入正确索引（y*128+x，行优先）")
        check(str(data["dimension"]) == "minecraft:overworld", "维度为字符串")
        check(int(root["DataVersion"]) == 4325, "DataVersion 写入根标签")
        check(collector.write_maps(tmp, 4325) == 0, "已存在的地图不覆盖")


# ---------------------------------------------------------------- 实体
def test_entities():
    tracker = EntityTracker()
    names = {50: "minecraft:zombie", 9: "minecraft:cow"}
    uuid_a = bytes(range(16))

    def spawn(eid, etype, x, y, z, uid):
        return (write_varint(eid) + uid + write_varint(etype)
                + struct.pack(">ddd", x, y, z)
                + b"\x00\x00\x00" + write_varint(0)
                + struct.pack(">hhh", 0, 0, 0))

    check(tracker.on_spawn_entity(spawn(100, 50, 10.5, 64, -20.25, uuid_a), names),
          "Spawn Entity 解析成功")
    check(tracker.on_spawn_entity(spawn(101, 9, 200, 70, 300, uuid_a), names),
          "第二只实体")
    # 1.21.2+ 精简格式（无 data / 速度字段）
    short_spawn = (write_varint(300) + uuid_a + write_varint(50)
                   + struct.pack(">ddd", 5.0, 65.0, 5.0) + b"\x00\x00")
    check(tracker.on_spawn_entity(short_spawn, names), "1.21.2+ 精简 Spawn 格式")
    check(tracker.count == 3, "快照共 3 个实体")
    # 旧格式传送（末尾带 onGround）
    teleport = (write_varint(100) + struct.pack(">ddd", 12.0, 64.0, -21.0)
                + b"\x00\x00" + b"\x01")
    check(tracker.on_entity_teleport(teleport, None), "Entity Teleport 更新")
    check(tracker.on_destroy_entities(write_varint(1) + write_varint(101)),
          "Destroy Entities")
    check(tracker.count == 2, "销毁后剩 2 个")
    # Spawn Player
    check(tracker.on_spawn_player(write_varint(200) + uuid_a
                                  + struct.pack(">ddd", 0.5, 65, 0.5) + b"\x00\x00"),
          "Spawn Player 解析")

    async def run():
        with tempfile.TemporaryDirectory(prefix="ent_") as tmp:
            extractor = ChunkExtractor(tmp)
            total = await tracker.flush(tmp, 5023, extractor.save_entity_chunk)
            check(total == 3, f"写入 3 个实体 (got {total})")
            # 区块 (0,-2) 属于区域 r.0.-1（区域坐标 = 区块>>5）；玩家在 (0,0) -> r.0.0
            region = Path(tmp) / "entities" / "r.0.-1.mca"
            check(region.exists(), "entities/r.0.-1.mca 已生成")
            check((Path(tmp) / "entities" / "r.0.0.mca").exists(),
                  "玩家实体所在 r.0.0.mca 已生成")
            root = await _read_root(extractor, 0, -2, subdir="entities")
            check(root is not None, "实体区块可读回")
            if root:
                check(int(root["DataVersion"]) == 5023, "实体区块 DataVersion")
                check(list(root["Position"]) == [0, -2], "Position IntArray=[0,-2]")
                entities = list(root["Entities"])
                check(len(entities) == 1, "区块内 1 个实体")
                zombie = entities[0]
                check(str(zombie["id"]) == "minecraft:zombie", "实体类型")
                check(list(zombie["Pos"]) == [12.0, 64.0, -21.0], "传送后坐标")
                check(len(list(zombie["UUID"])) == 4, "UUID 为 4×Int")
                check("Motion" in zombie and "Rotation" in zombie, "必填字段齐全")

    asyncio.run(run())


# ---------------------------------------------------------------- 位置/出生点
def test_position_and_spawn():
    parser = ProtocolParser(world_dir=tempfile.gettempdir(), allow_download=False)
    parser._on_position(struct.pack(">ddd", 12.5, 70.0, -3.5) + b"\x00" * 10)
    check(parser.player_pos == (12.5, 70.0, -3.5), "玩家位置更新")
    parser._on_position_look(struct.pack(">ddd", 13.0, 71.0, -4.0) + b"\x00" * 10)
    check(parser.player_pos == (13.0, 71.0, -4.0), "别名处理器同样更新位置")

    with tempfile.TemporaryDirectory(prefix="spawn_") as tmp:
        ensure_world_metadata(tmp, "w", 4325, spawn=(13, 71, -4))
        with gzip.open(Path(tmp) / "level.dat", "rb") as fh:
            root, _ = nbt.parse_named_tag(fh.read(), 0)
        data = root["Data"]
        check((int(data["SpawnX"]), int(data["SpawnY"]), int(data["SpawnZ"]))
              == (13, 71, -4), "出生点写入 level.dat")


# ---------------------------------------------------------------- doctor
def test_doctor():
    result = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "doctor.py")],
        capture_output=True, timeout=120,
    )
    check(result.returncode == 0, f"doctor 自检退出码 0 (got {result.returncode})")
    out = result.stdout.decode("utf-8", errors="replace")
    check("环境就绪" in out or "失败" in out, "doctor 有可读输出")


def main() -> int:
    test_window_items_parsing()
    asyncio.run(test_container_merge_pending())
    asyncio.run(test_container_patch_after_save())
    test_maps()
    test_entities()
    test_position_and_spawn()
    test_doctor()
    print(f"\n附加捕获测试: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
