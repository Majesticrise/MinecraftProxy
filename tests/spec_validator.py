"""
独立校验器（**不使用项目代码**，自写最小 NBT 读取器）。

目标：以真实 1.21.10 世界（金样本，Paper 数据生成器产出；位于项目根的
`标准文件/新的世界/`，旧名 `你想要的/` 亦被识别）为基准，
逐项核对本项目产出的 `downloaded_world` 是否符合官方 Anvil 规范。

默认行为：用项目代码从真实抓包固件生成一个临时世界，再用本校验器
（独立实现）核对它。这样「生成」与「校验」使用两套完全独立的代码路径，
能捕获「自洽但不符合规范」的缺陷（历史上 `.mca` 字节序 bug 正是此类）。

用法:
    python tests/spec_validator.py              # 自动生成并校验
    python tests/spec_validator.py <世界目录>    # 校验已有世界

校验项：
  A. 区域文件字节布局（[len 4B BE][type 1B][data]，length = 1 + len(compressed)）
  B. 区块顶层键集合 ⊇ 官方必需键
  C. Status == "full"、isLightOn == 1、yPos == 最低节 Y
  D. sections 数量 ∈ {16, 24}，Y 连续递增
  E. block_states.palette 元素为 Compound（Name/Properties）
  F. biomes.palette 元素为 String（资源位置）★ 关键修复点
  G. structures 含 starts + References
  H. PostProcessing 长度 == 节数
  I. level.dat：DragonFight 在 Data 顶层，且无 DimensionData ★ 关键修复点
"""
from __future__ import annotations

import gzip
import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 金样本数据目录：目录曾名 `你想要的/`，后改名 `标准文件/`，依次探测两者，
# 避免改名后校验被静默跳过（blocks.json 等报告文件位于同级的 generated/ 下）
REF_CANDIDATES = [
    ROOT / "标准文件" / "新的世界",
    ROOT / "你想要的" / "新的世界",
]
REF = next((p for p in REF_CANDIDATES if p.exists()), REF_CANDIDATES[0])
FIXTURE_DIR = ROOT / "tests" / "fixtures"
FIXTURES = [
    ("real_1_21_9_via_chunk_0.bin", 0, 0),
    ("real_1_21_9_via_chunk_1.bin", 1, 0),
    ("real_1_21_9_via_chunk_2.bin", 1, 1),
]

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


# ---------------------------------------------------------------------------
# 最小 NBT 读取器（独立实现）
# ---------------------------------------------------------------------------
TAGN = {0: "END", 1: "Byte", 2: "Short", 3: "Int", 4: "Long", 5: "Float",
        6: "Double", 7: "ByteArray", 8: "String", 9: "List", 10: "Compound",
        11: "IntArray", 12: "LongArray"}


def _read_payload(data: bytes, pos: int, tid: int):
    if tid == 1:
        return struct.unpack(">b", data[pos:pos + 1])[0], pos + 1
    if tid == 2:
        return struct.unpack(">h", data[pos:pos + 2])[0], pos + 2
    if tid == 3:
        return struct.unpack(">i", data[pos:pos + 4])[0], pos + 4
    if tid == 4:
        return struct.unpack(">q", data[pos:pos + 8])[0], pos + 8
    if tid == 5:
        return struct.unpack(">f", data[pos:pos + 4])[0], pos + 4
    if tid == 6:
        return struct.unpack(">d", data[pos:pos + 8])[0], pos + 8
    if tid == 7:
        n = struct.unpack(">i", data[pos:pos + 4])[0]
        return data[pos + 4:pos + 4 + n], pos + 4 + n
    if tid == 8:
        n = struct.unpack(">H", data[pos:pos + 2])[0]
        return data[pos + 2:pos + 2 + n].decode("utf-8", "replace"), pos + 2 + n
    if tid == 9:
        et = data[pos]
        n = struct.unpack(">i", data[pos + 1:pos + 5])[0]
        pos += 5
        items = []
        for _ in range(n):
            v, pos = _read_payload(data, pos, et)
            items.append(v)
        return {"__list_type__": et, "items": items}, pos
    if tid == 10:
        out: dict = {}
        while data[pos] != 0:
            name, val, pos = _read_named(data, pos)
            out[name] = val
        return out, pos + 1
    if tid == 11:
        n = struct.unpack(">i", data[pos:pos + 4])[0]
        return list(struct.unpack(f">{n}i", data[pos + 4:pos + 4 + 4 * n])), pos + 4 + 4 * n
    if tid == 12:
        n = struct.unpack(">i", data[pos:pos + 4])[0]
        return list(struct.unpack(f">{n}q", data[pos + 4:pos + 4 + 8 * n])), pos + 4 + 8 * n
    raise ValueError(f"未知 tag {tid}")


