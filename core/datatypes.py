"""
Minecraft 协议基础数据类型读取工具

提供带边界检查的顺序读取缓冲区，覆盖协议文档中定义的全部基础类型。
（VarInt / VarLong / String / Identifier / UUID / Position / BitSet ...）

所有读取方法在数据不足时抛出 BufferError，方便调用方用 try/except
做“结构探测”（structural probing）。
"""

from __future__ import annotations

import struct
import uuid as _uuid

# 缓冲区可以是 bytes / bytearray / memoryview
BufferLike = bytes | bytearray | memoryview


class BufferError(Exception):
    """数据不足或格式非法"""


class PacketBuffer:
    """顺序读取缓冲区"""

    __slots__ = ("data", "offset")

    def __init__(self, data: BufferLike, offset: int = 0):
        self.data: BufferLike = data
        self.offset = offset

    # ------------------------------------------------------------------ 基础
    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def at_end(self) -> bool:
        return self.offset >= len(self.data)

    def _take(self, n: int) -> bytes:
        if n < 0:
            raise BufferError(f"读取长度为负数: {n}")
        end = self.offset + n
        if end > len(self.data):
            raise BufferError(f"需要 {n} 字节, 仅剩 {self.remaining}")
        out = bytes(self.data[self.offset:end])
        self.offset = end
        return out

    def read(self, n: int) -> bytes:
        return self._take(n)

    def skip(self, n: int) -> None:
        self._take(n)

    def peek_ubyte(self) -> int:
        if self.offset >= len(self.data):
            raise BufferError("数据不足")
        return self.data[self.offset]

    # -------------------------------------------------------------- 定长类型
    def read_bool(self) -> bool:
        return self._take(1)[0] != 0

    def read_byte(self) -> int:
        return int(struct.unpack(">b", self._take(1))[0])

    def read_ubyte(self) -> int:
        return self._take(1)[0]

    def read_short(self) -> int:
        return int(struct.unpack(">h", self._take(2))[0])

    def read_ushort(self) -> int:
        return int(struct.unpack(">H", self._take(2))[0])

    def read_int(self) -> int:
        return int(struct.unpack(">i", self._take(4))[0])

    def read_uint(self) -> int:
        return int(struct.unpack(">I", self._take(4))[0])

    def read_long(self) -> int:
        return int(struct.unpack(">q", self._take(8))[0])

    def read_ulong(self) -> int:
        return int(struct.unpack(">Q", self._take(8))[0])

    def read_float(self) -> float:
        return float(struct.unpack(">f", self._take(4))[0])

    def read_double(self) -> float:
        return float(struct.unpack(">d", self._take(8))[0])

    # --------------------------------------------------------------- 变长类型
    def read_varint(self) -> int:
        """读取 VarInt，返回带符号解释的 32 位整数"""
        value = 0
        for i in range(5):
            byte = self._take(1)[0]
            value |= (byte & 0x7F) << (7 * i)
            if (byte & 0x80) == 0:
                if value >= 1 << 31:
                    value -= 1 << 32
                return value
        raise BufferError("VarInt 超过 5 字节")

    def read_varlong(self) -> int:
        value = 0
        for i in range(10):
            byte = self._take(1)[0]
            value |= (byte & 0x7F) << (7 * i)
            if (byte & 0x80) == 0:
                if value >= 1 << 63:
                    value -= 1 << 64
                return value
        raise BufferError("VarLong 超过 10 字节")

    # ------------------------------------------------------------------ 复合
    def read_string(self, max_chars: int = 32767) -> str:
        length = self.read_varint()
        if length < 0 or length > max_chars * 4:
            raise BufferError(f"字符串长度非法: {length}")
        raw = self._take(length)
        return raw.decode("utf-8", errors="replace")

    def read_identifier(self) -> str:
        return self.read_string()

    def read_uuid(self) -> str:
        return str(_uuid.UUID(bytes=self._take(16)))

    def read_bitset(self) -> list[int]:
        """BitSet = VarInt 长度 + 若干 Long（无符号）"""
        count = self.read_varint()
        if count < 0 or count > 4096:
            raise BufferError(f"BitSet 长度非法: {count}")
        return [self.read_ulong() for _ in range(count)]

    def read_position(self) -> tuple[int, int, int]:
        """Position: x(26) | z(26) | y(12)，均为有符号"""
        val = self.read_ulong()
        x = val >> 38
        z = (val >> 12) & 0x3FFFFFF
        y = val & 0xFFF
        if x >= 1 << 25:
            x -= 1 << 26
        if y >= 1 << 11:
            y -= 1 << 12
        if z >= 1 << 25:
            z -= 1 << 26
        return x, y, z

    def read_rest(self) -> bytes:
        return self._take(self.remaining)


def read_varint_bytes(data, offset: int = 0) -> tuple[int, int]:
    """轻量级 VarInt 读取，返回 (值, 消耗字节数)。失败返回 (-1, 0)。"""
    value = 0
    for i in range(5):
        if offset + i >= len(data):
            return -1, 0
        byte = data[offset + i]
        value |= (byte & 0x7F) << (7 * i)
        if (byte & 0x80) == 0:
            return value, i + 1
    return -1, 0


def write_varint(value: int) -> bytes:
    """编码 VarInt（支持负数，按 32 位补码处理）"""
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            break
    return bytes(out)


def write_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return write_varint(len(raw)) + raw
