"""
统一测试入口

用法:
    python run_tests.py            # 运行全部测试
    python run_tests.py selftest   # 只运行指定套件（子串匹配）
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TESTS = ROOT / "tests"

SUITES = [
    ("单元测试（NBT / 区块 / ID 解析 / 区域写入）", "selftest.py"),
    ("误判测试（常见包不被识别为区块包）", "misdetect_test.py"),
    ("端到端测试（真实 TCP，协议 777）", "e2e_test.py"),
    ("版本自适应测试（无 ID 表，纯结构探测）", "version_test.py"),
    ("边界测试（直接调色板 / 光照 / 分片）", "edge_test.py"),
    ("导出世界可用性测试（真实方块名 / 光照 / level.dat）", "usability_test.py"),
    ("多版本兼容测试（1.8.9 / 1.16.5 / 1.18.2）", "multiversion_test.py"),
    ("协议版本分派测试（家族 / 几何 / 维度）", "version_dispatch_test.py"),
    ("健壮性回归测试（负数 VarInt / 落盘 / 内存上限 / 网络）", "robustness_test.py"),
    ("真实数据回归（ViaVersion 未翻译区块负载 / 1.21.9 精确表）", "realdata_test.py"),
    ("生物群系识别（1.8 / 1.16 数字表 / 1.18+ 网络注册表 / 降采样）", "biome_test.py"),
    ("独立规范校验（对照真实 1.21.10 世界，不依赖项目代码）", "spec_validator.py"),
]


def main(argv):
    selected = SUITES
    if len(argv) > 1:
        wanted = argv[1].lower()
        selected = [s for s in SUITES if wanted in s[1].lower()]
        if not selected:
            print(f"未找到匹配的测试套件: {wanted}")
            return 1

    failed = []
    for title, script in selected:
        print("\n" + "=" * 72)
        print(f"  {title}")
        print("=" * 72)
        result = subprocess.run(
            [sys.executable, str(TESTS / script)],
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            failed.append(script)

    print("\n" + "=" * 72)
    if failed:
        print(f"  失败: {', '.join(failed)}")
        return 1
    print("  全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
