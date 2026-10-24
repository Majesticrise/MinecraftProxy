"""
附加数据表：物品 / 实体的数字 ID -> 资源名。

为什么需要
------------
容器内容（Window Items）里的物品是**数字 item id**，实体（Spawn Entity）
是**数字 entity type id**，而写入世界的 NBT 需要资源名
（`minecraft:stone` / `minecraft:zombie`）。与方块表一样，
PrismarineJS 的 items.json / entities.json 提供逐版本映射。

来源与缓存策略与 block_registry 相同：本地缓存（core/data/）->
PrismarineJS 下载 -> 任意邻近版本回退。
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .block_registry import PRISMARINE_VERSION_DIRS
from .logger import setup_logger

logger = setup_logger(__name__)

_DOWNLOAD_BASE = (
    "https://raw.githubusercontent.com/PrismarineJS/minecraft-data/master/data/pc/{ver}/{name}.json"
)


def _shared_data_dir() -> Path:
    return Path(__file__).parent / "data"


def _candidate_dirs(protocol_version: int | None) -> list[str]:
    known = PRISMARINE_VERSION_DIRS.get(protocol_version or -1)
    candidates: list[str] = []
    if known:
        candidates.append(known)
    for ver in sorted(PRISMARINE_VERSION_DIRS.values(), reverse=True):
        if ver not in candidates:
            candidates.append(ver)
    return candidates


def load_name_map(
    resource: str,
    protocol_version: int | None,
    data_dir: str = ".",
    allow_download: bool = True,
) -> dict[int, str]:
    """
    加载 `resource`（"items" / "entities"）的 id -> 资源名映射。

    找不到时返回空 dict（调用方自行降级：物品退化为 `minecraft:unknown`，
    实体跳过写入），绝不抛异常。
    """
    shared = _shared_data_dir()
    proto = protocol_version if protocol_version is not None else -1

    # 1) 精确版本的本地缓存（items_<proto>.json / entities_<proto>.json）
    exact = shared / f"{resource}_{proto}.json"
    if exact.exists():
        try:
            return _load_file(exact, resource)
        except Exception as exc:
            logger.debug(f"读取 {exact} 失败: {exc}")

    # 2) 下载（优先精确版本，其次按邻近版本回退）
    if allow_download and protocol_version is not None:
        for ver in _candidate_dirs(protocol_version):
            url = _DOWNLOAD_BASE.format(ver=ver, name=resource)
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    raw = resp.read()
                mapping = _parse_prismarine(json.loads(raw.decode("utf-8")), resource)
                if not mapping:
                    continue
                exact.parent.mkdir(parents=True, exist_ok=True)
                with open(exact, "w", encoding="utf-8") as fh:
                    json.dump(mapping, fh, ensure_ascii=False)
                logger.info(f"{resource} 数据表: {len(mapping)} 项 [来源: {ver}]")
                return mapping
            except Exception as exc:
                logger.debug(f"下载 {url} 失败: {exc}")

    # 3) 任意已有的本地缓存（映射大体稳定，邻近版本远好于丢弃）
    for path in sorted(shared.glob(f"{resource}_*.json")):
        try:
            return _load_file(path, resource)
        except Exception:
            continue

    logger.warning(f"未找到 {resource} 数据表（容器/实体内容将退化）")
    return {}


def _load_file(path: Path, resource: str) -> dict[int, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {int(k): str(v) for k, v in data.items()}
    return _parse_prismarine(data, resource)


def _parse_prismarine(data, resource: str) -> dict[int, str]:
    """PrismarineJS 的 items.json / entities.json 都是列表 [{name, id, ...}]"""
    mapping: dict[int, str] = {}
    if not isinstance(data, list):
        return mapping
    for entry in data:
        try:
            name = entry.get("name")
            item_id = entry.get("id")
            if isinstance(name, str) and isinstance(item_id, int):
                if not name.startswith("minecraft:") and ":" not in name:
                    name = "minecraft:" + name
                mapping[item_id] = name
        except Exception:
            continue
    return mapping
