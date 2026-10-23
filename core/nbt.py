"""
NBT（Named Binary Tag）解析与序列化

用于：
  * 跳过 / 校验网络包中的 NBT（方块实体数据、Registry Data 等）
  * 构建 Anvil 区域文件所需的区块 NBT

类型映射（Python 对象 <-> NBT 标签）:
  Byte/Short/Int/Long/Float/Double -> 对应的 int/float 子类
  String            -> String(str 子类)
  ByteArray         -> ByteArray(bytes 子类)
  IntArray/LongArray-> IntArray/LongArray(list 子类)
  List              -> List(element_type, values)
  Compound          -> 普通 dict
"""

from __future__ import annotations

import struct
from typing import Any

TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12


class Byte(int):
    pass


class Short(int):
    pass


class Int(int):
    pass


class Long(int):
    pass


class Float(float):
    pass


class Double(float):
    pass


class String(str):
    pass


class ByteArray(bytes):
    pass


class IntArray(list):
    pass


class LongArray(list):
    pass


class List(list):
    """NBT 列表，携带元素类型"""

    def __init__(self, element_type=TAG_END, values=()):
        super().__init__(values)
        self.element_type = element_type


_TAG_IDS = {
    Byte: TAG_BYTE,
    Short: TAG_SHORT,
    Int: TAG_INT,
    Long: TAG_LONG,
    Float: TAG_FLOAT,
    Double: TAG_DOUBLE,
    ByteArray: TAG_BYTE_ARRAY,
    String: TAG_STRING,
    List: TAG_LIST,
    IntArray: TAG_INT_ARRAY,
    LongArray: TAG_LONG_ARRAY,
    dict: TAG_COMPOUND,
}

_TAG_CLASSES = {
    TAG_BYTE: Byte,
    TAG_SHORT: Short,
    TAG_INT: Int,
    TAG_LONG: Long,
    TAG_FLOAT: Float,
    TAG_DOUBLE: Double,
    TAG_BYTE_ARRAY: ByteArray,
    TAG_STRING: String,
    TAG_INT_ARRAY: IntArray,
    TAG_LONG_ARRAY: LongArray,
    TAG_COMPOUND: dict,
}


class NbtError(Exception):
    pass


# --------------------------------------------------------------------- 读取
def _read_string(data, offset: int) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise NbtError("字符串长度字段越界")
    length = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    if offset + length > len(data):
        raise NbtError("字符串内容越界")
    text = data[offset:offset + length].decode("utf-8", errors="replace")
    return String(text), offset + length


def _need(data, offset: int, size: int) -> None:
    if offset + size > len(data):
        raise NbtError(f"NBT 数据不足: 需要 {size} 字节 @ {offset}")


def _parse_payload(data, offset: int, tag_type: int) -> tuple[Any, int]:
    if tag_type == TAG_BYTE:
        _need(data, offset, 1)
        return Byte(struct.unpack_from(">b", data, offset)[0]), offset + 1
    if tag_type == TAG_SHORT:
        _need(data, offset, 2)
        return Short(struct.unpack_from(">h", data, offset)[0]), offset + 2
    if tag_type == TAG_INT:
        _need(data, offset, 4)
        return Int(struct.unpack_from(">i", data, offset)[0]), offset + 4
    if tag_type == TAG_LONG:
        _need(data, offset, 8)
        return Long(struct.unpack_from(">q", data, offset)[0]), offset + 8
    if tag_type == TAG_FLOAT:
        _need(data, offset, 4)
        return Float(struct.unpack_from(">f", data, offset)[0]), offset + 4
    if tag_type == TAG_DOUBLE:
        _need(data, offset, 8)
        return Double(struct.unpack_from(">d", data, offset)[0]), offset + 8
    if tag_type == TAG_BYTE_ARRAY:
        _need(data, offset, 4)
        length = struct.unpack_from(">i", data, offset)[0]
        offset += 4
        if length < 0:
            raise NbtError("ByteArray 长度为负")
        _need(data, offset, length)
        return ByteArray(bytes(data[offset:offset + length])), offset + length
    if tag_type == TAG_STRING:
        return _read_string(data, offset)
    if tag_type == TAG_LIST:
        _need(data, offset, 5)
        element_type = data[offset]
        length = struct.unpack_from(">i", data, offset + 1)[0]
        offset += 5
        if length < 0 or length > 16_777_216:
            raise NbtError(f"List 长度非法: {length}")
        values = []
        for _ in range(length):
            value, offset = _parse_payload(data, offset, element_type)
            values.append(value)
        return List(element_type, values), offset
    if tag_type == TAG_COMPOUND:
        result: dict[str, Any] = {}
        while True:
            if offset >= len(data):
                raise NbtError("Compound 未以 TAG_End 结束")
            inner_type = data[offset]
            offset += 1
            if inner_type == TAG_END:
                break
            name, offset = _read_string(data, offset)
            value, offset = _parse_payload(data, offset, inner_type)
            result[str(name)] = value
        return result, offset
    if tag_type == TAG_INT_ARRAY:
        _need(data, offset, 4)
        length = struct.unpack_from(">i", data, offset)[0]
        offset += 4
        if length < 0:
            raise NbtError("IntArray 长度为负")
        _need(data, offset, length * 4)
        values = list(struct.unpack_from(f">{length}i", data, offset)) if length else []
        return IntArray(values), offset + length * 4
    if tag_type == TAG_LONG_ARRAY:
        _need(data, offset, 4)
        length = struct.unpack_from(">i", data, offset)[0]
        offset += 4
        if length < 0:
            raise NbtError("LongArray 长度为负")
        _need(data, offset, length * 8)
        values = list(struct.unpack_from(f">{length}q", data, offset)) if length else []
        return LongArray(values), offset + length * 8
    raise NbtError(f"未知 NBT 标签类型: {tag_type}")