def _read_named(data: bytes, pos: int):
    tid = data[pos]
    pos += 1
    if tid == 0:
        return "", None, pos
    nl = struct.unpack(">H", data[pos:pos + 2])[0]
    pos += 2
    name = data[pos:pos + nl].decode("utf-8", "replace")
    pos += nl
    val, pos = _read_payload(data, pos, tid)
    return name, val, pos


def parse_nbt(data: bytes):
    """返回 (根 compound, 消耗字节数)"""
    assert data[0] == 10, f"根标签应为 Compound，实际 {data[0]}"
    name, val, pos = _read_named(data, 0)
    return val, pos


# ---------------------------------------------------------------------------
# 区域文件读取（独立实现，严格按官方规范）
# ---------------------------------------------------------------------------
def read_region_chunk(raw: bytes, idx: int):
    """返回 (nbt_bytes, compression_type) 或 None"""
    entry = struct.unpack(">I", raw[idx * 4:idx * 4 + 4])[0]
    offset = entry >> 8
    if offset == 0:
        return None
    pos = offset * 4096
    length = struct.unpack(">I", raw[pos:pos + 4])[0]
    ctype = raw[pos + 4]
    payload = raw[pos + 5:pos + 5 + (length - 1)]
    if ctype == 1:
        return gzip.decompress(payload), ctype
    if ctype == 2:
        return zlib.decompress(payload), ctype
    if ctype == 3:
        return payload, ctype
    return None


def list_blocks(data):
    """把 List 值解包为普通列表"""
    if isinstance(data, dict) and "__list_type__" in data:
        return data["items"], data["__list_type__"]
    return data, None


def generate_world(out_dir: Path) -> bool:
    """
    用**项目代码**从真实抓包固件生成一个世界（供独立校验器核对）。

    注意：这里刻意调用项目代码；校验器本身完全独立实现，
    因此「生成」与「校验」是两条独立的代码路径。

    生物群系注册表用**真实世界里实际出现过的群系名**（从基准世界扫出来），
    这样既验证「注册表 -> 名字」的管道，又让产出的名字有据可查。
    """
    sys.path.insert(0, str(ROOT))
    import asyncio

    from core.biome_registry import BiomeRegistry
    from core.block_registry import load_registry
    from core.chunk_extractor import ChunkExtractor
    from core.chunk_format import (
        build_chunk_nbt,
        compress_chunk_nbt,
        parse_chunk_data_packet,
    )

    class _Chunk:
        def __init__(self, x: int, z: int, data: bytes):
            self.x = x
            self.z = z
            self.data = data
            self.block_entities: list = []

    async def run() -> bool:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        registry = load_registry(773, str(ROOT), allow_download=False)
        if not registry.loaded:
            print("  [警告] 方块注册表未加载，跳过生成")
            return False

        # 用真实世界的群系名构造注册表（排序保证可复现）
        biome_names = sorted(_collect_biome_names(REF)) if REF.exists() else []
        biome_registry = BiomeRegistry()
        if biome_names:
            biome_registry.set_names(biome_names, "真实世界实测")

        extractor = ChunkExtractor(str(out_dir))
        extractor.data_version = 4556  # 1.21.10
        ok = 0
        for name, cx, cz in FIXTURES:
            fp = FIXTURE_DIR / name
            if not fp.exists():
                continue
            parsed = parse_chunk_data_packet(
                fp.read_bytes(), section_count=24, min_section_y=-4,
                has_fluid_count=False, has_data_length=False,
            )
            if parsed is None:
                continue
            parsed.x, parsed.z = cx, cz
            raw = build_chunk_nbt(parsed, registry, data_version=4556,
                                  biome_registry=biome_registry)
            if await extractor.save_chunk(
                _Chunk(cx, cz, compress_chunk_nbt(raw)), "minecraft:overworld"
            ):
                ok += 1
        await extractor.drain()
        await extractor.shutdown()
        return ok > 0

    return asyncio.run(run())


