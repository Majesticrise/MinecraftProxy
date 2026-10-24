"""
实体捕获（第一版：快照）。

状态机：Spawn Entity / Spawn Player -> 记录 {UUID, 类型, 坐标, 朝向}；
        Entity Destroy -> 移除；Entity Teleport -> 刷新坐标。
        （相对移动/元数据/装备暂不跟踪——见 README「已知限制」）

落盘：按 1.17+ 的独立 entities 区域文件（entities/r.x.z.mca），
      区块 NBT = {DataVersion, Position: IntArray[x,z], Entities: List}，
      每个实体必填 id / Pos / Motion / Rotation，UUID 为 4×Int（高位在前）。
      区域文件本体与 region/ 格式相同，直接复用 AnvilWriter。
"""

from __future__ import annotations

import uuid as _uuid  # noqa: F401  （保留给未来按 UUID 去重）
from collections import defaultdict
from typing import Any

from . import nbt
from .datatypes import PacketBuffer
from .logger import setup_logger

logger = setup_logger(__name__)

MAX_ENTITIES = 65536          # 防御上限（真实视野内实体远少于此）
# Entity Teleport 自该版本起末尾无 onGround（1.21.2 精简）
VERSION_TELEPORT_NO_FLAGS_MIN = 768


class EntityRecord:
    __slots__ = ("type_name", "uuid_bytes", "x", "y", "z", "yaw", "pitch")

    def __init__(self, type_name: str, uuid_bytes: bytes,
                 x: float, y: float, z: float, yaw: float, pitch: float):
        self.type_name = type_name
        self.uuid_bytes = uuid_bytes
        self.x = x
        self.y = y
        self.z = z
        self.yaw = yaw
        self.pitch = pitch


