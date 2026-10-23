"""
方块注册表：全局方块状态 ID → 方块名称 + 属性

为什么需要它
------------
区块数据包里的调色板存的是「全局方块状态 ID」（整数），而 Anvil 的
`.mca` 文件要求调色板是 **方块名称**（`Name`/`Properties` 复合标签，
或字符串列表）。两者之间必须有一次映射，否则导出的世界无法被加载。

映射数据无法从网络获得：`minecraft:block` 属于**硬编码注册表**，
服务端不会通过 `Registry Data` 包下发。因此本模块支持三种来源：

  1. `--block-map <file>` 显式指定
  2. 本地缓存 `<data-dir>/blocks_<protocol>.json`
  3. 内置 `data/blocks_<protocol>.json`
  4. （可选）从 PrismarineJS/minecraft-data 自动下载并缓存

数据格式
--------
* **PrismarineJS**（推荐）：`[{name, minStateId, maxStateId, defaultState, states:[{name,type,num_values,values}]}]`
* **原版数据生成器**：`java -DbundlerMainClass=net.minecraft.data.Main -jar server.jar --reports`
  产生的 `reports/blocks.json`：`{"minecraft:stone": {"properties": {...}, "states": [{"id":0,"default":true,"properties":{}}]}}`

参考: https://minecraft.wiki/w/Java_Edition_protocol/Packets#Other_definitions
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from .logger import setup_logger

logger = setup_logger(__name__)

# PrismarineJS 仓库中协议版本 -> 版本目录名
# 只列出我们确切知道的；未知版本会依次尝试候选名（见 _candidate_version_dirs）
# 目录名已逐一核实存在（部分版本在 PrismarineJS 中没有独立目录，
# 例如 1.21.7 / 1.21.10 就没有，因此用同协议号的相邻目录）
PRISMARINE_VERSION_DIRS: dict[int, str] = {
    47: "1.18",      # 1.8 仅需「名称表」；与写入的 DataVersion 2975 保持一致
    754: "1.16.2",    # 1.16.5 与 1.16.2 数据兼容（PrismarineJS 无 1.16.5 目录）
    758: "1.18",      # 1.18.2 与 1.18 数据兼容
    763: "1.20",
    764: "1.20.2",
    765: "1.20.4",
    766: "1.20.5",    # PrismarineJS 无 1.20.6 目录，1.20.5 数据兼容
    767: "1.21.1",
    768: "1.21.3",
    769: "1.21.4",
    770: "1.21.5",
    771: "1.21.6",
    772: "1.21.8",    # 1.21.7 与 1.21.8 同协议号
    773: "1.21.9",    # 1.21.9 与 1.21.10 同协议号
    774: "1.21.11",
    777: "26.1",     # PrismarineJS 尚无 26.3 目录，26.1 为最新可用数据
}

_DOWNLOAD_BASE = (
    "https://raw.githubusercontent.com/PrismarineJS/minecraft-data/master/data/pc/{ver}/blocks.json"
)

# 已知的 DataVersion（写入 level.dat 与区块 NBT 用）
# 选一个支持经典 Name/Properties 调色板格式的版本，让游戏自行 DataFixUp
DEFAULT_DATA_VERSION = 3955  # 1.21.1
# 协议版本 -> DataVersion。
#
# 数据来源: https://minecraft.wiki/w/Data_version（每个版本取该协议号对应的
# 正式版，不含预发布/快照）。同一协议号对应多个游戏版本时取较新的那个，
# 因为它的 DataVersion 更高、DataFixUp 路径更短。
#
# 缺失的版本回退到 DEFAULT_DATA_VERSION：Minecraft 原生支持从旧版本升级世界，
# 写入偏低的 DataVersion 是安全的；写错的高值才会导致世界无法加载。
PROTOCOL_DATA_VERSION: dict[int, int] = {
    47: 2975,    # 1.8.9（归一化为现代格式后写入）
    754: 2975,   # 1.16.5（同上）
    758: 2975,   # 1.18.2
    763: 3465,   # 1.20 / 1.20.1
    764: 3578,   # 1.20.2
    765: 3700,   # 1.20.3 / 1.20.4
    766: 3839,   # 1.20.5 / 1.20.6
    767: 3955,   # 1.21 / 1.21.1
    768: 4082,   # 1.21.2 / 1.21.3
    769: 4189,   # 1.21.4
    770: 4325,   # 1.21.5
    771: 4435,   # 1.21.6
    772: 4440,   # 1.21.7 / 1.21.8
    773: 4556,   # 1.21.9 / 1.21.10
    774: 4671,   # 1.21.11
    777: 5023,   # 26.3
}

# DataVersion -> 该版本的显示名（写入 level.dat 的 Version.Name）
DATA_VERSION_NAMES: dict[int, str] = {
    340: "1.8.9",
    2586: "1.16.5",
    2975: "1.18.2",
    3465: "1.20.1",
    3578: "1.20.2",
    3700: "1.20.4",
    3839: "1.20.6",
    3953: "1.21",
    3955: "1.21.1",
    4080: "1.21.2",
    4082: "1.21.3",
    4189: "1.21.4",
    4325: "1.21.5",
    4435: "1.21.6",
    4438: "1.21.7",
    4440: "1.21.8",
    4554: "1.21.9",
    4556: "1.21.10",
    4671: "1.21.11",
    5023: "26.3",
}


def version_name_for_data_version(data_version: int) -> str:
    """把 DataVersion 转成可写入 level.dat 的版本名；未知时给一个安全值。"""
    return DATA_VERSION_NAMES.get(data_version, "1.21.1")


class BlockRegistryError(Exception):
    pass


class BlockState:
    """单个方块状态：名称 + 属性"""

    __slots__ = ("name", "properties")

    def __init__(self, name: str, properties: dict[str, str] | None = None):
        self.name = name
        self.properties = properties or {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self.properties:
            props = ",".join(f"{k}={v}" for k, v in sorted(self.properties.items()))
            return f"{self.name}[{props}]"
        return self.name


class BlockStateRegistry:
    """
    全局方块状态 ID 的查询表。

    内部用「区间列表」表示：每个方块占据 [min_state_id, max_state_id]，
    查询时二分查找定位方块，再按属性顺序计算区间内偏移。
    """

    def __init__(self):
        # 每项: (min_state_id, max_state_id, name, states_meta)
        self._ranges: list[tuple[int, int, str, list[dict[str, Any]]]] = []
        self._mins: list[int] = []
        self._by_name: dict[str, tuple[int, int, list[dict[str, Any]]]] = {}
        self.source: str = "空（未加载）"
        self.air_state_id: int = 0
        self._cache: dict[int, BlockState | None] = {}
        self.stats: dict[str, int] = {"hits": 0, "misses": 0}

    # ------------------------------------------------------------------ 属性
    @property
    def loaded(self) -> bool:
        return bool(self._ranges)

    @property
    def block_count(self) -> int:
        return len(self._ranges)

    @property
    def state_count(self) -> int:
        return self._ranges[-1][1] + 1 if self._ranges else 0

    # ------------------------------------------------------------------ 构建
    def _finalize(self) -> None:
        self._ranges.sort(key=lambda item: item[0])
        self._mins = [item[0] for item in self._ranges]
        for min_id, max_id, name, meta in self._ranges:
            self._by_name[name] = (min_id, max_id, meta)
        for min_id, _max, name, _meta in self._ranges:
            if name in ("minecraft:air", "air"):
                self.air_state_id = min_id
                break

    @classmethod
    def from_prismarine(cls, data: Any, source: str = "PrismarineJS") -> BlockStateRegistry:
        """解析 PrismarineJS 的 blocks.json（列表形式）"""
        if not isinstance(data, list):
            raise BlockRegistryError("PrismarineJS blocks.json 应为数组")
        registry = cls()
        for block in data:
            try:
                name = block["name"]
                min_id = int(block["minStateId"])
                max_id = int(block["maxStateId"])
            except (KeyError, TypeError, ValueError):
                continue
            if not name.startswith("minecraft:") and ":" not in name:
                name = "minecraft:" + name
            states_meta = block.get("states") or []
            registry._ranges.append((min_id, max_id, name, states_meta))
        if not registry._ranges:
            raise BlockRegistryError("PrismarineJS 数据中未找到任何方块")
        registry._finalize()
        registry.source = source
        return registry

    @classmethod
    def from_reports(cls, data: Any, source: str = "原版数据生成器") -> BlockStateRegistry:
        """解析原版数据生成器的 reports/blocks.json（字典形式）"""
        if not isinstance(data, dict):
            raise BlockRegistryError("reports/blocks.json 应为对象")
        registry = cls()
        for block_name, block in data.items():
            if not isinstance(block, dict):
                continue
            states = block.get("states") or []
            ids = [s.get("id") for s in states if isinstance(s.get("id"), int)]
            if not ids:
                continue
            prop_names = list((block.get("properties") or {}).keys())
            states_meta = []
            for prop in prop_names:
                values = block["properties"][prop]
                if isinstance(values, list):
                    states_meta.append({"name": prop, "values": [str(v) for v in values]})
            registry._ranges.append((min(ids), max(ids), block_name, states_meta))
        if not registry._ranges:
            raise BlockRegistryError("reports/blocks.json 中未找到任何方块")
        registry._finalize()
        registry.source = source
        return registry

    @classmethod
    def from_file(cls, path: str) -> BlockStateRegistry:
        """从文件加载，自动识别两种格式"""
        p = Path(path)
        if not p.exists():
            raise BlockRegistryError(f"方块数据文件不存在: {path}")
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            raise BlockRegistryError(f"读取方块数据失败 {path}: {exc}") from exc

        label = f"{p.name}"
        if isinstance(data, list):
            registry = cls.from_prismarine(data, label)
        elif isinstance(data, dict):
            # 兼容 PrismarineJS 的 {blocks: [...]} 包装
            if "blocks" in data and isinstance(data["blocks"], list):
                registry = cls.from_prismarine(data["blocks"], label)
            else:
                registry = cls.from_reports(data, label)
        else:
            raise BlockRegistryError(f"无法识别的方块数据格式: {type(data)}")

        return registry

    # ------------------------------------------------------------------ 查询
    def name_for(self, state_id: int) -> BlockState | None:
        """把全局方块状态 ID 解析为 (名称, 属性)；未知返回 None"""
        cached = self._cache.get(state_id)
        if cached is not None or state_id in self._cache:
            return cached
        result = self._lookup(state_id)
        if len(self._cache) < 200000:
            self._cache[state_id] = result
        if result is None:
            self.stats["misses"] += 1
        else:
            self.stats["hits"] += 1
        return result

    def _lookup(self, state_id: int) -> BlockState | None:
        if state_id < 0 or not self._ranges:
            return None
        # 二分查找所在区间
        lo, hi = 0, len(self._ranges) - 1
        found = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            min_id, max_id, _name, _meta = self._ranges[mid]
            if state_id < min_id:
                hi = mid - 1
            elif state_id > max_id:
                lo = mid + 1
            else:
                found = mid
                break
        if found < 0:
            return None
        _min_id, _max_id, name, meta = self._ranges[found]
        if not meta:
            return BlockState(name, {})
        return BlockState(name, self._decode_properties(state_id - _min_id, meta))

    @staticmethod
    def _decode_properties(offset: int, meta: list[dict[str, Any]]) -> dict[str, str]:
        """
        把区间内偏移还原为属性组合。

        原版 `StateDefinition` 的顺序是「首个属性变化最慢」，
        即 `index = index * count + value_index` 依次累加。
        """
        counts: list[int] = []
        value_lists: list[list[str]] = []
        for prop in meta:
            values = prop.get("values")
            if not values:
                num = int(prop.get("num_values") or 2)
                if prop.get("type") == "bool":
                    values = ["true", "false"]
                else:
                    values = [str(i) for i in range(num)]
            values = [str(v) for v in values]
            value_lists.append(values)
            counts.append(len(values))

        total = 1
        for count in counts:
            total *= count
        if offset < 0 or offset >= total:
            return {}

        properties: dict[str, str] = {}
        remaining = offset
        # 从最后一个属性往前剥离（最后一个属性变化最快）
        for index in range(len(meta) - 1, -1, -1):
            count = counts[index]
            if count <= 0:
                continue
            value_index = remaining % count
            remaining //= count
            properties[str(meta[index].get("name"))] = value_lists[index][value_index]
        return properties

    def block_name(self, state_id: int) -> str | None:
        state = self.name_for(state_id)
        return state.name if state else None

    def state_id_for_name(self, name: str) -> int | None:
        """按方块名称反查最小状态 ID（无属性时即默认状态）；未知返回 None。

        同时兼容带 / 不带 `minecraft:` 前缀的写法。
        """
        if not name:
            return None
        entry = self._by_name.get(name)
        if entry is None and not name.startswith("minecraft:"):
            entry = self._by_name.get("minecraft:" + name)
        if entry is None and name.startswith("minecraft:"):
            entry = self._by_name.get(name[len("minecraft:"):])
        return entry[0] if entry else None

    def is_air(self, state_id: int) -> bool:
        state = self.name_for(state_id)
        if state is None:
            return state_id == self.air_state_id
        return state.name in ("minecraft:air", "minecraft:void_air", "minecraft:cave_air")

    # ------------------------------------------------------------------ 诊断
    def describe(self) -> str:
        if not self.loaded:
            return "方块注册表未加载（导出的世界将使用占位方块）"
        return (
            f"方块注册表: {self.block_count} 个方块 / {self.state_count} 个状态 "
            f"[来源: {self.source}]"
        )


# --------------------------------------------------------------------------
# 发现 / 下载
# --------------------------------------------------------------------------
def _candidate_version_dirs(protocol_version: int) -> list[str]:
    known = PRISMARINE_VERSION_DIRS.get(protocol_version)
    candidates: list[str] = []
    if known:
        candidates.append(known)
    # 常见回退：向下寻找最近的已发布版本
    for ver in sorted(PRISMARINE_VERSION_DIRS.values(), reverse=True):
        if ver not in candidates:
            candidates.append(ver)
    return candidates


def find_local_fallback(data_dir: str) -> Path | None:
    """
    找不到精确版本的数据时，退而使用任意一个已有的 blocks_*.json。

    方块注册表在不同版本间大体稳定，用邻近版本的数据远好于完全退化为空气。
    """
    for directory in (_shared_data_dir(), Path(data_dir)):
        if not directory.exists():
            continue
        try:
            matches = sorted(directory.glob("blocks_*.json"))
        except Exception:
            continue
        if matches:
            return matches[0]
    return None


def download_block_data(
    protocol_version: int,
    dest: Path,
    timeout: float = 20.0,
) -> Path | None:
    """从 PrismarineJS/minecraft-data 下载方块数据并写入 dest"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for ver in _candidate_version_dirs(protocol_version):
        url = _DOWNLOAD_BASE.format(ver=ver)
        try:
            logger.info(f"正在下载方块数据: {url}")
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, list) or not data:
                continue
            payload = {"source": url, "version": ver, "blocks": data}
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            logger.info(f"方块数据已缓存: {dest} (版本 {ver})")
            return dest
        except Exception as exc:
            logger.debug(f"下载失败 {url}: {exc}")
    return None