def main() -> int:
    print("=" * 70)
    print("独立校验器：以真实 1.21.10 世界为基准核对本项目产出")
    print("=" * 70)

    # 确定待校验世界
    tmp_dir = None
    if len(sys.argv) > 1:
        ours = Path(sys.argv[1])
    else:
        if not REF.exists():
            print(f"\n[跳过] 未找到真实基准世界: {REF}")
            print("（该目录由用户提供；缺失时本校验器不运行）")
            return 0
        tmp_dir = Path(tempfile.mkdtemp(prefix="specval_"))
        ours = tmp_dir / "world"
        print(f"\n生成测试世界 -> {ours}")
        if not generate_world(ours):
            print("  [失败] 无法生成测试世界")
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return 1

    try:
        return _validate(ours)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _collect_biome_names(world: Path) -> set[str]:
    """扫描一个世界，收集所有出现过的生物群系资源位置"""
    names: set[str] = set()
    for fp in sorted((world / "region").glob("r.*.mca")):
        raw = fp.read_bytes()
        for idx in range(0, 1024, 11):
            got = read_region_chunk(raw, idx)
            if got is None:
                continue
            try:
                root, _end = parse_nbt(got[0])
            except Exception:  # noqa: BLE001
                continue
            sections, _ = list_blocks(root.get("sections"))
            for sec in sections:
                if not isinstance(sec, dict):
                    continue
                bi = sec.get("biomes")
                if not isinstance(bi, dict):
                    continue
                pal, _t = list_blocks(bi.get("palette"))
                for entry in pal:
                    if isinstance(entry, str):
                        names.add(entry)
    return names


