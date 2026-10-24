"""
MinecraftProxy 一键自检（--doctor）

排查「为什么用不了」的第一步：逐项检查运行环境并给出修复建议，
全部结果一张表输出，方便截图求助。

用法:
    python tools/doctor.py               # 基础检查（Python / 数据表 / 端口 / agent）
    python tools/doctor.py --smoke       # 追加 agent 链路冒烟（需 java 与已构建 jar）
    python main.py --doctor              # 等价入口
"""

import argparse
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
ICONS = {OK: "[OK]  ", WARN: "[警告]", FAIL: "[失败]"}

results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))


def check_python() -> None:
    # 运行时环境检查（用户可能用旧 Python 启动 doctor，不能按 py310 目标裁剪）
    if sys.version_info >= (3, 10):  # noqa: UP036
        record(OK, f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        record(FAIL, f"Python {sys.version_info.major}.{sys.version_info.minor}",
               "需要 >= 3.10（X | None 语法）")


def check_data_tables() -> None:
    import glob
    tables = glob.glob(os.path.join(ROOT, "core", "data", "blocks_*.json"))
    if tables:
        record(OK, f"方块数据表已缓存（{len(tables)} 个版本）")
    else:
        record(WARN, "方块数据表未缓存",
               "首次运行会联网自动下载；离线环境请用 --block-map 指定")


def _run(cmd: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        text = (r.stdout or b"") + (r.stderr or b"")
        return r.returncode == 0, text.decode("gbk", errors="replace").strip()[:80]
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def check_java() -> None:
    ok, out = _run(["java", "-version"])
    if ok:
        record(OK, f"java 可用（{out.splitlines()[0][:60] if out else ''}）")
    else:
        record(WARN, "java 不可用", "仅镜像模式（正版服）需要；代理模式不受影响")


def check_javac() -> None:
    ok, _ = _run(["javac", "-version"])
    if ok:
        record(OK, "javac 可用（可构建 agent）")
    else:
        record(WARN, "javac 不可用", "构建 mc-mirror-agent 需要 JDK（仅 JRE 不够）")


def check_agent() -> None:
    jar = os.path.join(ROOT, "agent", "mc-mirror-agent.jar")
    if os.path.exists(jar):
        record(OK, "mc-mirror-agent.jar 已构建")
    else:
        record(WARN, "mc-mirror-agent.jar 不存在",
               "抓正版服需要：cd agent && build.bat（或 bash build.sh）")
    lib = os.path.join(ROOT, "agent", "lib", "byte-buddy.jar")
    if os.path.exists(lib):
        record(OK, "agent 依赖（byte-buddy）在位")
    else:
        record(WARN, "agent 依赖缺失（agent/lib/byte-buddy.jar）",
               "按 agent/build.bat 开头的提示下载")


def check_ports() -> None:
    for port, usage in ((25565, "代理模式默认端口"), (25566, "镜像模式默认端口")):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                s.close()
            record(OK, f"端口 {port} 可用（{usage}）")
        except OSError:
            record(WARN, f"端口 {port} 被占用（{usage}）",
                   f"换端口：--local-port / --mirror-port {port + 1}")


def check_smoke() -> None:
    jar = os.path.join(ROOT, "agent", "mc-mirror-agent.jar")
    if not os.path.exists(jar):
        record(FAIL, "冒烟跳过：agent 未构建")
        return
    r = subprocess.run([sys.executable, os.path.join(ROOT, "agent", "test", "run_smoke.py")],
                       capture_output=True, timeout=120)
    tail = (r.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()
    summary = tail[-1] if tail else ""
    if r.returncode == 0:
        record(OK, f"agent 链路冒烟通过（{summary}）")
    else:
        record(FAIL, "agent 链路冒烟失败", summary or "运行 python agent/test/run_smoke.py 查看详情")


def main() -> int:
    parser = argparse.ArgumentParser(description="MinecraftProxy 一键自检")
    parser.add_argument("--smoke", action="store_true", help="追加 agent 链路冒烟测试")
    args = parser.parse_args()

    print("\n=== MinecraftProxy 自检 ===\n")
    check_python()
    check_data_tables()
    check_ports()
    check_java()
    check_javac()
    check_agent()
    if args.smoke:
        check_smoke()

    fails = 0
    for status, name, detail in results:
        line = f"  {ICONS[status]} {name}"
        if detail:
            line += f" —— {detail}"
        print(line)
        if status == FAIL:
            fails += 1

    print()
    if fails:
        print(f"  {fails} 项失败。把上面的输出发到求助帖里即可。")
        return 1
    print("  环境就绪。WARNING 项只影响对应功能，不影响其他模式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
