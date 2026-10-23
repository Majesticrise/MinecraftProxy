"""
区块提取模块

从拦截的数据包中提取区块数据，转换为 Anvil 格式并写入 .mca 区域文件。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from .logger import setup_logger

logger = setup_logger(__name__)

SECTOR_SIZE = 4096
HEADER_SIZE = SECTOR_SIZE * 2  # 位置表 + 时间戳表
SECTORS_PER_CHUNK_MAX = 255

# 去重集合的内存上限：超出后按 LRU 淘汰，避免长时间挂机内存无限增长
MAX_TRACKED_CHUNKS = 200_000

# 维度名 -> 目录名。未知维度（模组）会被安全地映射到 dimension_<n>
KNOWN_DIMENSION_DIRS: dict[str, str] = {
    "minecraft:overworld": "",
    "minecraft:the_nether": "DIM-1",
    "minecraft:the_end": "DIM1",
}

# 全局共享的写入锁：多个客户端会话必须串行写同一个区域文件
_GLOBAL_REGION_LOCK: asyncio.Lock | None = None
_GLOBAL_LOCK_LOOP: Any = None


def _get_global_lock() -> asyncio.Lock:
    """
    返回进程级共享锁。

    注意：asyncio.Lock 绑定事件循环，因此若事件循环变化（例如测试里反复
    `asyncio.run`）需要重建，否则会抛 "attached to a different loop"。
    """
    global _GLOBAL_REGION_LOCK, _GLOBAL_LOCK_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _GLOBAL_REGION_LOCK is None or _GLOBAL_LOCK_LOOP is not loop:
        _GLOBAL_REGION_LOCK = asyncio.Lock()
        _GLOBAL_LOCK_LOOP = loop
    return _GLOBAL_REGION_LOCK


def dimension_dir_name(dimension: Any) -> str:
    """
    把维度标识转换为目录名。

    支持三种输入：
      * 维度名（"minecraft:the_nether"）
      * 已知维度 ID（-1 / 0 / 1）
      * 未知维度 ID（任意整数）-> "dimension_<id>"
    """
    if isinstance(dimension, str):
        if dimension in KNOWN_DIMENSION_DIRS:
            return KNOWN_DIMENSION_DIRS[dimension]
        # 未知命名空间：用合法目录名保存
        safe = dimension.replace(":", "_").replace("/", "_")
        return f"dimension_{safe}"
    if dimension == -1:
        return "DIM-1"
    if dimension == 1:
        return "DIM1"
    if dimension == 0:
        return ""
    return f"dimension_{dimension}"


@dataclass
class RegionStats:
    chunks_written: int = 0
    bytes_written: int = 0
    sectors_reused: int = 0
    external_chunks: int = 0


class ExtractorStats(TypedDict):
    """区块提取统计。

    使用 TypedDict 而非裸 dict：既保留 `stats["chunks_saved"]` 的字典访问方式，
    又让类型检查器知道每个键的确切类型（裸 dict 会退化成 object）。
    """

    chunks_saved: int
    chunks_failed: int
    dimensions: set[str]


class AnvilWriter:
    """
    Anvil (.mca) 区域文件写入器

    文件结构:
      0x0000 - 0x0FFF  位置表 (1024 项 x 4 字节)
      0x1000 - 0x1FFF  时间戳表 (1024 项 x 4 字节)
      0x2000 - ...     区块数据（以 4KB 扇区为单位）

    位置表项: 高 3 字节 = 起始扇区号, 低 1 字节 = 占用扇区数
    区块数据: [压缩后长度 4 字节大端][压缩类型 1 字节][压缩数据]
    """

    COMPRESSION_ZLIB = 2

    def __init__(self, world_dir: str):
        self.world_dir = Path(world_dir)
        # 使用进程级共享锁：同一区域文件可能被多个会话并发写入
        self.lock = _get_global_lock()
        self.stats = RegionStats()

    # ------------------------------------------------------------------ 路径
    def _get_region_dir(self, dimension: Any) -> Path:
        return self.world_dir / dimension_dir_name(dimension) / "region"

    def _get_region_path(self, chunk_x: int, chunk_z: int, dimension: Any) -> Path:
        region_dir = self._get_region_dir(dimension)
        region_dir.mkdir(parents=True, exist_ok=True)
        return region_dir / f"r.{chunk_x >> 5}.{chunk_z >> 5}.mca"

    # ------------------------------------------------------------ 扇区数据
    @staticmethod
    def _build_sector_data(compressed: bytes) -> bytes:
        """
        构建一个扇区对齐的区块记录。

        ⚠️ 字段顺序与长度语义**必须**严格遵守官方规范：
          https://minecraft.wiki/w/Region_file_format
          "Chunk data begins with a (big-endian) four-byte signed length field
           that indicates the exact length of the remaining chunk data in bytes.
           The following byte indicates the compression scheme used for chunk
           data, and the remaining (length-1) bytes are the compressed data."

        因此：
          * 字节顺序 = `[长度 4B 大端][压缩类型 1B][压缩数据]`
          * **长度 = 1（压缩类型字节）+ len(压缩数据)**，即「剩余全部字节数」，
            而不是单纯的压缩数据长度。
          * 末尾的扇区填充**不计入**长度字段。

        早期版本有两处错误（顺序对调 + 长度少算 1），导致所有标准读取方
        （游戏本体、C2ME、MCEdit 等）解析失败，报
        `stream is truncated: expected 33554460 but read 8187`，
        世界**完全无法加载**（只有本程序自己的解析器能读）。
        """
        length = 1 + len(compressed)          # 1 = 压缩类型字节
        body = struct.pack(">I", length)
        body += struct.pack(">B", AnvilWriter.COMPRESSION_ZLIB)
        body += compressed
        padding = (-len(body)) % SECTOR_SIZE
        if padding:
            body += b"\x00" * padding
        return body

    # -------------------------------------------------------------- 文件头
    @staticmethod
    def _read_header(file_path: Path) -> tuple[bytearray, bytearray]:
        locations = bytearray(SECTOR_SIZE)
        timestamps = bytearray(SECTOR_SIZE)
        if file_path.exists():
            with open(file_path, "rb") as fh:
                raw = fh.read(HEADER_SIZE)
            if len(raw) >= SECTOR_SIZE:
                locations = bytearray(raw[:SECTOR_SIZE])
            if len(raw) >= HEADER_SIZE:
                timestamps = bytearray(raw[SECTOR_SIZE:HEADER_SIZE])
        return locations, timestamps

    @staticmethod
    def _collect_used_sectors(locations: bytearray, file_sectors: int) -> list[bool]:
        used = [False] * max(file_sectors, 2)
        for index in range(1024):
            entry = struct.unpack_from(">I", locations, index * 4)[0]
            if entry == 0:
                continue
            offset = entry >> 8
            count = entry & 0xFF
            if offset < 2:
                continue
            for sector in range(offset, offset + count):
                if sector < len(used):
                    used[sector] = True
        return used

    @classmethod
    def _find_free_run(cls, locations: bytearray, file_sectors: int, needed: int) -> int:
        used = cls._collect_used_sectors(locations, file_sectors)
        run_start = -1
        run_len = 0
        for sector in range(2, len(used) + needed + 1):
            is_used = sector < len(used) and used[sector]
            if not is_used:
                if run_len == 0:
                    run_start = sector
                run_len += 1
                if run_len >= needed:
                    return run_start
            else:
                run_len = 0
        return len(used)

    @staticmethod
    def _set_header_entry(
        locations: bytearray,
        timestamps: bytearray,
        local_x: int,
        local_z: int,
        sector_offset: int,
        sector_count: int,
        timestamp: int,
    ) -> None:
        index = (local_x & 31) + (local_z & 31) * 32
        value = ((sector_offset & 0xFFFFFF) << 8) | (sector_count & 0xFF)
        struct.pack_into(">I", locations, index * 4, value)
        struct.pack_into(">I", timestamps, index * 4, timestamp & 0xFFFFFFFF)

    # ------------------------------------------------------------------ 写入
    async def save_chunk(
        self,
        chunk_x: int,
        chunk_z: int,
        compressed_nbt: bytes,
        dimension: Any = 0,
    ) -> bool:
        """把（已 zlib 压缩的）区块 NBT 写入区域文件"""
        if not compressed_nbt:
            return False

        sector_data = self._build_sector_data(compressed_nbt)
        sector_count = len(sector_data) // SECTOR_SIZE
        if sector_count == 0:
            logger.warning(f"区块 ({chunk_x}, {chunk_z}) 数据为空")
            return False

        # 超过 255 扇区（约 1020 KiB）时必须外置为 c.x.z.mcc
        if sector_count > SECTORS_PER_CHUNK_MAX:
            return await self._save_external_chunk(
                chunk_x, chunk_z, compressed_nbt, dimension
            )

        region_file = self._get_region_path(chunk_x, chunk_z, dimension)
        local_x = chunk_x & 31
        local_z = chunk_z & 31

        async with self.lock:
            try:
                # 磁盘 I/O 是阻塞的：放到线程里执行，避免拖住整个事件循环
                ok = await asyncio.to_thread(
                    self._write_chunk_sync,
                    region_file, local_x, local_z,
                    sector_data, sector_count, chunk_x, chunk_z, dimension,
                )
            except Exception as exc:
                logger.error(f"保存区块 ({chunk_x}, {chunk_z}) 失败: {exc}", exc_info=True)
                return False

        if ok:
            logger.debug(
                f"区块 ({chunk_x}, {chunk_z}) 已写入 {region_file.name} "
                f"[维度 {dimension}, 扇区 {sector_count}]"
            )
        return ok

    def _write_chunk_sync(
        self,
        region_file: Path,
        local_x: int,
        local_z: int,
        sector_data: bytes,
        sector_count: int,
        chunk_x: int,
        chunk_z: int,
        dimension: Any,
    ) -> bool:
        """同步写入主体（由 save_chunk 通过线程调用，锁在调用方持有）"""
        locations, timestamps = self._read_header(region_file)
        index = (local_x & 31) + (local_z & 31) * 32

        if region_file.exists():
            file_sectors = (region_file.stat().st_size + SECTOR_SIZE - 1) // SECTOR_SIZE
        else:
            file_sectors = 0

        existing = struct.unpack_from(">I", locations, index * 4)[0]
        if existing != 0:
            existing_offset = existing >> 8
            existing_count = existing & 0xFF
            if existing_count >= sector_count and existing_offset >= 2:
                sector_offset = existing_offset
                self.stats.sectors_reused += 1
            else:
                sector_offset = self._find_free_run(locations, file_sectors, sector_count)
        else:
            sector_offset = self._find_free_run(locations, file_sectors, sector_count)

        timestamp = int(time.time())
        self._set_header_entry(
            locations, timestamps, local_x, local_z,
            sector_offset, sector_count, timestamp,
        )

        mode = "r+b" if region_file.exists() else "wb"
        with open(region_file, mode) as fh:
            needed_size = (sector_offset + sector_count) * SECTOR_SIZE
            fh.seek(0, os.SEEK_END)
            if fh.tell() < needed_size:
                fh.write(b"\x00" * (needed_size - fh.tell()))
            # ① 先写数据扇区并落盘
            fh.seek(sector_offset * SECTOR_SIZE)
            fh.write(sector_data)
            fh.flush()
            os.fsync(fh.fileno())
            # ② 再更新位置表/时间戳表并落盘
            #    （顺序很重要：崩溃时头部绝不会指向尚未写入的数据）
            fh.seek(0)
            fh.write(locations)
            fh.write(timestamps)
            fh.flush()
            os.fsync(fh.fileno())

        self.stats.chunks_written += 1
        self.stats.bytes_written += len(sector_data)
        return True

    async def _save_external_chunk(
        self,
        chunk_x: int,
        chunk_z: int,
        compressed_nbt: bytes,
        dimension: Any,
    ) -> bool:
        """
        超大区块外置存储。

        官方规定：当压缩数据超过 255 扇区时，把数据写入 `c.<x>.<z>.mcc`，
        并在区域文件的位置表里写入「压缩类型 | 128」和 length = 1。
        """
        region_dir = self._get_region_dir(dimension)
        external = region_dir / f"c.{chunk_x}.{chunk_z}.mcc"
        region_file = self._get_region_path(chunk_x, chunk_z, dimension)
        local_x = chunk_x & 31
        local_z = chunk_z & 31
        index = local_x + local_z * 32

        async with self.lock:
            try:
                ok = await asyncio.to_thread(
                    self._save_external_chunk_sync,
                    region_dir, external, region_file, index, compressed_nbt,
                )
            except Exception as exc:
                logger.error(f"外置区块 ({chunk_x}, {chunk_z}) 失败: {exc}")
                return False

        if ok:
            logger.debug(
                f"区块 ({chunk_x}, {chunk_z}) 超过扇区上限，已外置为 {external.name}"
            )
        return ok

    def _save_external_chunk_sync(
        self,
        region_dir: Path,
        external: Path,
        region_file: Path,
        index: int,
        compressed_nbt: bytes,
    ) -> bool:
        """
        外置区块同步写入（由 _save_external_chunk 通过线程调用）。

        ⚠️ 官方规范（https://minecraft.wiki/w/Region_file_format）：
          "If the compressed chunk data would exceed 1020 KiB (255 sectors),
           the compression scheme's value is increased by 128, the length is
           set to 1, and the compressed chunk data is instead saved in a file
           called c.<x>.<z>.mcc."

        所以外置标记在区域文件里是**两个字节的负载**：
          `[长度=1 (4B 大端)][压缩类型 + 128 (1B)]`
        早期实现只写 `(1<<8)|1`（把长度写进了位置表高位），
        既没写压缩类型 +128，也没有实际的数据头，标准读取方无法识别。
        """
        region_dir.mkdir(parents=True, exist_ok=True)
        # ① 先写外置数据文件（内容 = 压缩后的 NBT，无额外头）并落盘
        with open(external, "wb") as fh:
            fh.write(compressed_nbt)
            fh.flush()
            os.fsync(fh.fileno())

        # ② 区域文件里写「外置标记」记录：长度 1 + 压缩类型 +128
        marker = struct.pack(">I", 1)
        marker += struct.pack(">B", self.COMPRESSION_ZLIB + 128)
        sector_data = marker + b"\x00" * (SECTOR_SIZE - len(marker))

        locations, timestamps = self._read_header(region_file)
        if region_file.exists():
            file_sectors = (region_file.stat().st_size + SECTOR_SIZE - 1) // SECTOR_SIZE
        else:
            file_sectors = 0
        sector_offset = self._find_free_run(locations, file_sectors, 1)
        self._set_header_entry(
            locations, timestamps,
            index % 32, index // 32,
            sector_offset, 1, int(time.time()),
        )

        mode = "r+b" if region_file.exists() else "wb"
        with open(region_file, mode) as fh:
            needed_size = (sector_offset + 1) * SECTOR_SIZE
            fh.seek(0, os.SEEK_END)
            if fh.tell() < needed_size:
                fh.write(b"\x00" * (needed_size - fh.tell()))
            fh.seek(sector_offset * SECTOR_SIZE)
            fh.write(sector_data)
            fh.flush()
            os.fsync(fh.fileno())
            fh.seek(0)
            fh.write(locations)
            fh.write(timestamps)
            fh.flush()
            os.fsync(fh.fileno())

        self.stats.external_chunks += 1
        return True


class ChunkExtractor:
    """区块提取器：把拦截到的区块写入 .mca 文件"""

    def __init__(self, world_dir: str, debug: bool = False):
        self.world_dir = Path(world_dir)
        self.debug = debug
        self.anvil_writer = AnvilWriter(world_dir)
        self.stats: ExtractorStats = {
            "chunks_saved": 0,
            "chunks_failed": 0,
            "dimensions": set(),
        }
        self._saved_keys: set[tuple[Any, int, int]] = set()
        # 去重键的插入顺序（用于 LRU 淘汰，防止内存无限增长）
        self._saved_order: deque[tuple[Any, int, int]] = deque()
        # 正在进行的写入任务（会话结束前需要 await，防止丢数据）
        self._pending_writes: set[asyncio.Task] = set()
        # 写入区块 NBT 的 DataVersion（影响游戏如何做数据升级）
        self.data_version: int = 3955

    # ------------------------------------------------------------------ 保存
    async def save_chunk(self, chunk_data, dimension: Any = 0) -> bool:
        """保存区块。

        chunk_data 支持:
          * 带 .x/.z/.data 属性的对象（protocol.ChunkData）
          * (x, z, compressed_nbt) 元组

        dimension 可以是维度名（"minecraft:the_nether"）或维度 ID。
        """
        if hasattr(chunk_data, "x") and hasattr(chunk_data, "z"):
            chunk_x = int(chunk_data.x)
            chunk_z = int(chunk_data.z)
            nbt_data = chunk_data.data
            block_entities = list(getattr(chunk_data, "block_entities", []) or [])
        elif isinstance(chunk_data, tuple) and len(chunk_data) == 3:
            chunk_x, chunk_z, nbt_data = chunk_data
            block_entities = []
        else:
            logger.error(f"无效的区块数据格式: {type(chunk_data)}")
            self.stats["chunks_failed"] += 1
            return False

        key = (dimension, chunk_x, chunk_z)
        if key in self._saved_keys:
            return True
        if not nbt_data:
            self.stats["chunks_failed"] += 1
            return False

        success = await self.anvil_writer.save_chunk(chunk_x, chunk_z, nbt_data, dimension)
        if not success:
            # 不缓存失败键：否则重发的同一区块会被永久跳过
            self.stats["chunks_failed"] += 1
            return False

        self._track_key(key)
        self.stats["chunks_saved"] += 1
        self.stats["dimensions"].add(str(dimension))
        if block_entities:
            await self._save_block_entities(chunk_x, chunk_z, block_entities, dimension)

        if self.stats["chunks_saved"] % 50 == 0:
            logger.info(
                f"进度: 已保存 {self.stats['chunks_saved']} 个区块 "
                f"(失败 {self.stats['chunks_failed']})"
            )
        return True

    # ------------------------------------------------------------ 去重/生命周期
    def _track_key(self, key: tuple[Any, int, int]) -> None:
        """记录已保存键，超出上限时按 FIFO 淘汰最旧的（防止内存无限增长）"""
        self._saved_keys.add(key)
        self._saved_order.append(key)
        while len(self._saved_order) > MAX_TRACKED_CHUNKS:
            oldest = self._saved_order.popleft()
            self._saved_keys.discard(oldest)

    def track_write(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """登记一个写入协程，供会话结束前统一 await（防止任务被丢弃）"""
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)
        return task

    async def drain(self) -> None:
        """等待所有已登记的写入任务完成（会话结束前必须调用，否则可能丢数据）"""
        while self._pending_writes:
            pending = list(self._pending_writes)
            logger.debug(f"等待 {len(pending)} 个未完成的区块写入任务...")
            results = await asyncio.gather(*pending, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.debug(f"写入任务异常: {result}")

    # ------------------------------------------------------------ 方块实体
    async def _save_block_entities(
        self,
        chunk_x: int,
        chunk_z: int,
        block_entities: list[dict[str, Any]],
        dimension: Any = 0,
    ) -> None:
        region_dir = self.anvil_writer._get_region_dir(dimension)
        file_path = region_dir / f"chunk_{chunk_x}_{chunk_z}_block_entities.json"

        try:
            payload = self._serialize_block_entities(
                chunk_x, chunk_z, block_entities, dimension
            )
            # 磁盘 I/O 移出事件循环
            await asyncio.to_thread(self._write_json, file_path, payload)
            logger.debug(f"方块实体数据已保存: {file_path.name}")
        except Exception as exc:
            logger.error(f"保存方块实体数据失败: {exc}", exc_info=True)

    @staticmethod
    def _serialize_block_entities(
        chunk_x: int,
        chunk_z: int,
        block_entities: list[dict[str, Any]],
        dimension: Any,
    ) -> dict[str, Any]:
        serialized = []
        for entity in block_entities:
            entry = {
                "x": entity.get("x"),
                "y": entity.get("y"),
                "z": entity.get("z"),
                "type_id": entity.get("type_id"),
            }
            nbt_bytes = entity.get("nbt_bytes") or b""
            if nbt_bytes:
                entry["nbt_base64"] = base64.b64encode(nbt_bytes).decode("ascii")
            nbt_value = entity.get("nbt")
            if isinstance(nbt_value, dict):
                entry["id"] = str(nbt_value.get("id", ""))
            serialized.append(entry)

        return {
            "chunk_x": chunk_x,
            "chunk_z": chunk_z,
            "dimension": dimension,
            "block_entities": serialized,
        }

    @staticmethod
    def _write_json(file_path: Path, payload: dict[str, Any]) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------------- 关闭
    async def shutdown(self) -> None:
        # 先把所有未完成的写入任务跑完，否则会丢数据
        await self.drain()

        saved = self.stats["chunks_saved"]
        failed = self.stats["chunks_failed"]
        dims = len(self.stats["dimensions"])
        external = self.anvil_writer.stats.external_chunks
        logger.info(
            f"区块统计: 成功={saved}, 失败={failed}, 维度={dims}"
            + (f", 外置={external}" if external else "")
        )

        # 生成 level.dat，让输出目录成为可加载的世界
        if saved > 0:
            from .level_dat import ensure_world_metadata
            ensure_world_metadata(
                str(self.world_dir),
                world_name=self.world_dir.name or "downloaded_world",
                data_version=self.data_version,
            )
        logger.info("区块提取器已关闭")