def parse_named_tag(data, offset: int = 0) -> tuple[Any, int]:
    """解析一个带名字的标签，返回 (值, 新偏移)。TAG_End 返回 (None, offset)。"""
    if offset >= len(data):
        raise NbtError("NBT 数据为空")
    tag_type = data[offset]
    offset += 1
    if tag_type == TAG_END:
        return None, offset
    _name, offset = _read_string(data, offset)
    return _parse_payload(data, offset, tag_type)


def parse_anonymous_tag(data, offset: int = 0) -> tuple[Any, int]:
    """
    解析一个**匿名**标签（只有 type + payload，没有名字）。

    网络协议中「NBT」类型（如区块内的方块实体、以及部分可选字段）使用
    匿名编码；只有磁盘 Anvil 格式才带名字。TAG_End 返回 (None, offset)。
    """
    if offset >= len(data):
        raise NbtError("NBT 数据为空")
    tag_type = data[offset]
    offset += 1
    if tag_type == TAG_END:
        return None, offset
    return _parse_payload(data, offset, tag_type)


def nbt_size(data, offset: int = 0) -> int:
    """返回该 NBT 标签占用的字节数"""
    _, end = parse_named_tag(data, offset)
    return end - offset


def try_parse_named_tag(data, offset: int = 0):
    """容错版本：失败返回 (None, offset)"""
    try:
        return parse_named_tag(data, offset)
    except Exception:
        return None, offset


# ------------------------------------------------------------------- 序列化
def _type_id(value) -> int:
    tag_id = _TAG_IDS.get(type(value))
    if tag_id is not None:
        return tag_id
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, int):
        return TAG_INT
    if isinstance(value, float):
        return TAG_DOUBLE
    if isinstance(value, (bytes, bytearray)):
        return TAG_BYTE_ARRAY
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, (list, tuple)):
        return TAG_LIST
    raise NbtError(f"无法序列化的 NBT 值类型: {type(value)}")


def _write_string(out: bytearray, text: str) -> None:
    raw = text.encode("utf-8")
    out += struct.pack(">H", len(raw))
    out += raw


def _write_payload(out: bytearray, value, tag_id: int) -> None:
    if tag_id == TAG_BYTE:
        out += struct.pack(">b", int(value))
    elif tag_id == TAG_SHORT:
        out += struct.pack(">h", int(value))
    elif tag_id == TAG_INT:
        out += struct.pack(">i", int(value))
    elif tag_id == TAG_LONG:
        out += struct.pack(">q", int(value))
    elif tag_id == TAG_FLOAT:
        out += struct.pack(">f", float(value))
    elif tag_id == TAG_DOUBLE:
        out += struct.pack(">d", float(value))
    elif tag_id == TAG_BYTE_ARRAY:
        raw = bytes(value)
        out += struct.pack(">i", len(raw))
        out += raw
    elif tag_id == TAG_STRING:
        _write_string(out, str(value))
    elif tag_id == TAG_LIST:
        element_type = getattr(value, "element_type", TAG_END)
        if element_type == TAG_END and len(value):
            element_type = _type_id(value[0])
        out.append(element_type)
        out += struct.pack(">i", len(value))
        for item in value:
            _write_payload(out, item, element_type)
    elif tag_id == TAG_COMPOUND:
        for key, item in value.items():
            item_id = _type_id(item)
            out.append(item_id)
            _write_string(out, key)
            _write_payload(out, item, item_id)
        out.append(TAG_END)
    elif tag_id == TAG_INT_ARRAY:
        out += struct.pack(">i", len(value))
        for item in value:
            out += struct.pack(">i", int(item))
    elif tag_id == TAG_LONG_ARRAY:
        out += struct.pack(">i", len(value))
        for item in value:
            out += struct.pack(">q", int(item))
    else:
        raise NbtError(f"未知 NBT 标签类型: {tag_id}")


def serialize_named_tag(name: str, value) -> bytes:
    out = bytearray()
    tag_id = _type_id(value)
    out.append(tag_id)
    _write_string(out, name)
    _write_payload(out, value, tag_id)
    return bytes(out)


def serialize_anonymous_tag(value) -> bytes:
    """序列化一个**匿名**标签（只有 type + payload，无名字）"""
    out = bytearray()
    tag_id = _type_id(value)
    out.append(tag_id)
    _write_payload(out, value, tag_id)
    return bytes(out)


def build_root(entries: dict[str, Any], name: str = "") -> bytes:
    """构建一个根 Compound 标签（Anvil 区域文件使用空名字）"""
    return serialize_named_tag(name, dict(entries))


def compound(entries: dict[str, Any]) -> dict[str, Any]:
    return dict(entries)


def list_of(element_type: int, values=()) -> List:
    return List(element_type, values)