def _validate(OURS: Path) -> int:
    # ---------------- A. 区域文件字节布局 ----------------
    print("\n== A. 区域文件字节布局 ==")
    our_regions = sorted((OURS / "region").glob("r.*.mca"))
    check(bool(our_regions), "存在区域文件")
    for fp in our_regions:
        raw = fp.read_bytes()
        check(len(raw) % 4096 == 0, f"{fp.name} 大小为 4096 的整数倍")
        for idx in range(1024):
            entry = struct.unpack(">I", raw[idx * 4:idx * 4 + 4])[0]
            off = entry >> 8
            cnt = entry & 0xFF
            if off == 0:
                continue
            pos = off * 4096
            length = struct.unpack(">I", raw[pos:pos + 4])[0]
            ctype = raw[pos + 4]
            check(ctype in (1, 2, 3),
                  f"{fp.name} idx{idx} 压缩类型合法 ({ctype})")
            # length = 1 + len(compressed)；据此推出压缩数据长度
            comp_len = length - 1
            need_sectors = (5 + comp_len + 4095) // 4096
            check(cnt == need_sectors,
                  f"{fp.name} idx{idx} 扇区数匹配 (声明 {cnt}, 计算 {need_sectors})")
            try:
                payload = raw[pos + 5:pos + 5 + comp_len]
                if ctype == 2:
                    nbt = zlib.decompress(payload)
                elif ctype == 1:
                    nbt = gzip.decompress(payload)
                else:
                    nbt = payload
                check(nbt[0] == 10, f"{fp.name} idx{idx} 解压后为 Compound")
            except Exception as e:  # noqa: BLE001
                check(False, f"{fp.name} idx{idx} 解压失败: {e}")

    # ---------------- 方块注册表 vs 官方 blocks.json ----------------
    print("\n== J. 方块注册表 vs 官方 blocks.json ==")
    blocks_json = REF.parent / "generated" / "reports" / "blocks.json"
    if blocks_json.exists():
        import json
        with blocks_json.open("r", encoding="utf-8") as fh:
            official = json.load(fh)
        official_states = sum(len(v.get("states", [])) for v in official.values())
        check(len(official) == 1166, f"官方方块数=1166 (got {len(official)})")
        check(official_states == 29671,
              f"官方状态数=29671 (got {official_states})")

        # 与项目注册表逐条比对
        sys.path.insert(0, str(ROOT))
        from core.block_registry import load_registry
        reg = load_registry(773, str(ROOT), allow_download=False)
        check(reg.loaded, "项目注册表已加载")
        if reg.loaded:
            check(reg.block_count == len(official),
                  f"方块数一致 ({reg.block_count} vs {len(official)})")
            check(reg.state_count == official_states,
                  f"状态数一致 ({reg.state_count} vs {official_states})")

            # 抽查若干状态 ID -> 名称/属性
            mismatches = 0
            for block_name, entry in list(official.items()):
                for st in entry.get("states", []):
                    sid = st["id"]
                    got = reg.name_for(sid)
                    if got is None or got.name != block_name:
                        mismatches += 1
                        if mismatches <= 3:
                            print(f"    不匹配 id={sid} 期望={block_name} 实际={got}")
                    elif st.get("properties"):
                        exp = {k: str(v) for k, v in st["properties"].items()}
                        if got.properties != exp:
                            mismatches += 1
                            if mismatches <= 3:
                                print(f"    属性不匹配 id={sid} 期望={exp} 实际={got.properties}")
            check(mismatches == 0, f"全部状态 ID 映射一致（不匹配 {mismatches} 条）")
    else:
        print("  [跳过] 未找到官方 blocks.json")

    # ---------------- 生物群系名交叉校验 ----------------
    print("\n== K. 生物群系名交叉校验（对照真实世界）==")
    real_biomes = _collect_biome_names(REF) if REF.exists() else set()
    if real_biomes:
        check(len(real_biomes) >= 8,
              f"真实世界出现 ≥8 种群系 (got {len(real_biomes)})")
        print(f"     真实世界群系: {sorted(real_biomes)}")
        # 我们产出的群系名必须都在真实世界出现过的集合里
        ours_biomes = _collect_biome_names(OURS)
        check(bool(ours_biomes), f"我们产出的群系非空 (got {len(ours_biomes)})")
        if ours_biomes:
            print(f"     我们的群系: {sorted(ours_biomes)}")
            unknown = {b for b in ours_biomes if b not in real_biomes}
            check(not unknown,
                  f"我们的群系名都在真实世界集合内（多出 {unknown}）")
    else:
        print("  [跳过] 未找到真实世界基准")

    # ---------------- B~H. 区块结构 ----------------
    print("\n== B~H. 区块结构（对照真实世界）==")
    checked = 0
    for fp in our_regions:
        raw = fp.read_bytes()
        for idx in range(1024):
            got = read_region_chunk(raw, idx)
            if got is None:
                continue
            nbt, _ = got
            root, end = parse_nbt(nbt)
            check(end == len(nbt), f"{fp.name} idx{idx} NBT 恰好消耗全部字节")
            checked += 1

            # B. 顶层键
            required = {"DataVersion", "xPos", "zPos", "yPos", "Status",
                        "LastUpdate", "InhabitedTime", "isLightOn", "sections",
                        "Heightmaps", "structures", "block_entities",
                        "block_ticks", "fluid_ticks", "PostProcessing"}
            missing = required - set(root.keys())
            check(not missing, f"{fp.name} idx{idx} 顶层键齐全（缺 {missing}）")

            # C. 关键取值
            check(root.get("Status") == "full",
                  f"{fp.name} idx{idx} Status=full")
            check(root.get("isLightOn") == 1,
                  f"{fp.name} idx{idx} isLightOn=1")

            # D. sections
            sections, _ = list_blocks(root.get("sections"))
            check(len(sections) in (16, 24),
                  f"{fp.name} idx{idx} 节数 ∈ {{16,24}} (got {len(sections)})")
            ys = [s.get("Y") for s in sections if isinstance(s, dict)]
            ys_int = [int(y) for y in ys if isinstance(y, int)]
            # 节 Y 必须从 yPos 开始连续递增
            check(root.get("yPos") == (ys_int[0] if ys_int else None),
                  f"{fp.name} idx{idx} yPos == 最低节 Y "
                  f"(yPos={root.get('yPos')}, Y0={ys_int[0] if ys_int else None})")
            check(bool(ys_int) and ys_int == list(range(ys_int[0], ys_int[0] + len(ys_int))),
                  f"{fp.name} idx{idx} Y 连续递增 (got {ys_int[:4]}...)")

            # E/F. 调色板
            for si, sec in enumerate(sections[:3]):
                if not isinstance(sec, dict):
                    continue
                bs = sec.get("block_states")
                if isinstance(bs, dict):
                    bp, _ = list_blocks(bs.get("palette"))
                    check(all(isinstance(e, dict) for e in bp),
                          f"{fp.name} idx{idx} sec{si} block palette 为 Compound")
                    check(all("Name" in e for e in bp if isinstance(e, dict)),
                          f"{fp.name} idx{idx} sec{si} block palette 含 Name")
                bi = sec.get("biomes")
                if isinstance(bi, dict):
                    bip, _t = list_blocks(bi.get("palette"))
                    check(all(isinstance(e, str) for e in bip),
                          f"{fp.name} idx{idx} sec{si} biome palette 为 String "
                          f"(got {[type(e).__name__ for e in bip]})")
                    # 生物群系名必须是真实资源位置（不是占位值）
                    check(all(str(e).startswith("minecraft:") for e in bip),
                          f"{fp.name} idx{idx} sec{si} biome 以 minecraft: 开头")
                    # 单元素调色板时不应有 data
                    if len(bip) == 1:
                        check("data" not in bi,
                              f"{fp.name} idx{idx} sec{si} 单元素 biome 无 data")
                    elif len(bip) > 1:
                        bdata = bi.get("data")
                        bits = max(1, (len(bip) - 1).bit_length())
                        per = 64 // bits
                        expect = (64 + per - 1) // per
                        check(isinstance(bdata, list) and len(bdata) == expect,
                              f"{fp.name} idx{idx} sec{si} biome data 长度 "
                              f"(palette={len(bip)} bits={bits} "
                              f"got {len(bdata) if isinstance(bdata, list) else bdata} "
                              f"expect {expect})")

            # G. structures
            st = root.get("structures")
            check(isinstance(st, dict) and "starts" in st and "References" in st,
                  f"{fp.name} idx{idx} structures 含 starts+References")

            # H. PostProcessing
            pp, _ = list_blocks(root.get("PostProcessing"))
            check(isinstance(pp, list) and len(pp) == len(sections),
                  f"{fp.name} idx{idx} PostProcessing 长度 == 节数")

            if checked >= 12:
                break
        if checked >= 12:
            break
    check(checked >= 3, f"至少校验 3 个区块 (got {checked})")

    # ---------------- I. level.dat ----------------
    print("\n== I. level.dat（对照真实世界）==")
    our_lvl = OURS / "level.dat"
    check(our_lvl.exists(), "存在 level.dat")

    with gzip.open(our_lvl, "rb") as fh:
        our_nbt = fh.read()
    our_root, _ = parse_nbt(our_nbt)
    our_data = our_root.get("Data", {})
    check(isinstance(our_data, dict), "Data 是 Compound")

    # DragonFight 必须在顶层
    check("DragonFight" in our_data, "Data 顶层含 DragonFight ★")
    check("DimensionData" not in our_data,
          "Data 不含 DimensionData（非官方结构）★")
    df = our_data.get("DragonFight")
    if isinstance(df, dict):
        for f in ("DragonKilled", "PreviouslyKilled", "NeedsStateScanning", "Gateways"):
            check(f in df, f"DragonFight 含 {f}")
        gw, _ = list_blocks(df.get("Gateways"))
        check(isinstance(gw, list) and len(gw) == 20,
              f"Gateways 为 20 个索引 (got {len(gw) if isinstance(gw, list) else gw})")

    # 与真实世界对照（若基准存在）
    ref_lvl = REF / "level.dat"
    if ref_lvl.exists():
        with gzip.open(ref_lvl, "rb") as fh:
            ref_root, _ = parse_nbt(fh.read())
        ref_data = ref_root.get("Data", {})
        check("DragonFight" in ref_data, "真实世界 Data 顶层也有 DragonFight（对照）")
        check("DimensionData" not in ref_data, "真实世界无 DimensionData（对照）")

    print("\n" + "=" * 70)
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    if FAILURES:
        print("\n失败项:")
        for f in FAILURES[:30]:
            print(f"  - {f}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