def _shared_data_dir() -> Path:
    """模块自带的共享数据目录（下载缓存首选位置，便于跨世界复用）"""
    return Path(__file__).parent / "data"


def find_block_data(
    protocol_version: int | None,
    data_dir: str,
    explicit: str | None = None,
    allow_download: bool = True,
) -> Path | None:
    """按优先级查找可用的方块数据文件"""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        logger.warning(f"指定的方块数据文件不存在: {explicit}")

    base = Path(data_dir)
    shared = _shared_data_dir()
    proto = protocol_version if protocol_version is not None else -1

    # 查找顺序: 世界目录 -> 共享数据目录 -> 内置 data 目录
    candidates = [
        base / f"blocks_{proto}.json",
        shared / f"blocks_{proto}.json",
    ]
    for path in candidates:
        if path.exists():
            return path

    if allow_download and protocol_version is not None:
        # 优先缓存到共享目录，这样其它世界/会话无需再次下载
        dest = shared / f"blocks_{proto}.json"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            dest = base / f"blocks_{proto}.json"
        downloaded = download_block_data(protocol_version, dest)
        if downloaded is not None:
            return downloaded

    # 未知/不可下载版本：退回使用任意已存在的方块数据（好过完全退化为空气）
    return find_local_fallback(data_dir)


def load_registry(
    protocol_version: int | None,
    data_dir: str,
    explicit: str | None = None,
    allow_download: bool = True,
) -> BlockStateRegistry:
    """
    加载方块注册表；找不到任何数据时返回一个空注册表（降级模式），
    调用方可通过 `registry.loaded` 判断是否可用。
    """
    path = find_block_data(protocol_version, data_dir, explicit, allow_download)
    if path is not None:
        try:
            registry = BlockStateRegistry.from_file(str(path))
            logger.info(registry.describe())
            return registry
        except BlockRegistryError as exc:
            logger.error(f"加载方块数据失败: {exc}")

    logger.warning(
        "未找到方块数据表 —— 导出的调色板将退化为「空气 / 石头」占位，"
        "世界可加载但方块类型不正确。\n"
        "  解决办法: 指定 --block-map <blocks.json>，"
        "或允许联网自动下载，或从服务端生成 reports/blocks.json。"
    )
    return BlockStateRegistry()