class EntityTracker:
    """按实体 ID 维护快照；flush 时按维度与区块分桶写入 entities 区域文件。"""

    def __init__(self) -> None:
        # eid -> (dimension, EntityRecord)
        self._entities: dict[int, tuple[str, EntityRecord]] = {}

    @property
    def count(self) -> int:
        return len(self._entities)

    # ------------------------------------------------------------------ 包解析
    def on_spawn_entity(self, payload: bytes, entity_names: dict[int, str]) -> bool:
        """
        Spawn Entity（1.13+）：VarInt eid, UUID(16), VarInt type,
        Double x/y/z, UByte pitch/yaw/headYaw, VarInt data, Short vx/vy/vz
        （1.19~1.21.1 头部字段顺序略有差异，这里用「宽容读取 + 末尾校验」）
        """
        try:
            buf = PacketBuffer(payload)
            eid = buf.read_varint()
            uid = buf.read(16)
            type_id = buf.read_varint()
            x = buf.read_double()
            y = buf.read_double()
            z = buf.read_double()
            pitch = buf.read_ubyte()
            yaw = buf.read_ubyte()
            buf.read_ubyte()   # 头部朝向
            buf.read_varint()  # 附加数据
            buf.read_short()   # 速度
            buf.read_short()
            buf.read_short()
            if not buf.at_end():
                return False
            type_name = entity_names.get(type_id)
            if not type_name:
                return False  # 没有类型表就不写（写错实体比不写更糟）
            self._remember(eid, type_name, uid, x, y, z, yaw, pitch)
            return True
        except Exception:
            return False

    def on_spawn_player(self, payload: bytes) -> bool:
        """Spawn Player：VarInt eid, UUID(16), Double x/y/z, UByte x2"""
        try:
            buf = PacketBuffer(payload)
            eid = buf.read_varint()
            uid = buf.read(16)
            x = buf.read_double()
            y = buf.read_double()
            z = buf.read_double()
            yaw = buf.read_ubyte()
            pitch = buf.read_ubyte()
            if not buf.at_end():
                return False
            self._remember(eid, "minecraft:player", uid, x, y, z, yaw, pitch)
            return True
        except Exception:
            return False

    def on_destroy_entities(self, payload: bytes) -> bool:
        """Destroy Entities：VarInt count + count×VarInt eid"""
        try:
            buf = PacketBuffer(payload)
            count = buf.read_varint()
            if not (0 < count <= 4096):
                return False
            for _ in range(count):
                self._entities.pop(buf.read_varint(), None)
            return buf.at_end()
        except Exception:
            return False

    def on_entity_teleport(self, payload: bytes,
                           protocol_version: int | None) -> bool:
        """Entity Teleport：VarInt eid, Double x/y/z, UByte yaw/pitch, [Bool onGround]"""
        try:
            buf = PacketBuffer(payload)
            eid = buf.read_varint()
            x = buf.read_double()
            y = buf.read_double()
            z = buf.read_double()
            yaw = buf.read_ubyte()
            pitch = buf.read_ubyte()
            if not buf.at_end():
                # 旧版本末尾还有 onGround / smooth teleport 等字段
                if protocol_version is None or protocol_version < VERSION_TELEPORT_NO_FLAGS_MIN:
                    buf.read_bool()
                    if not buf.at_end():
                        return False
                else:
                    return False
            entry = self._entities.get(eid)
            if entry is not None:
                dimension, record = entry
                record.x, record.y, record.z = x, y, z
                record.yaw, record.pitch = yaw, pitch
                self._entities[eid] = (dimension, record)
            return True
        except Exception:
            return False

    def _remember(self, eid: int, type_name: str, uid: bytes,
                  x: float, y: float, z: float, yaw: float, pitch: float,
                  dimension: str | None = None) -> None:
        if len(self._entities) >= MAX_ENTITIES and eid not in self._entities:
            oldest = next(iter(self._entities))
            self._entities.pop(oldest, None)
        dim = dimension if dimension is not None else _current_dimension[0]
        self._entities[eid] = (dim, EntityRecord(
            type_name, uid, x, y, z, yaw, pitch))

    # ------------------------------------------------------------------ 落盘
    async def flush(self, world_dir: str, data_version: int,
                    save_chunk_fn) -> int:
        """
        把快照写进 entities 区域文件。

        save_chunk_fn(compressed_nbt, chunk_x, chunk_z, dimension) 为协程，
        由调用方提供（ChunkExtractor.save_entity_chunk 的包装）。
        返回写入的实体数。
        """
        if not self._entities:
            return 0
        import zlib

        buckets: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        for _eid, (dimension, rec) in self._entities.items():
            buckets[(dimension, int(rec.x) >> 4, int(rec.z) >> 4)].append(
                _entity_nbt(rec, data_version))

        total = 0
        for (dimension, cx, cz), entities in buckets.items():
            root = {
                "DataVersion": nbt.Int(data_version),
                "Position": nbt.IntArray([cx, cz]),
                "Entities": nbt.List(nbt.TAG_COMPOUND, entities),
            }
            raw = nbt.build_root(root, "")
            try:
                ok = await save_chunk_fn(zlib.compress(raw, 6), cx, cz, dimension)
                if ok:
                    total += len(entities)
                else:
                    logger.error(f"写实体区块 ({cx},{cz}) 失败")
            except Exception as exc:
                logger.error(f"写实体区块 ({cx},{cz}) 失败: {exc}")
        if total:
            logger.info(f"实体快照: 已写入 {total} 个实体 -> entities/*.mca")
        return total


# 当前维度由 protocol 侧在维度切换时更新（简单线程内全局，单会话单线程足够）
_current_dimension: list[str] = ["minecraft:overworld"]


def set_current_dimension(name: str) -> None:
    _current_dimension[0] = name


def _entity_nbt(rec: EntityRecord, data_version: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": nbt.String(rec.type_name),
        "Pos": nbt.List(nbt.TAG_DOUBLE, [nbt.Double(rec.x), nbt.Double(rec.y),
                                         nbt.Double(rec.z)]),
        "Motion": nbt.List(nbt.TAG_DOUBLE, [nbt.Double(0.0), nbt.Double(0.0),
                                            nbt.Double(0.0)]),
        "Rotation": nbt.List(nbt.TAG_FLOAT, [nbt.Float(rec.yaw * 360.0 / 256.0),
                                             nbt.Float(rec.pitch * 360.0 / 256.0)]),
    }
    try:
        # UUID(16B) -> 4×Int（高位在前）
        entry["UUID"] = nbt.IntArray([
            int.from_bytes(rec.uuid_bytes[0:4], "big"),
            int.from_bytes(rec.uuid_bytes[4:8], "big"),
            int.from_bytes(rec.uuid_bytes[8:12], "big"),
            int.from_bytes(rec.uuid_bytes[12:16], "big"),
        ])
    except Exception:
        pass
    return entry
