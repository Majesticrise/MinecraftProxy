"""
区块相关数据包的结构化解析 + Anvil 区块 NBT 构建

覆盖:
  * Chunk Data and Update Light (Play 0x2E)
  * Update Light (Play 0x31)
  * Block Entity Data (Play 0x06)

并把这些网络结构转换为可写入 .mca 的 Anvil 区块 NBT。

参考: https://minecraft.wiki/w/Java_Edition_protocol/Chunk_format
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field
from typing import Any

from . import nbt
from .biome_registry import PLACEHOLDER_BIOME, BiomeRegistry, normalize_biome_name
from .datatypes import PacketBuffer
from .legacy_blocks import legacy_state_id

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
HEIGHTMAP_TYPES = {
    1: "WORLD_SURFACE",
    2: "WORLD_SURFACE_WG",
    3: "OCEAN_FLOOR",
    4: "MOTION_BLOCKING",
    5: "MOTION_BLOCKING_NO_LEAVES",
    6: "OCEAN_FLOOR_WG",
}

# 默认世界高度（主世界 1.18+ = 24 节 / 384 格）
DEFAULT_SECTIONS = 24
DEFAULT_MIN_SECTION_Y = -4

SECTION_BLOCK_COUNT = 4096
SECTION_BIOME_COUNT = 64
LIGHT_ARRAY_SIZE = 2048

# 经典世界高度（1.8 ~ 1.17，以及 ViaVersion 未翻译负载的旧核心）
CLASSIC_SECTION_COUNT = 16

# 上限保护：允许模组的大高度，但防止畸形数据
MAX_SECTIONS = 128


class ChunkFormatError(Exception):
    pass


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class ChunkSection:
    block_count: int
    fluid_count: int
    block_states: list[int]
    biomes: list[int]
    block_palette: list[int] | None = None
    biome_palette: list[int] | None = None
    block_bits: int = 0
    biome_bits: int = 0
    block_single: int | None = None
    biome_single: int | None = None
    sky_light: bytes | None = None
    block_light: bytes | None = None


@dataclass
class ParsedChunk:
    x: int
    z: int
    heightmaps: dict[str, list[int]]
    sections: list[ChunkSection]
    block_entities: list[dict[str, Any]] = field(default_factory=list)
    light: dict[str, Any] | None = None
    data_version: int = 0
    raw_section_bytes: bytes = b""
    min_section_y: int = DEFAULT_MIN_SECTION_Y
    section_count: int = DEFAULT_SECTIONS
    # 1.8：整个区块共享的 256 项旧数字生物群系 ID（z*16+x 索引）；
    # 1.16/1.17：整个区块共享的 1024 项全局生物群系 ID；
    # 1.18+：None（每节自带调色板）
    chunk_biomes: list[int] | None = None


@dataclass
class ParsedLight:
    x: int
    z: int
    sky_light_mask: list[int]
    block_light_mask: list[int]
    empty_sky_light_mask: list[int]
    empty_block_light_mask: list[int]
    sky_light: list[bytes]
    block_light: list[bytes]


# --------------------------------------------------------------------------
# 调色板容器 / 位压缩数组
# --------------------------------------------------------------------------
def _read_paletted_container(
    buf: PacketBuffer,
    entries: int,
    min_bits: int,
    max_bits: int,
    has_data_length: bool = False,
):
    """返回 (values, palette, bits, single_value)

    palette 为 None 表示「直接（direct / 全局调色板）」模式，
    此时不应物化庞大的调色板列表。

    has_data_length：数据数组前是否带 VarInt 长度前缀。
    1.16 ~ 1.21.4 带前缀，1.21.5+ 不带（由 BPE 计算）。
    """
    bits = buf.read_ubyte()
    if bits == 0:
        single = buf.read_varint()
        return [single] * entries, [single], 0, single

    if not (min_bits <= bits <= max_bits):
        raise ChunkFormatError(f"调色板位宽非法: {bits}")

    palette: list[int] | None = None
    if bits <= 8:
        palette = []
        palette_len = buf.read_varint()
        if palette_len < 0 or palette_len > entries:
            raise ChunkFormatError(f"调色板长度非法: {palette_len}")
        for _ in range(palette_len):
            palette.append(buf.read_varint())
    # bits > 8: 直接模式，无调色板（palette 保持 None）

    entries_per_long = 64 // bits
    long_count = math.ceil(entries / entries_per_long)
    if has_data_length:
        declared = buf.read_varint()
        if declared < 0 or declared > long_count * 2:
            raise ChunkFormatError(f"数据数组长度非法: {declared}")
        long_count = declared
    longs = [buf.read_ulong() for _ in range(long_count)]

    mask = (1 << bits) - 1
    values: list[int] = []
    for i in range(entries):
        long_index = i // entries_per_long
        bit_index = (i % entries_per_long) * bits
        values.append((longs[long_index] >> bit_index) & mask)
    return values, palette, bits, None


# --------------------------------------------------------------------------
# 区块数据包解析
# --------------------------------------------------------------------------
def _parse_heightmaps(buf: PacketBuffer) -> dict[str, list[int]]:
    count = buf.read_varint()
    if count < 0 or count > 16:
        raise ChunkFormatError(f"高度图数量非法: {count}")
    heightmaps: dict[str, list[int]] = {}
    for _ in range(count):
        heightmap_type = buf.read_varint()
        long_count = buf.read_varint()
        if long_count < 0 or long_count > 512:
            raise ChunkFormatError(f"高度图长度非法: {long_count}")
        data = [buf.read_ulong() for _ in range(long_count)]
        heightmaps[HEIGHTMAP_TYPES.get(heightmap_type, f"TYPE_{heightmap_type}")] = data
    return heightmaps


def _parse_heightmaps_nbt(buf: PacketBuffer) -> dict[str, list[int]]:
    """1.16 ~ 1.21.4：高度图是一个 NBT 复合标签（键名为类型名）"""
    value, end = nbt.parse_named_tag(buf.data, buf.offset)
    buf.offset = end
    if not isinstance(value, dict):
        return {}
    heightmaps: dict[str, list[int]] = {}
    for name, data in value.items():
        if isinstance(data, (list, tuple)):
            heightmaps[str(name)] = [int(v) for v in data]
    return heightmaps


def _parse_block_entities(buf: PacketBuffer, anonymous: bool = True) -> list[dict[str, Any]]:
    count = buf.read_varint()
    if count < 0 or count > 4096:
        raise ChunkFormatError(f"方块实体数量非法: {count}")
    entities: list[dict[str, Any]] = []
    for _ in range(count):
        start_of_entry = buf.offset
        try:
            # 布局 A（1.18 ~ 1.21.x）：Packed XZ(ubyte) + Y(short) + Type(VarInt) + NBT
            packed_xz = buf.read_ubyte()
            y = buf.read_short()
            type_id = buf.read_varint()
            start = buf.offset
            value, end = _read_block_entity_nbt(buf, anonymous)
            buf.offset = end
            entities.append({
                "packed_xz": packed_xz,
                "local_x": (packed_xz >> 4) & 0x0F,
                "local_z": packed_xz & 0x0F,
                "y": y,
                "type_id": type_id,
                "nbt": value,
                "nbt_bytes": bytes(buf.data[start:end]),
            })
        except Exception:
            # 布局 B（部分新版本）：Position(long) + Type(VarInt) + NBT
            buf.offset = start_of_entry
            try:
                pos = buf.read_position()
                type_id = buf.read_varint()
                start = buf.offset
                value, end = _read_block_entity_nbt(buf, anonymous)
                buf.offset = end
                entities.append({
                    "x": pos[0],
                    "y": pos[1],
                    "z": pos[2],
                    "type_id": type_id,
                    "nbt": value,
                    "nbt_bytes": bytes(buf.data[start:end]),
                })
            except Exception as exc:
                raise ChunkFormatError(f"方块实体解析失败: {exc}") from exc
    return entities


def _read_block_entity_nbt(buf: PacketBuffer, anonymous: bool = True) -> tuple[Any, int]:
    """
    读取方块实体的 NBT 数据。

    线上协议中的 NBT **不是**磁盘那种具名格式，具体取决于版本
    （数据来源 PrismarineJS pc/<ver>/proto.yml + prismarine-nbt/nbt.json）：

    * 1.16 / 1.17：`blockEntities: nbt[]varint`，`nbt` = `optionalNbt`
      → 可选**具名**（首字节 0 表示不存在，否则 type + name + value）
    * 1.18+：`blockEntities: chunkBlockEntity[]varint`，其中
      `nbtData: anonOptionalNbt` → 可选**匿名**（首字节 0 表示不存在，
      否则 type + value，**没有名字**）

    参数 `anonymous` 用于区分这两族。为容错，主路径失败后会回退到另一种。
    """
    start = buf.offset
    # 可选：首字节为 0（TAG_End）表示该方块实体无 NBT
    if start < len(buf.data) and buf.data[start] == nbt.TAG_END:
        return None, start + 1

    primary = nbt.parse_anonymous_tag if anonymous else nbt.parse_named_tag
    fallback = nbt.parse_named_tag if anonymous else nbt.parse_anonymous_tag
    try:
        return primary(buf.data, start)
    except Exception:
        return fallback(buf.data, start)


def _read_light_array(buf: PacketBuffer) -> bytes:
    """
    读取单个光照数组（2048 字节 = 4096 个 4bit 亮度值）。

    线上格式为「VarInt 长度前缀 + 数据」，长度恒为 2048：
      * minecraft.wiki《Java Edition protocol/Data types》§ Light Data：
        Sky/Block Light array = Prefixed Array (2048) of Byte
      * PrismarineJS protocol.json：array(countType=varint) of array(countType=varint) of u8
      * 原版实现：FriendlyByteBuf.readByteArray(2048)（先读 VarInt 长度）

    极少数非标准服务端会省略该前缀，故这里做兼容回退。
    """
    start = buf.offset
    try:
        length = buf.read_varint()
    except Exception:
        length = -1
    if length == LIGHT_ARRAY_SIZE and buf.remaining >= LIGHT_ARRAY_SIZE:
        return buf.read(LIGHT_ARRAY_SIZE)
    # 回退：无长度前缀
    buf.offset = start
    return buf.read(LIGHT_ARRAY_SIZE)


def _parse_light(buf: PacketBuffer) -> dict[str, Any]:
    sky_mask = buf.read_bitset()
    block_mask = buf.read_bitset()
    empty_sky_mask = buf.read_bitset()
    empty_block_mask = buf.read_bitset()

    sky_count = buf.read_varint()
    if sky_count < 0 or sky_count > 2048:
        raise ChunkFormatError(f"天空光照数组数量非法: {sky_count}")
    sky_arrays = [_read_light_array(buf) for _ in range(sky_count)]

    block_count = buf.read_varint()
    if block_count < 0 or block_count > 2048:
        raise ChunkFormatError(f"方块光照数组数量非法: {block_count}")
    block_arrays = [_read_light_array(buf) for _ in range(block_count)]

    return {
        "sky_light_mask": sky_mask,
        "block_light_mask": block_mask,
        "empty_sky_light_mask": empty_sky_mask,
        "empty_block_light_mask": empty_block_mask,
        "sky_light": sky_arrays,
        "block_light": block_arrays,
    }


def _read_one_section(
    buf: PacketBuffer,
    has_fluid_count: bool,
    has_data_length: bool,
) -> ChunkSection:
    block_count = buf.read_short()
    if block_count == -1:
        # 空节：无调色板/生物群系数据
        return ChunkSection(block_count=-1, fluid_count=0, block_states=[], biomes=[])
    if not (0 <= block_count <= SECTION_BLOCK_COUNT):
        raise ChunkFormatError(f"节方块数非法: {block_count}")
    fluid_count = buf.read_short() if has_fluid_count else 0
    block_values, block_palette, block_bits, block_single = _read_paletted_container(
        buf, SECTION_BLOCK_COUNT, 4, 15, has_data_length
    )
    biome_values, biome_palette, biome_bits, biome_single = _read_paletted_container(
        buf, SECTION_BIOME_COUNT, 1, 7, has_data_length
    )
    return ChunkSection(
        block_count=block_count,
        fluid_count=fluid_count,
        block_states=block_values,
        biomes=biome_values,
        block_palette=block_palette,
        biome_palette=biome_palette,
        block_bits=block_bits,
        biome_bits=biome_bits,
        block_single=block_single,
        biome_single=biome_single,
    )


def _parse_sections(
    buf: PacketBuffer,
    section_count: int | None,
    has_fluid_count: bool = True,
    has_data_length: bool = False,
) -> list[ChunkSection]:
    """
    解析 section 列表。

    `section_count` 为 None 时表示「贪婪模式」：一直解析到字节耗尽为止
    （用于反推真实布局，见 `_parse_sections_adaptive`）。
    """
    sections: list[ChunkSection] = []
    if section_count is None:
        # 贪婪模式：一直解析到字节耗尽（受 MAX_SECTIONS 保护）
        while not buf.at_end() and len(sections) < MAX_SECTIONS:
            sections.append(_read_one_section(buf, has_fluid_count, has_data_length))
    else:
        for _ in range(section_count):
            sections.append(_read_one_section(buf, has_fluid_count, has_data_length))
    return sections


def _parse_sections_adaptive(
    data: bytes,
    declared_count: int,
    declared_fluid: bool,
    declared_data_length: bool,
) -> tuple[list[ChunkSection], bool, bool] | None:
    """
    自适应解析 section 区，返回 `(sections, has_fluid_count, has_data_length)`。

    为什么需要它：**ViaVersion / ViaBackwards 这类跨版本插件只翻译包 ID 与
    连接状态机，不翻译区块负载**。因此一个「协议 773」的连接里，区块负载
    可能仍是旧核心格式（世界高度 256 / 16 节、无 fluid_count、带数据长度
    前缀）。仅凭协议号选布局必然解析失败（实测：三个真实区块包全部失败）。

    策略：按「**必须精确消费全部字节**」逐个候选布局试解，
    优先级 = 协议声明布局 → 其他布局；每个布局先试声明节数，再试贪婪模式。
    """
    candidates: list[tuple[bool, bool]] = []
    for cand in ((declared_fluid, declared_data_length), (False, False),
                 (True, False), (False, True), (True, True)):
        if cand not in candidates:
            candidates.append(cand)

    for fluid, data_length in candidates:
        # 1) 先按声明节数精确解析
        try:
            buf = PacketBuffer(data)
            sections = _parse_sections(buf, declared_count, fluid, data_length)
            if buf.at_end() and sections:
                return sections, fluid, data_length
        except Exception:
            pass
        # 2) 再按贪婪模式反推节数
        try:
            buf = PacketBuffer(data)
            sections = _parse_sections(buf, None, fluid, data_length)
            if buf.at_end() and sections:
                return sections, fluid, data_length
        except Exception:
            pass
    return None


def _apply_light(sections: list[ChunkSection], light: dict[str, Any]) -> None:
    """
    把光照数组分配到对应的 section。

    掩码位 i 对应「最低世界高度以下第 i-1 个」节，即 section 索引 i-1；
    因此 section 索引 s 对应掩码位 s+1。
    """
    def bit_set(mask: list[int], bit: int) -> bool:
        index = bit // 64
        if index < 0 or index >= len(mask):
            return False
        return bool((mask[index] >> (bit % 64)) & 1)

    sky_mask = light.get("sky_light_mask") or []
    block_mask = light.get("block_light_mask") or []
    sky_arrays = light.get("sky_light") or []
    block_arrays = light.get("block_light") or []

    sky_cursor = 0
    block_cursor = 0
    for section_index, section in enumerate(sections):
        bit = section_index + 1
        if bit_set(sky_mask, bit):
            if sky_cursor < len(sky_arrays):
                section.sky_light = sky_arrays[sky_cursor]
            sky_cursor += 1
        if bit_set(block_mask, bit):
            if block_cursor < len(block_arrays):
                section.block_light = block_arrays[block_cursor]
            block_cursor += 1


def parse_chunk_data_packet(
    payload: bytes,
    section_count: int = DEFAULT_SECTIONS,
    min_section_y: int = DEFAULT_MIN_SECTION_Y,
    has_fluid_count: bool = True,
    has_data_length: bool = False,
    has_nbt_heightmaps: bool = False,
    block_entity_anonymous: bool = True,
) -> ParsedChunk | None:
    """
    解析 Chunk Data and Update Light 包。
    结构非法（即该包不是区块包）时返回 None。

    `section_count` 必须与服务器实际发送的节数一致：
    主世界 24（384 格），下界/末地 16（256 格），模组可能更多。

    `has_fluid_count`：1.21.5 起 section 内多了 fluid_count 字段；
    1.18.2 ~ 1.21.4 没有该字段。
    `has_data_length`：1.21.5 起数据数组不再带 VarInt 长度前缀。
    `has_nbt_heightmaps`：1.16 ~ 1.21.4 高度图是单个 NBT 复合标签；
    1.21.5+ 是 Prefixed Array of Heightmap。
    `block_entity_anonymous`：1.16/1.17 的方块实体是「可选具名 NBT」
    （`nbt[]varint`），1.18+ 是「可选匿名 NBT」（`chunkBlockEntity`）。
    """
    if not (1 <= section_count <= MAX_SECTIONS):
        section_count = DEFAULT_SECTIONS

    try:
        buf = PacketBuffer(payload)
        chunk_x = buf.read_int()
        chunk_z = buf.read_int()
        if abs(chunk_x) > 1_000_000 or abs(chunk_z) > 1_000_000:
            return None

        heightmaps = (
            _parse_heightmaps_nbt(buf) if has_nbt_heightmaps else _parse_heightmaps(buf)
        )

        data_len = buf.read_varint()
        if data_len < 0 or data_len > 16 * 1024 * 1024:
            return None
        section_bytes = buf.read(data_len)

        # 退化包过滤：真实的区块包必然带有高度图，且节数据至少包含
        # section_count 个 Short（每个节 2 字节，空节为 -1）。二者皆无则
        # 判定为「结构巧合」（例如 UUID + 小 VarInt 的其他数据包）。
        if not heightmaps and data_len < section_count * 2:
            return None

        block_entities = _parse_block_entities(buf, anonymous=block_entity_anonymous)
        light = _parse_light(buf)

        if not buf.at_end():
            return None

        # 空区块的 Data 可能为空
        if data_len == 0:
            sections: list[ChunkSection] = []
            section_count_used = 0
        else:
            adaptive = _parse_sections_adaptive(
                section_bytes, section_count, has_fluid_count, has_data_length
            )
            if adaptive is None:
                return None
            sections, _fluid_used, _dlen_used = adaptive
            section_count_used = len(sections)
            _apply_light(sections, light)

        # 世界高度校正：ViaVersion 只翻译包 ID/状态机，不翻译区块负载，
        # 因此「协议 773」的连接可能送来经典高度（16 节 / 0..255）的区块。
        # 若实际节数与声明不符且恰为经典高度，则把最低节 Y 归零，
        # 否则整列方块会上下错位（例如 1.18+ 默认 -4 会让方块整体下移 64 格）。
        if section_count_used and section_count_used != section_count \
                and section_count_used == CLASSIC_SECTION_COUNT:
            min_section_y = 0

        return ParsedChunk(
            x=chunk_x,
            z=chunk_z,
            heightmaps=heightmaps,
            sections=sections,
            block_entities=block_entities,
            light=light,
            raw_section_bytes=section_bytes,
            min_section_y=min_section_y,
            section_count=section_count_used,
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# 1.8 legacy 区块解析
# --------------------------------------------------------------------------
# 1.8 线路上每节的数据布局（按 mask 位升序，仅含被置位的节）：
#   Blocks     8192 B  (每方块 uint16 LE，值 = (block_id << 4) | metadata)
#   然后「所有节」的 BlockLight 2048 B/节（每方块 4 位）
#   然后「所有节」的 SkyLight   2048 B/节（仅主世界；每方块 4 位）
#   末尾 Biomes 256 B (仅 fullChunk)
#
# 索引顺序为 YZX: idx = x + 16 * (z + 16 * y)
# 数据来源: MCP 9.19 S21PacketChunkData + PrismarineJS prismarine-chunk 1.8

LEGACY_SECTION_BLOCKS = 8192
LEGACY_SECTION_NIBBLES = 2048
LEGACY_SECTION_COUNT = 16
LEGACY_BIOME_COUNT = 256


def _unpack_nibbles(raw: bytes, count: int) -> list[int]:
    """把半字节数组展开为每项 4 位的列表（偶数索引在低 4 位）"""
    out: list[int] = []
    for byte in raw:
        out.append(byte & 0x0F)
        out.append((byte >> 4) & 0x0F)
    return out[:count]


def parse_legacy_chunk_data_packet(
    payload: bytes,
    registry: Any = None,
) -> ParsedChunk | None:
    """
    解析 1.8 的 Chunk Data（0x21）包。

    结构: Int x, Int z, Bool groundUp, UShort bitMap, VarInt size, Bytes data

    `registry` 为已加载的 `BlockStateRegistry`，用于把
    (block_id, metadata) 映射为现代全局状态 ID；缺失时退化为空气。
    """
    try:
        buf = PacketBuffer(payload)
        chunk_x = buf.read_int()
        chunk_z = buf.read_int()
        if abs(chunk_x) > 1_000_000 or abs(chunk_z) > 1_000_000:
            return None
        ground_up = buf.read_bool()
        bit_map = buf.read_ushort()
        data_len = buf.read_varint()
        if data_len < 0 or data_len > 16 * 1024 * 1024:
            return None
        data = buf.read(data_len)
        if not buf.at_end():
            return None

        return _parse_legacy_column(
            data, chunk_x, chunk_z, bit_map, ground_up, registry
        )
    except Exception:
        return None


def parse_legacy_map_chunk_bulk_packet(
    payload: bytes,
    registry: Any = None,
) -> list[ParsedChunk]:
    """
    解析 1.8 的 Map Chunk Bulk（0x26）包。

    结构: VarInt count, Bool skyLightSent,
          count × (Int x, Int z, UShort bitMap),
          count × (VarInt size, Bytes data)

    这是 1.8 初次进入世界时批量下发区块的主要方式；若只处理 Chunk Data
    会漏掉绝大多数区块。返回解析成功的区块列表（解析失败的列被跳过）。
    """
    results: list[ParsedChunk] = []
    try:
        buf = PacketBuffer(payload)
        count = buf.read_varint()
        if count < 0 or count > 4096:
            return results
        sky_light_sent = buf.read_bool()

        metas: list[tuple[int, int, int]] = []
        for _ in range(count):
            chunk_x = buf.read_int()
            chunk_z = buf.read_int()
            bit_map = buf.read_ushort()
            metas.append((chunk_x, chunk_z, bit_map))

        for chunk_x, chunk_z, bit_map in metas:
            data_len = buf.read_varint()
            if data_len < 0 or data_len > 16 * 1024 * 1024:
                break
            data = buf.read(data_len)
            parsed = _parse_legacy_column(
                data, chunk_x, chunk_z, bit_map, True, registry,
                sky_light=sky_light_sent,
            )
            if parsed is not None:
                results.append(parsed)
    except Exception:
        pass
    return results


def _parse_legacy_column(
    data: bytes,
    chunk_x: int,
    chunk_z: int,
    bit_map: int,
    ground_up: bool,
    registry: Any,
    sky_light: bool | None = None,
) -> ParsedChunk | None:
    """
    解析 1.8 单个区块列的数据体（Chunk Data 与 Map Chunk Bulk 共用）。

    `sky_light` 为 None 时按剩余长度推断（Chunk Data 包没有该标志）；
    Map Chunk Bulk 可直接传入头部的 skyLightSent。
    """
    included = [i for i in range(LEGACY_SECTION_COUNT) if bit_map & (1 << i)]
    section_count = len(included)
    if section_count == 0:
        return None

    base = section_count * LEGACY_SECTION_BLOCKS
    if len(data) < base:
        return None
    light_bytes = section_count * LEGACY_SECTION_NIBBLES
    remaining = len(data) - base

    # 天空光照：主世界发送。根据剩余长度推断（兼容下界/末地）。
    if sky_light is None:
        has_sky = remaining >= light_bytes * 2 + (
            LEGACY_BIOME_COUNT if ground_up else 0
        )
    else:
        has_sky = sky_light
    expected = base + light_bytes + (light_bytes if has_sky else 0)
    if ground_up:
        expected += LEGACY_BIOME_COUNT
    if len(data) < expected:
        return None

    offset = 0
    block_arrays: list[bytes] = []
    for _ in range(section_count):
        block_arrays.append(data[offset:offset + LEGACY_SECTION_BLOCKS])
        offset += LEGACY_SECTION_BLOCKS

    block_light_arrays: list[bytes] = []
    for _ in range(section_count):
        block_light_arrays.append(data[offset:offset + LEGACY_SECTION_NIBBLES])
        offset += LEGACY_SECTION_NIBBLES

    sky_arrays: list[bytes] = []
    if has_sky:
        for _ in range(section_count):
            sky_arrays.append(data[offset:offset + LEGACY_SECTION_NIBBLES])
            offset += LEGACY_SECTION_NIBBLES

    biomes: bytes | None = None
    if ground_up and len(data) - offset >= LEGACY_BIOME_COUNT:
        biomes = data[offset:offset + LEGACY_BIOME_COUNT]
        offset += LEGACY_BIOME_COUNT

    # 构建归一化的 section 列表（共 16 节，未发送的为空气）
    sections: list[ChunkSection] = []
    for section_index in range(LEGACY_SECTION_COUNT):
        if section_index not in included:
            sections.append(ChunkSection(
                block_count=-1, fluid_count=0, block_states=[], biomes=[]
            ))
            continue
        pos = included.index(section_index)
        state_ids = _legacy_section_states(block_arrays[pos], registry)
        block_count = sum(1 for sid in state_ids if sid != 0)
        palette, indices, bits, single = _palettize_states(state_ids)
        section = ChunkSection(
            block_count=block_count,
            fluid_count=0,
            block_states=indices,
            biomes=[],
            block_palette=palette,
            block_bits=bits,
            block_single=single,
        )
        section.block_light = block_light_arrays[pos]
        if has_sky:
            section.sky_light = sky_arrays[pos]
        sections.append(section)

    # 把生物群系（256 字节，索引 z*16+x）挂到 section 上（仅第一个）
    if biomes is not None and sections:
        sections[0].biomes = list(biomes)

    return ParsedChunk(
        x=chunk_x,
        z=chunk_z,
        heightmaps={},
        sections=sections,
        block_entities=[],
        light=None,
        raw_section_bytes=data,
        min_section_y=0,
        section_count=LEGACY_SECTION_COUNT,
        # 1.8 的 256 字节是旧版数字生物群系 ID（0~167），由内置表解析
        chunk_biomes=list(biomes) if biomes is not None else None,
    )


def _palettize_states(states: list[int]) -> tuple[list[int] | None, list[int], int, int | None]:
    """把一组全局状态 ID 压成调色板 + 索引。

    返回 (palette, indices, bits, single_value)：
      * 全部相同时 palette 为 None、single_value 为该值
      * 否则 palette 为去重后的有序列表，indices 为对应的索引
    """
    unique = sorted(set(states))
    if len(unique) == 1:
        return None, states, 0, unique[0]
    index_of = {value: i for i, value in enumerate(unique)}
    indices = [index_of[value] for value in states]
    bits = max(4, (len(unique) - 1).bit_length())
    return unique, indices, bits, None


def _legacy_section_states(blocks: bytes, registry: Any) -> list[int]:
    """
    把一节 1.8 数据（8192 字节 uint16 LE）转换为现代全局状态 ID 列表。

    每个 uint16 值 = (block_id << 4) | metadata。
    """
    states = [0] * 4096
    for i in range(4096):
        value = blocks[i * 2] | (blocks[i * 2 + 1] << 8)
        block_id = value >> 4
        metadata = value & 0x0F
        if block_id == 0:
            continue
        state_id = legacy_state_id(block_id, metadata, registry)
        states[i] = state_id if state_id is not None else 0
    return states


# --------------------------------------------------------------------------
# 1.9 ~ 1.12.2 mid 区块解析
# --------------------------------------------------------------------------
# 包结构（协议 107 ~ 340，数据来源 wiki.vg《Chunk Data》/《Chunk Format》，
# id 逐一核对 PrismarineJS pc/1.12/blocks.json）:
#   Int x, Int z, Bool groundUp, VarInt primaryBitMask, VarInt size,
#   Byte[] data, VarInt blockEntityCount + 具名 NBT 复合标签数组
#   （这个时代的方块实体**就在区块包里**，每个 NBT 自带 x/y/z/id，
#     也可以随后用 Update Block Entity 增量更新）
#
# data 内按 mask 位升序逐节交错排列：
#   UByte bitsPerBlock
#   VarInt paletteLen（0 = 直接模式，值即全局 id；>0 = VarInt[] 全局 id 调色板）
#   VarInt longCount + Long[] data
#   BlockLight 2048 B
#   SkyLight   2048 B（仅主世界；下界/末地不发送）
# 末尾 Biomes 256 B（仅 groundUp，索引 z*16+x，旧版数字群系 ID）。
#
# 调色板值 = 全局 id = (block_id << 4) | metadata（13 位）。
# 与现代格式的两个关键差异：
#   1. 位打包**跨 long 边界**（方块 5 的位 53~64 在第一个 long、位 65 起在
#     第二个 long），4096×bpe 恰被 64 整除 → longCount 恒等于 64×bpe；
#   2. 光照数组内嵌在每节之后，而不是包尾统一排列。
MID_MIN_BPE = 4
MID_MAX_BPE = 16          # Forge 可到 16
MID_GLOBAL_ID_MAX = 0x1FFF  # 全局调色板 13 位


def _unpack_spanning_long_array(
    longs: list[int], bits: int, count: int
) -> list[int]:
    """
    1.9 ~ 1.12.2 的位打包数组：值可以跨 long 边界（与 1.16+ 的不跨界不同）。

    wiki.vg 给出的反序列化公式：
        startLong = i*bits / 64;  offset = i*bits % 64
        endLong = ((i+1)*bits - 1) / 64
        跨界时 value = data[start] >> offset | data[end] << (64 - offset)
    """
    mask = (1 << bits) - 1
    values: list[int] = []
    for i in range(count):
        start_bit = i * bits
        start_long = start_bit >> 6
        offset = start_bit & 63
        end_long = (start_bit + bits - 1) >> 6
        if end_long == start_long:
            value = longs[start_long] >> offset
        else:
            value = (longs[start_long] >> offset) | (longs[end_long] << (64 - offset))
        values.append(value & mask)
    return values


def _read_mid_section(buf: PacketBuffer, has_sky: bool, registry: Any) -> ChunkSection:
    """解析 1.9 ~ 1.12.2 的单个 section（方块 + 内嵌光照）"""
    bpe = buf.read_ubyte()
    if not (MID_MIN_BPE <= bpe <= MID_MAX_BPE):
        raise ChunkFormatError(f"mid 节位宽非法: {bpe}")

    palette_len = buf.read_varint()
    if palette_len < 0 or palette_len > SECTION_BLOCK_COUNT:
        raise ChunkFormatError(f"mid 调色板长度非法: {palette_len}")
    palette: list[int] | None = None
    if palette_len > 0:
        palette = []
        for _ in range(palette_len):
            entry = buf.read_varint()
            if entry < 0 or entry > MID_GLOBAL_ID_MAX:
                raise ChunkFormatError(f"mid 调色板项非法: {entry}")
            palette.append(entry)
    # palette_len == 0：直接模式（bpe >= 9），数据值即全局 id

    long_count = buf.read_varint()
    # 4096 × bpe 恰被 64 整除（4096 = 2^12），长度可精确校验
    if long_count != 64 * bpe:
        raise ChunkFormatError(f"mid 数据数组长度非法: {long_count} != {64 * bpe}")
    longs = [buf.read_ulong() for _ in range(long_count)]

    indices = _unpack_spanning_long_array(longs, bpe, SECTION_BLOCK_COUNT)
    block_light = buf.read(LIGHT_ARRAY_SIZE)
    sky_light = buf.read(LIGHT_ARRAY_SIZE) if has_sky else None

    # 全局 id = (block_id << 4) | metadata -> 现代状态 id（同值只查一次）
    cache: dict[int, int] = {}

    def to_state(global_id: int) -> int:
        if global_id in cache:
            return cache[global_id]
        state = legacy_state_id(global_id >> 4, global_id & 0x0F, registry) or 0
        cache[global_id] = state
        return state

    if palette is not None:
        mapped = [to_state(entry) for entry in palette]
        if any(index >= len(mapped) for index in indices):
            raise ChunkFormatError("mid 调色板索引越界")
        states = [mapped[index] for index in indices]
    else:
        states = [to_state(index) for index in indices]

    palette_out, indices_out, bits, single = _palettize_states(states)
    section = ChunkSection(
        block_count=sum(1 for state in states if state != 0),
        fluid_count=0,
        block_states=indices_out,
        biomes=[],
        block_palette=palette_out,
        block_bits=bits,
        block_single=single,
    )
    section.block_light = block_light
    if sky_light is not None:
        section.sky_light = sky_light
    return section


# 1.9 / 1.10 的方块实体 id 未命名空间化（TileEntity.register 的字面量），
# 1.11+ 线路上已是 minecraft:xxx。写入 DataVersion 2975 的现代世界时
# 必须规整，否则原版校验（仅允许 [a-z0-9_.-]）会丢弃整个方块实体。
MID_TILE_IDS: dict[str, str] = {
    "furnace": "minecraft:furnace",
    "chest": "minecraft:chest",
    "enderchest": "minecraft:ender_chest",
    "sign": "minecraft:sign",
    "mobspawner": "minecraft:spawner",
    "music": "minecraft:jukebox",
    "hopper": "minecraft:hopper",
    "skull": "minecraft:skull",
    "banner": "minecraft:banner",
    "beacon": "minecraft:beacon",
    "flowerpot": "minecraft:flower_pot",
    "piston": "minecraft:piston",
    "cauldron": "minecraft:cauldron",
    "brewingstand": "minecraft:brewing_stand",
    "enchanttable": "minecraft:enchanting_table",
    "endgateway": "minecraft:end_gateway",
    "control": "minecraft:command_block",
    "structure": "minecraft:structure_block",
    "dldetector": "minecraft:daylight_detector",
    "comparator": "minecraft:comparator",
    "dispenser": "minecraft:dispenser",
    "dropper": "minecraft:dropper",
    "airportal": "minecraft:end_portal",
}


def _parse_mid_block_entities(buf: PacketBuffer) -> list[dict[str, Any]]:
    """解析 1.9 ~ 1.12.2 的包内方块实体（VarInt count + 具名 NBT 复合标签）"""
    count = buf.read_varint()
    if count < 0 or count > 4096:
        raise ChunkFormatError(f"方块实体数量非法: {count}")
    entities: list[dict[str, Any]] = []
    for _ in range(count):
        value, end = nbt.parse_named_tag(buf.data, buf.offset)
        buf.offset = end
        if not isinstance(value, dict):
            raise ChunkFormatError("mid 方块实体不是复合标签")
        try:
            x, y, z = int(value["x"]), int(value["y"]), int(value["z"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChunkFormatError("mid 方块实体缺少坐标") from exc
        tile_id = value.get("id")
        if isinstance(tile_id, str) and ":" not in tile_id:
            normalized = MID_TILE_IDS.get(tile_id.lower())
            if normalized is not None:
                value["id"] = nbt.String(normalized)
        entities.append({"x": x, "y": y, "z": z, "type_id": -1, "nbt": value})
    return entities


def parse_mid_chunk_data_packet(
    payload: bytes,
    registry: Any = None,
) -> ParsedChunk | None:
    """
    解析 1.9 ~ 1.12.2 的 Chunk Data 包。

    结构: Int x, Int z, Bool groundUp, VarInt primaryBitMask, VarInt size,
          Bytes data, VarInt blockEntityCount + NBT[]

    `registry` 为已加载的 `BlockStateRegistry`，用于把
    (block_id, metadata) 映射为现代全局状态 ID；缺失时退化为空气。
    """
    try:
        buf = PacketBuffer(payload)
        chunk_x = buf.read_int()
        chunk_z = buf.read_int()
        if abs(chunk_x) > 1_000_000 or abs(chunk_z) > 1_000_000:
            return None
        ground_up = buf.read_bool()
        bit_map = buf.read_varint()
        if bit_map < 0 or bit_map > 0xFFFF:
            return None
        data_len = buf.read_varint()
        if data_len < 0 or data_len > 16 * 1024 * 1024:
            return None
        data = buf.read(data_len)
        if data_len and not data:
            return None
        block_entities = _parse_mid_block_entities(buf)
        if not buf.at_end():
            return None

        included = [i for i in range(CLASSIC_SECTION_COUNT) if bit_map & (1 << i)]

        # 天空光照是否发送无法从包头判断（仅主世界发送），按两个假设试解；
        # 解析错位会在下一节的位宽/长度校验处快速失败。
        parsed_map: dict[int, ChunkSection] | None = None
        biomes: list[int] | None = None
        for has_sky in (True, False):
            try:
                sec_buf = PacketBuffer(data)
                sections: dict[int, ChunkSection] = {}
                for section_index in included:
                    sections[section_index] = _read_mid_section(
                        sec_buf, has_sky, registry
                    )
                chunk_biomes: list[int] | None = None
                remaining = sec_buf.remaining
                if remaining == LEGACY_BIOME_COUNT:
                    chunk_biomes = list(sec_buf.read(LEGACY_BIOME_COUNT))
                elif remaining != 0:
                    raise ChunkFormatError(f"mid 区块尾部剩余 {remaining} 字节")
                if ground_up and chunk_biomes is None and included:
                    # groundUp=true 的完整区块应带群系；不过部分服务端省略，
                    # 保持容忍（缺群系只影响颜色，不影响方块）。
                    pass
                parsed_map = sections
                biomes = chunk_biomes
                break
            except Exception:
                continue
        if parsed_map is None:
            return None

        # 归一化为 16 节列表（未发送的为空气）
        section_list: list[ChunkSection] = []
        for section_index in range(CLASSIC_SECTION_COUNT):
            section = parsed_map.get(section_index)
            if section is None:
                section_list.append(ChunkSection(
                    block_count=-1, fluid_count=0, block_states=[], biomes=[]
                ))
            else:
                section_list.append(section)
        if biomes is not None and section_list:
            section_list[0].biomes = biomes

        return ParsedChunk(
            x=chunk_x,
            z=chunk_z,
            heightmaps={},
            sections=section_list,
            block_entities=block_entities,
            light=None,
            raw_section_bytes=data,
            min_section_y=0,
            section_count=CLASSIC_SECTION_COUNT,
            # 1.9 ~ 1.12.2 的 256 字节是旧版数字生物群系 ID（同 1.8）
            chunk_biomes=biomes,
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# 1.16.5 flattened 区块解析
# --------------------------------------------------------------------------
# 1.16.5 Chunk Data 包结构（PrismarineJS pc/1.16.2 proto.yml）:
#   Int x, Int z, Bool groundUp, VarInt bitMap, NBT heightmaps,
#   [groundUp ? VarInt biome_count + VarInt[] biomes(1024) : -],
#   VarInt size, Bytes data, VarInt blockEntityCount + NBT[]
#
# section: Short block_count, UByte BPE, [palette], Long[] data
#          （无 fluid_count；生物群系是独立的 1024 项数组）
CHUNK_BIOME_COUNT_1_16 = 4 * 4 * 64


def _read_bitmap_bitset(buf: PacketBuffer, section_count: int) -> int:
    """
    读取 1.17 的 `bitMap: i64[]varint` 位图，转换为普通整数掩码。

    该位图以 64 位为单位（每 64 节一个 long），最低位对应最低的节。
    解析失败时返回全 1（保守假设所有节都存在）。
    """
    try:
        long_count = buf.read_varint()
        if long_count < 0 or long_count > 8:
            return (1 << max(section_count, 1)) - 1
        mask = 0
        for index in range(long_count):
            value = buf.read_long()
            # 每个 long 贡献 64 位；只保留 section_count 范围内的位
            for bit in range(64):
                if value & (1 << bit):
                    section = index * 64 + bit
                    if section < section_count:
                        mask |= 1 << section
        return mask
    except Exception:
        return (1 << max(section_count, 1)) - 1


def parse_flattened_chunk_data_packet(
    payload: bytes,
    section_count: int = 16,
    min_section_y: int = 0,
    has_ground_up: bool = True,
    bitmap_bitset: bool = False,
) -> ParsedChunk | None:
    """
    解析 1.16 / 1.17 的 Chunk Data（1.16 = 0x20，1.17 = 0x22）包。

    1.16 与 1.17 的区块头差异（数据来源 PrismarineJS pc/1.16.2 与 pc/1.17.1）：
      * 1.16：`Bool groundUp` + `VarInt bitMap`
      * 1.17：**没有** groundUp；`bitMap` 是 `i64[]varint`（位图），生物群系总是下发

    共同点：
      * section 内没有 fluid_count，数据数组带 VarInt 长度前缀
      * 生物群系不是每节一个调色板容器，而是全局的 1024 项 VarInt 数组

    参数 `has_ground_up` / `bitmap_bitset` 用于区分这两个版本。
    """
    try:
        buf = PacketBuffer(payload)
        chunk_x = buf.read_int()
        chunk_z = buf.read_int()
        if abs(chunk_x) > 1_000_000 or abs(chunk_z) > 1_000_000:
            return None

        if has_ground_up:
            ground_up = buf.read_bool()
            bit_map = buf.read_varint()
            if bit_map < 0 or bit_map > 0xFFFF:
                return None
        else:
            # 1.17：生物群系总是下发，没有 groundUp
            ground_up = True
            bit_map = _read_bitmap_bitset(buf, section_count)

        heightmaps = _parse_heightmaps_nbt(buf)

        biomes: list[int] = []
        if ground_up:
            biome_count = buf.read_varint()
            if biome_count < 0 or biome_count > 4096:
                return None
            biomes = [buf.read_varint() for _ in range(biome_count)]

        data_len = buf.read_varint()
        if data_len < 0 or data_len > 16 * 1024 * 1024:
            return None
        section_bytes = buf.read(data_len)

        block_entities = _parse_block_entities(buf, anonymous=False)
        if not buf.at_end():
            return None

        sections: list[ChunkSection] = []
        sec_buf = PacketBuffer(section_bytes)
        for index in range(section_count):
            if not (bit_map & (1 << index)):
                sections.append(ChunkSection(
                    block_count=-1, fluid_count=0, block_states=[], biomes=[]
                ))
                continue
            sections.append(_parse_flattened_section(sec_buf))
        if not sec_buf.at_end():
            return None

        first_layer = _apply_global_biomes(sections, biomes)

        return ParsedChunk(
            x=chunk_x,
            z=chunk_z,
            heightmaps=heightmaps,
            sections=sections,
            block_entities=block_entities,
            light=None,
            raw_section_bytes=section_bytes,
            min_section_y=min_section_y,
            section_count=section_count,
            chunk_biomes=first_layer,
        )
    except Exception:
        return None


def _parse_flattened_section(buf: PacketBuffer) -> ChunkSection:
    """解析 1.16/1.17 的 section（无 fluid_count，数据数组带长度前缀）"""
    block_count = buf.read_short()
    if block_count == -1:
        return ChunkSection(block_count=-1, fluid_count=0, block_states=[], biomes=[])
    block_values, block_palette, block_bits, block_single = _read_paletted_container(
        buf, SECTION_BLOCK_COUNT, 4, 15, has_data_length=True
    )
    return ChunkSection(
        block_count=block_count,
        fluid_count=0,
        block_states=block_values,
        biomes=[],
        block_palette=block_palette,
        block_bits=block_bits,
        block_single=block_single,
    )


def _apply_global_biomes(
    sections: list[ChunkSection], biomes: list[int]
) -> list[int] | None:
    """
    把 1.16/1.17 的全局 1024 项生物群系数组按 4x4x4 切分到各 section。

    1024 = 4(Y) × 4(Z) × 4(X) × 16 节，每节 64 项。
    （1.16/1.17 世界固定 16 节、min_y=0，故 Y 索引 0..3 对应 y 0..15。）

    返回**第一层**（Y=0）的 64 项，供 Anvil 输出时作为整个区块的代表值
    （Anvil 只有 64 项空间，读取时对整列取第一层）。
    """
    if not biomes:
        return None
    for section_index, section in enumerate(sections):
        base = section_index * SECTION_BIOME_COUNT
        chunk_biomes = biomes[base:base + SECTION_BIOME_COUNT]
        if len(chunk_biomes) == SECTION_BIOME_COUNT:
            section.biomes = chunk_biomes
    if len(biomes) >= SECTION_BIOME_COUNT:
        return biomes[:SECTION_BIOME_COUNT]
    return None


# --------------------------------------------------------------------------
# 1.18.2 区块解析
# --------------------------------------------------------------------------
# 1.18.2 Chunk Data and Update Light 包结构（PrismarineJS pc/1.18 proto.yml）:
#   Int x, Int z, NBT heightmaps, VarInt size, Bytes sections,
#   VarInt blockEntityCount + chunkBlockEntity[], Light Data
#   section: Short block_count, UByte BPE, [palette], Long[] data,
#            PalettedContainer biomes(64)
#   与 1.21.x 的唯一区别：section 内没有 fluid_count。
def parse_caves_chunk_data_packet(
    payload: bytes,
    section_count: int = 24,
    min_section_y: int = -4,
) -> ParsedChunk | None:
    """解析 1.18.2 的 Chunk Data and Update Light（0x22）包"""
    return parse_chunk_data_packet(
        payload,
        section_count=section_count,
        min_section_y=min_section_y,
        has_fluid_count=False,
        has_data_length=True,
        has_nbt_heightmaps=True,
        block_entity_anonymous=True,
    )


def parse_update_light_packet(payload: bytes) -> ParsedLight | None:
    """解析独立的 Update Light 包"""
    try:
        buf = PacketBuffer(payload)
        chunk_x = buf.read_varint()
        chunk_z = buf.read_varint()
        if abs(chunk_x) > 1_000_000 or abs(chunk_z) > 1_000_000:
            return None
        light = _parse_light(buf)
        if not buf.at_end():
            return None
        return ParsedLight(
            x=chunk_x,
            z=chunk_z,
            sky_light_mask=light["sky_light_mask"],
            block_light_mask=light["block_light_mask"],
            empty_sky_light_mask=light["empty_sky_light_mask"],
            empty_block_light_mask=light["empty_block_light_mask"],
            sky_light=light["sky_light"],
            block_light=light["block_light"],
        )
    except Exception:
        return None


def parse_block_entity_data_packet(payload: bytes) -> dict[str, Any] | None:
    """解析独立的 Block Entity Data 包"""
    try:
        buf = PacketBuffer(payload)
        x, y, z = buf.read_position()
        type_id = buf.read_varint()
        start = buf.offset
        value, end = nbt.parse_named_tag(buf.data, buf.offset)
        buf.offset = end
        if not buf.at_end():
            return None
        return {
            "x": x,
            "y": y,
            "z": z,
            "chunk_x": x >> 4,
            "chunk_z": z >> 4,
            "type_id": type_id,
            "nbt": value,
            "nbt_bytes": bytes(buf.data[start:end]),
        }
    except Exception:
        return None


def parse_unload_chunk_packet(payload: bytes) -> tuple[int, int] | None:
    """
    解析 Unload Chunk 包。
    结构: Chunk Z (Int), Chunk X (Int) —— 注意顺序为 Z 在前。
    """
    if len(payload) != 8:
        return None
    try:
        buf = PacketBuffer(payload)
        chunk_z = buf.read_int()
        chunk_x = buf.read_int()
        if abs(chunk_x) > 1_000_000 or abs(chunk_z) > 1_000_000:
            return None
        return chunk_x, chunk_z
    except Exception:
        return None


# --------------------------------------------------------------------------
# Anvil 区块 NBT 构建
# --------------------------------------------------------------------------
def _pack_values(values: list[int], bits: int, entries: int) -> list[int]:
    """按指定位宽把值打包进 Long 数组（低位在前，与原版一致）"""
    if bits <= 0:
        return []
    entries_per_long = 64 // bits
    long_count = math.ceil(entries / entries_per_long)
    longs = [0] * long_count
    mask = (1 << bits) - 1
    for i, value in enumerate(values):
        if i >= entries:
            break
        long_index = i // entries_per_long
        bit_index = (i % entries_per_long) * bits
        longs[long_index] |= (value & mask) << bit_index
    # 转换为有符号 64 位
    signed = []
    for value in longs:
        if value >= 1 << 63:
            value -= 1 << 64
        signed.append(value)
    return signed


def _bits_for_palette(palette_len: int, min_bits: int) -> int:
    """根据调色板长度决定打包位宽（与游戏内规则一致）"""
    if palette_len <= 1:
        return 0
    return max(min_bits, (palette_len - 1).bit_length())


def _to_signed_long(value: int) -> int:
    if value >= 1 << 63:
        return value - (1 << 64)
    return value


def _build_palette_entry(registry: Any, state_id: int) -> Any:
    """把全局方块状态 ID 转成 Anvil 调色板项（Name + Properties）"""
    if registry is not None and getattr(registry, "loaded", False):
        state = registry.name_for(state_id)
        if state is not None:
            if state.properties:
                return nbt.compound({
                    "Name": nbt.String(state.name),
                    "Properties": nbt.compound({
                        key: nbt.String(value)
                        for key, value in state.properties.items()
                    }),
                })
            return nbt.compound({"Name": nbt.String(state.name)})
    # 降级：无法解析时使用空气，至少保证文件可被加载
    return nbt.compound({"Name": nbt.String("minecraft:air")})


def _build_block_states(section: ChunkSection, registry: Any) -> dict[str, Any]:
    """构建方块状态（palette + data），palette 项为方块名称 + 属性"""
    if section.block_single is not None:
        return {"palette": nbt.List(
            nbt.TAG_COMPOUND, [_build_palette_entry(registry, section.block_single)]
        )}

    palette = section.block_palette
    if not palette:
        # 直接模式：无 palette，数据按原采样位宽重新打包
        bits = max(4, section.block_bits)
        data = _pack_values(section.block_states, bits, SECTION_BLOCK_COUNT)
        return {"data": nbt.LongArray(data)}

    bits = _bits_for_palette(len(palette), 4)
    if bits == 0:
        return {"palette": nbt.List(
            nbt.TAG_COMPOUND, [_build_palette_entry(registry, palette[0])]
        )}
    data = _pack_values(section.block_states, bits, SECTION_BLOCK_COUNT)
    return {
        "palette": nbt.List(
            nbt.TAG_COMPOUND,
            [_build_palette_entry(registry, value) for value in palette],
        ),
        "data": nbt.LongArray(data),
    }


# 生物群系调色板的元素类型是 **String**（生物群系资源位置），
# 不是 Compound。官方 wiki《Chunk format》(1.18+)：
#   "biomes: **palette**: Set of different biomes ... **{{nbt|string|Name}}**:
#    Biome resource location"
# 真实 1.21.10 世界的 `biomes.palette` 实测为 `List<8>`（TAG_String）。
#
# 名称来源见 `biome_registry`：1.18+ 用服务端下发的网络注册表（下标即 ID），
# 1.16/1.17 用内置数字 ID 表，1.8 用内置旧数字 ID 表；拿不到时用占位值。
def _biome_names_for(
    ids: list[int],
    biome_registry: BiomeRegistry | None,
    normalize: bool = False,
) -> list[str]:
    """把一组生物群系 ID 转成资源位置列表"""
    if biome_registry is None or not biome_registry.loaded:
        return [PLACEHOLDER_BIOME for _ in ids]
    names = [biome_registry.name_for(bid) for bid in ids]
    if normalize:
        names = [normalize_biome_name(n) for n in names]
    return names


def _build_biomes(
    section: ChunkSection,
    biome_registry: BiomeRegistry | None = None,
    normalize_biomes: bool = False,
) -> dict[str, Any]:
    """
    构建生物群系（`palette` 为字符串列表，`data` 为打包索引）。

    注意：`palette` 必须是 `List<TAG_STRING>`。写成 `List<TAG_COMPOUND>`
    会让 C2ME / 原版在加载 section 时报 `(Not a string -> default)`。
    """
    if section.biome_single is not None:
        names = _biome_names_for([section.biome_single], biome_registry,
                                 normalize_biomes)
        return {"palette": nbt.List(nbt.TAG_STRING, [nbt.String(names[0])])}

    palette = section.biome_palette
    if not palette:
        if section.biome_single is None and section.biome_bits <= 0:
            # 旧版数据（1.8 / 1.16）：生物群系无调色板，统一占位
            return {"palette": nbt.List(nbt.TAG_STRING, [nbt.String(PLACEHOLDER_BIOME)])}
        bits = max(1, section.biome_bits)
        data = _pack_values(section.biomes, bits, SECTION_BIOME_COUNT)
        return {"data": nbt.LongArray(data)}

    bits = _bits_for_palette(len(palette), 1)
    if bits == 0:
        names = _biome_names_for([palette[0]], biome_registry, normalize_biomes)
        return {"palette": nbt.List(nbt.TAG_STRING, [nbt.String(names[0])])}
    data = _pack_values(section.biomes, bits, SECTION_BIOME_COUNT)
    names = _biome_names_for(list(palette), biome_registry, normalize_biomes)
    return {
        "palette": nbt.List(nbt.TAG_STRING, [nbt.String(n) for n in names]),
        "data": nbt.LongArray(data),
    }


def _pack_heightmap(values: list[int]) -> nbt.LongArray:
    """
    Anvil 高度图 = 256 个 9 位值，每 Long 装 7 个（共 37 个 Long）。
    现代版本线路上已是同一编码，长度符合时直接透传。
    """
    if len(values) == 37:
        return nbt.LongArray([_to_signed_long(v) for v in values])
    if not values:
        return nbt.LongArray([])
    longs: list[int] = []
    current = 0
    bit_pos = 0
    for value in values[:256]:
        current |= (value & 0x1FF) << bit_pos
        bit_pos += 9
        if bit_pos >= 63:
            longs.append(current & 0x7FFFFFFFFFFFFFFF)
            current = 0
            bit_pos = 0
    if bit_pos > 0 or not longs:
        longs.append(current & 0x7FFFFFFFFFFFFFFF)
    while len(longs) < 37:
        longs.append(0)
    return nbt.LongArray([_to_signed_long(v) for v in longs])


def _build_legacy_biome_container(
    chunk_biomes: list[int],
    biome_registry: BiomeRegistry,
    normalize: bool,
) -> dict[str, Any]:
    """
    旧版（1.8 / 1.16）区块：整个区块共享一份生物群系数据，构建 64 项调色板。

    Anvil 的生物群系是 4×4×4 单元（64 项，索引 `y4*16 + z4*4 + x4`）。
    旧版线路数据是 2D/3D 的稠密数组，需要降采样：

    * **1.8**（256 项，索引 `z*16 + x`）：取 Y=0 层、每 4 格采一点
      → `biomes[(z4*4)*16 + x4*4]`
    * **1.16/1.17**（1024 项，索引 `section*64 + y4*16 + z4*4 + x4`）：
      取 section 0 的 Y=0 层 → `biomes[z4*4 + x4]`

    两者都只有 4×4 的 XZ 分辨率，因此每个单元在 Y 方向重复 4 次得到 64 项。
    """
    n = len(chunk_biomes)
    if n >= 1024:
        # 1.16/1.17：前 16 项即 section 0 的 Y=0 层
        sample = [chunk_biomes[z4 * 4 + x4] for z4 in range(4) for x4 in range(4)]
    elif n >= 256:
        # 1.8：256 项 2D 数组，索引 z*16 + x
        sample = [chunk_biomes[(z4 * 4) * 16 + (x4 * 4)]
                  for z4 in range(4) for x4 in range(4)]
    else:
        return {"palette": nbt.List(nbt.TAG_STRING, [nbt.String(PLACEHOLDER_BIOME)])}

    # 4×4 (XZ) -> 4×4×4 (YZX)：每个单元在 Y 方向重复 4 次
    raw = [sample[z4 * 4 + x4]
           for _y4 in range(4) for z4 in range(4) for x4 in range(4)]

    names = [biome_registry.name_for(bid) for bid in raw]
    if normalize:
        names = [normalize_biome_name(n) for n in names]
    return {"palette": nbt.List(nbt.TAG_STRING, [nbt.String(n) for n in names])}


def build_chunk_nbt(
    chunk: ParsedChunk,
    registry: Any = None,
    data_version: int = 0,
    min_section_y: int | None = None,
    biome_registry: BiomeRegistry | None = None,
    normalize_biomes: bool = False,
) -> bytes:
    """
    把解析后的区块转换为 Anvil 区块 NBT（未压缩字节）。

    参数:
        registry: `BlockStateRegistry`，用于把方块状态 ID 转成名称。
                  为 None 时退化为空气占位（文件仍可加载）。
        data_version: 写入 `DataVersion`，决定游戏如何 DataFixUp。
        min_section_y: 最低节 Y（主世界 -4），默认取 chunk 自身值。
        biome_registry: `BiomeRegistry`，用于把生物群系 ID 转成名称。
                        为 None 时写占位值（颜色不准，但文件可加载）。
        normalize_biomes: 是否把旧版群系名映射到 1.18+ 的等价名
                          （输出 DataVersion ≤ 2975 时为 True）。
    """
    base_y = chunk.min_section_y if min_section_y is None else min_section_y

    air_palette = nbt.List(nbt.TAG_COMPOUND,
                           [nbt.compound({"Name": nbt.String("minecraft:air")})])
    # 生物群系调色板的元素必须是字符串（见 `_build_biomes` 的说明）
    plains_palette = nbt.List(nbt.TAG_STRING, [nbt.String(PLACEHOLDER_BIOME)])

    # 旧版（1.8 / 1.16）没有每节调色板，整个区块共享一份生物群系数据
    legacy_container: dict[str, Any] | None = None
    if chunk.chunk_biomes and biome_registry is not None and biome_registry.loaded:
        legacy_container = _build_legacy_biome_container(
            chunk.chunk_biomes, biome_registry, normalize_biomes
        )

    sections: list[Any] = []
    for index, section in enumerate(chunk.sections):
        if section.block_count < 0:
            # 空节也要写入（官方规范：世界高度内的所有节都存在于列表中，
            # 用单一空气调色板表示）。这样世界高度才完整。
            empty_tag: dict[str, Any] = {
                "Y": nbt.Byte(base_y + index),
                "block_states": nbt.compound({"palette": air_palette}),
                "biomes": nbt.compound(legacy_container or {"palette": plains_palette}),
            }
            # 空节同样可以携带光照数据
            if section.sky_light is not None:
                empty_tag["SkyLight"] = nbt.ByteArray(list(section.sky_light))
            if section.block_light is not None:
                empty_tag["BlockLight"] = nbt.ByteArray(list(section.block_light))
            sections.append(nbt.compound(empty_tag))
            continue

        block_states = nbt.compound(_build_block_states(section, registry))
        if legacy_container is not None:
            # 旧版：整个区块共用同一份 64 项生物群系
            biome_states = nbt.compound(legacy_container)
        else:
            biome_states = nbt.compound(
                _build_biomes(section, biome_registry, normalize_biomes)
            )

        section_tag: dict[str, Any] = {
            "Y": nbt.Byte(base_y + index),
            "block_count": nbt.Short(section.block_count),
            "fluid_count": nbt.Short(section.fluid_count),
            "block_states": block_states,
            "biomes": biome_states,
        }
        # 光照直接挂在 section 上（Anvil 没有独立的 Light 段）
        if section.sky_light is not None:
            section_tag["SkyLight"] = nbt.ByteArray(list(section.sky_light))
        if section.block_light is not None:
            section_tag["BlockLight"] = nbt.ByteArray(list(section.block_light))

        sections.append(nbt.compound(section_tag))

    heightmaps = nbt.compound({
        name: _pack_heightmap(data)
        for name, data in chunk.heightmaps.items()
    })

    # ------------------------------------------------------------------
    # 顶层必需键
    #
    # 与真实 1.21.10 世界（Paper 数据生成器产出）逐键比对后补齐。
    # 官方 wiki《Chunk format》(1.18+) 对这些键的说明：
    #   isLightOn    —— 光照是否已计算（缺失时部分优化模组会重算/告警）
    #   structures   —— 结构数据。**必须**同时含 `starts` 与 `References`，
    #                   否则原版读取时报 `Not a compound tag: ...`。
    #   block_ticks  —— 待更新的方块（红石/沙子等），无则为空表
    #   fluid_ticks  —— 待更新的流体，无则为空表
    #   PostProcessing —— 待接收更新的坐标，长度 = 节数（每节一个子表）
    # ------------------------------------------------------------------
    section_total = len(sections)
    root: dict[str, Any] = {
        "DataVersion": nbt.Int(data_version or chunk.data_version),
        "xPos": nbt.Int(chunk.x),
        "zPos": nbt.Int(chunk.z),
        "yPos": nbt.Int(base_y),
        "Status": nbt.String("full"),
        "LastUpdate": nbt.Long(0),
        "InhabitedTime": nbt.Long(0),
        "isLightOn": nbt.Byte(1),
        "sections": nbt.List(nbt.TAG_COMPOUND, sections),
        "Heightmaps": heightmaps,
        "structures": nbt.compound({
            "References": nbt.LongArray([]),
            "starts": nbt.compound({}),
        }),
        "block_ticks": nbt.List(nbt.TAG_COMPOUND, []),
        "fluid_ticks": nbt.List(nbt.TAG_COMPOUND, []),
        "PostProcessing": nbt.List(
            nbt.TAG_LIST,
            [nbt.List(nbt.TAG_SHORT, []) for _ in range(section_total)],
        ),
    }

    # block_entities 始终写入（官方 ChunkSerializer 恒写该列表，
    # 无方块实体时为空表）。真实 1.21.10 世界实测每区块都含此键。
    entities = nbt.List(nbt.TAG_COMPOUND, [])
    for entity in chunk.block_entities:
        value = entity.get("nbt")
        if not isinstance(value, dict):
            continue
        value.setdefault("x", nbt.Int(entity.get("x", 0)))
        value.setdefault("y", nbt.Int(entity.get("y", 0)))
        value.setdefault("z", nbt.Int(entity.get("z", 0)))
        entities.append(value)
    root["block_entities"] = entities

    return nbt.build_root(root, "")


def compress_chunk_nbt(raw_nbt: bytes) -> bytes:
    """使用 zlib 压缩（Anvil 压缩类型 2）"""
    return zlib.compress(raw_nbt, 6)
