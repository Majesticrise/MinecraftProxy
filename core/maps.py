"""
地图画捕获：累积 Map Data 包的色板，退出时写 data/map_<id>.dat。

线上格式（1.14+，含 1.21.x）：
    VarInt mapId, Byte scale, Bool locked,
    Bool hasIcons + 图标数组（跳过）,
    Byte columns; columns>0 时: Byte rows, Byte x, Byte y,
                    VarInt len + len 字节颜色（128x128 画布的局部增量）

磁盘格式（data/map_<id>.dat，gzip NBT）：
    root { DataVersion: Int,
           data { scale: Byte, dimension: String(1.16+)或Int,
                  xCenter/zCenter: Int, trackingPosition: Byte,
                  locked: Byte, colors: ByteArray(16384) } }
    colors 索引 = y*128 + x（行优先，x 最快）

导出的世界里用 `/give @s filled_map[minecraft:map_id=<id>]`
即可取出查看（1.20.5+）。
"""

from __future__ import annotations

from pathlib import Path

from . import nbt
from .datatypes import PacketBuffer
from .logger import setup_logger

logger = setup_logger(__name__)

MAP_SIZE = 128
MAX_MAPS = 512          # 防御：异常数据不无限吃内存
# data_version 从该值起 dimension 为字符串（1.16+）
DATA_VERSION_STRING_DIMENSION_MIN = 2529  # 1.16


class MapCollector:
    """按 mapId 累积 128×128 色板；退出时统一落盘。"""

    def __init__(self) -> None:
        self._canvases: dict[int, bytearray] = {}
        self._last_params: dict[int, tuple[int, bool]] = {}  # mapId -> (scale, locked)

    # ------------------------------------------------------------------ 解析
    def on_map_data(self, payload: bytes) -> bool:
        """
        解析一个 Map Data 包，返回是否成功应用（结构不符时静默返回 False，
        保证探测/解析失败不影响其他包）。
        """
        try:
            buf = PacketBuffer(payload)
            map_id = buf.read_varint()
            if map_id < 0 or map_id > 1 << 30:
                return False
            scale = buf.read_byte() & 0xFF
            locked = buf.read_bool()
            self._last_params[map_id] = (scale, locked)
            if buf.read_bool():  # hasIcons -> 图标数组（地图画不需要）
                icon_count = buf.read_varint()
                if not (0 <= icon_count <= 1024):
                    return False
                for _ in range(icon_count):
                    buf.read_byte()   # type + rotation（同一字节）
                    buf.read_byte()
                    buf.read_byte()   # x
                    buf.read_byte()   # z
                    if buf.read_bool():  # displayName 可选 NBT
                        _skip_optional_nbt(buf)

            columns = buf.read_byte()
            if columns <= 0:
                return buf.at_end()
            rows = buf.read_byte()
            x0 = buf.read_byte()
            y0 = buf.read_byte()
            data_len = buf.read_varint()
            if not (0 < data_len <= MAP_SIZE * MAP_SIZE):
                return False
            data = buf.read(data_len)
            if not buf.at_end():
                return False

            # 网络坐标是有符号 byte（-128..127）
            x0 &= 0xFF
            y0 &= 0xFF
            if x0 + columns > MAP_SIZE or y0 + rows > MAP_SIZE:
                return False
            if len(data) < columns * rows:
                return False

            canvas = self._canvas(map_id)
            pos = 0
            for row in range(rows):
                y = y0 + row
                base = y * MAP_SIZE + x0
                canvas[base:base + columns] = data[pos:pos + columns]
                pos += columns
            return True
        except Exception:
            return False

    def _canvas(self, map_id: int) -> bytearray:
        canvas = self._canvases.get(map_id)
        if canvas is None:
            if len(self._canvases) >= MAX_MAPS:
                # 简单淘汰：丢掉最早的（真实服务器一次会话遇到的地图数远小于上限）
                oldest = next(iter(self._canvases))
                self._canvases.pop(oldest, None)
            canvas = bytearray(MAP_SIZE * MAP_SIZE)
            self._canvases[map_id] = canvas
        return canvas

    # ------------------------------------------------------------------ 落盘
    @property
    def count(self) -> int:
        return len(self._canvases)

    def write_maps(self, world_dir: str, data_version: int,
                   dimension: str = "minecraft:overworld") -> int:
        """把所有画布写为 data/map_<id>.dat，返回写入张数（无内容则 0）。"""
        if not self._canvases:
            return 0
        out_dir = Path(world_dir) / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for map_id, canvas in self._canvases.items():
            try:
                path = out_dir / f"map_{map_id}.dat"
                if path.exists():
                    continue  # 不覆盖（可能是真实世界已有的地图）
                scale, locked = self._last_params.get(map_id, (0, True))
                data: dict[str, object] = {
                    "scale": nbt.Byte(scale),
                    "xCenter": nbt.Int(0),
                    "zCenter": nbt.Int(0),
                    "trackingPosition": nbt.Byte(0),
                    "locked": nbt.Byte(1 if locked else 0),
                    "colors": nbt.ByteArray(bytes(canvas)),
                }
                if data_version >= DATA_VERSION_STRING_DIMENSION_MIN:
                    data["dimension"] = nbt.String(dimension)
                else:
                    dim_ids = {"minecraft:overworld": 0, "minecraft:the_nether": -1,
                               "minecraft:the_end": 1}
                    data["dimension"] = nbt.Int(dim_ids.get(dimension, 0))
                root = {
                    "DataVersion": nbt.Int(data_version),
                    "data": nbt.compound(data),
                }
                import gzip
                with gzip.open(path, "wb") as fh:
                    fh.write(nbt.build_root(root, ""))
                written += 1
            except Exception as exc:
                logger.error(f"写 map_{map_id}.dat 失败: {exc}")
        if written:
            logger.info(f"地图数据: 已写入 {written} 张 -> data/map_*.dat")
        return written


def _skip_optional_nbt(buf: PacketBuffer) -> None:
    """跳过「首字节 0=无」的可选 NBT（匿名优先，失败回退具名）"""
    if buf.peek_ubyte() == nbt.TAG_END:
        buf.read(1)
        return
    start = buf.offset
    for parser in (nbt.parse_anonymous_tag, nbt.parse_named_tag):
        try:
            _value, end = parser(buf.data, start)
            buf.offset = end
            return
        except Exception:
            continue
    raise ValueError("图标 NBT 无法跳过")
