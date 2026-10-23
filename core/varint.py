"""
VarInt 编解码模块
Minecraft协议中使用的变长整数编码
"""

import zlib

from .logger import setup_logger

logger = setup_logger(__name__)


class VarInt:
    """VarInt 编码/解码工具类"""

    MAX_BYTES = 5

    @staticmethod
    def read(buffer: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
        """
        从字节缓冲区读取VarInt（按有符号 32 位解释）

        参数:
            buffer: 字节缓冲区（bytes 或 bytearray）
            offset: 起始偏移量

        返回:
            (value, bytes_read): 解码的值和读取的字节数
        """
        result = 0
        bytes_read = 0

        for i in range(VarInt.MAX_BYTES):
            if offset + bytes_read >= len(buffer):
                raise ValueError("缓冲区数据不足，无法完成VarInt读取")

            byte = buffer[offset + bytes_read]
            result |= (byte & 0x7F) << (7 * i)
            bytes_read += 1

            if (byte & 0x80) == 0:
                break
        else:
            raise ValueError("VarInt超过最大长度")

        # 有符号 32 位：最高位为 1 时按补码解释为负数
        if result >= 1 << 31:
            result -= 1 << 32

        return result, bytes_read

    @staticmethod
    def write(value: int) -> bytes:
        """
        将整数编码为VarInt（支持负数，按 32 位补码处理）

        参数:
            value: 要编码的整数（协议中 VarInt 为有符号 32 位）
        返回:
            编码后的字节序列
        """
        # Minecraft 的 VarInt 是有符号 32 位，负数用补码表示后按 5 字节输出
        value &= 0xFFFFFFFF
        result = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value != 0:
                byte |= 0x80
            result.append(byte)
            if value == 0:
                break
        return bytes(result)

    @staticmethod
    def size(value: int) -> int:
        """返回VarInt编码所需的字节数"""
        return len(VarInt.write(value))


class VarLong:
    """VarLong 编码/解码工具类 (用于长整数)"""

    MAX_BYTES = 10

    @staticmethod
    def read(buffer: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
        """从字节缓冲区读取VarLong（按有符号 64 位解释）"""
        result = 0
        bytes_read = 0

        for i in range(VarLong.MAX_BYTES):
            if offset + bytes_read >= len(buffer):
                raise ValueError("缓冲区数据不足，无法完成VarLong读取")

            byte = buffer[offset + bytes_read]
            result |= (byte & 0x7F) << (7 * i)
            bytes_read += 1

            if (byte & 0x80) == 0:
                break
        else:
            raise ValueError("VarLong超过最大长度")

        # 有符号 64 位：最高位为 1 时按补码解释为负数
        if result >= 1 << 63:
            result -= 1 << 64

        return result, bytes_read

    @staticmethod
    def write(value: int) -> bytes:
        """将整数编码为VarLong（支持负数，按 64 位补码处理）"""
        value &= 0xFFFFFFFFFFFFFFFF
        result = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value != 0:
                byte |= 0x80
            result.append(byte)
            if value == 0:
                break
        return bytes(result)

    @staticmethod
    def size(value: int) -> int:
        """返回VarLong编码所需的字节数"""
        return len(VarLong.write(value))


def frame_size(buffer: bytes | bytearray, offset: int = 0) -> int:
    """
    只根据外层的「数据包长度」VarInt 计算整帧字节数。

    这是字节透明转发的关键：**帧长度与是否压缩无关**，因此即使
    载荷无法解析（例如解压失败），代理依然能够正确切分并转发。
    数据不完整或长度非法时返回 0。
    """
    try:
        total_len, len_bytes = VarInt.read(buffer, offset)
    except Exception:
        return 0
    if total_len <= 0:
        return 0
    end = offset + len_bytes + total_len
    if end > len(buffer):
        return 0
    return len_bytes + total_len


def read_packet(buffer: bytes | bytearray, offset: int = 0, compression_threshold: int = -1) -> tuple[int, bytes, int]:
    """
    读取一个完整的 Minecraft 数据包，支持压缩。
    参数 buffer 可以是 bytes 或 bytearray。
    返回 (packet_id, payload_bytes, total_packet_length)
    """
    try:
        total_len, len_bytes = VarInt.read(buffer, offset)
        if total_len <= 0 or len_bytes == 0:
            return -1, b'', 0
        packet_end = offset + len_bytes + total_len
        if packet_end > len(buffer):
            return -1, b'', 0
        frame_len = len_bytes + total_len

        if compression_threshold >= 0:
            return _read_compressed_packet(buffer, offset, len_bytes, total_len, packet_end)

        packet_id, id_bytes = VarInt.read(buffer, offset + len_bytes)
        payload = buffer[offset + len_bytes + id_bytes:packet_end]
        if isinstance(payload, bytearray):
            payload = bytes(payload)

        if packet_id > 0xFF or packet_id < 0:
            logger.debug(f"packet_id={packet_id} 异常，尝试压缩解析")
            return _read_compressed_packet(buffer, offset, len_bytes, total_len, packet_end)

        return packet_id, payload, frame_len

    except Exception as e:
        logger.debug(f"read_packet 异常: {e}")
        return -1, b'', 0


def _read_compressed_packet(
    buffer: bytes | bytearray,
    offset: int,
    len_bytes: int,
    total_len: int,
    packet_end: int
) -> tuple[int, bytes, int]:
    """
    辅助函数：读取压缩数据包。

    返回的第三个值始终是 **整帧长度**（len_bytes + total_len），
    与是否成功解压无关 —— 代理据此切分并原样转发字节。
    """
    frame_len = len_bytes + total_len

    # 解析失败时用哨兵 ID（-2）表示「帧有效但载荷不可解析」，
    # 以便调用方仍能按 frame_len 转发。
    data_len, data_len_bytes = VarInt.read(buffer, offset + len_bytes)
    data_start = offset + len_bytes + data_len_bytes

    if data_start > packet_end:
        return -2, b'', frame_len

    if data_len == 0:
        # 未压缩：包体直接位于 data_start..packet_end
        try:
            packet_id, id_bytes = VarInt.read(buffer, data_start)
        except Exception:
            return -2, b'', frame_len
        payload = buffer[data_start + id_bytes:packet_end]
        if isinstance(payload, bytearray):
            payload = bytes(payload)
    else:
        # 已压缩：data_len 是「解压后」的长度，压缩数据的实际长度由
        # 外层帧长度界定（packet_end），**不能**用 data_len 去比较缓冲区。
        compressed_data = bytes(buffer[data_start:packet_end])
        try:
            decompressed = zlib.decompress(compressed_data)
        except zlib.error as e:
            logger.debug(f"zlib 解压失败: {e}, data_len={data_len}")
            return -2, b'', frame_len
        try:
            packet_id, id_bytes = VarInt.read(decompressed, 0)
        except Exception:
            return -2, b'', frame_len
        payload = decompressed[id_bytes:]

    return packet_id, payload, frame_len


def build_packet(packet_id: int, payload: bytes) -> bytes:
    """
    构建一个Minecraft数据包

    参数:
        packet_id: 数据包ID
        payload: 载荷数据

    返回:
        完整的包字节序列
    """
    id_bytes = VarInt.write(packet_id)
    length = len(id_bytes) + len(payload)
    return VarInt.write(length) + id_bytes + payload
