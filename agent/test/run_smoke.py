"""
mc-mirror-agent 端到端冒烟测试（无需 Minecraft 客户端）

用 netty 测试桩（agent/test/TestHarness.java，管道 handler 与 vanilla 同名）
验证整条链路：
    ByteBuddy 插桩真实触发 -> 抽头安装 -> 锚点重定位 -> 帧镜像 -> Python 收到

前置：
    * agent/mc-mirror-agent.jar 已构建（bash agent/build.sh）
    * agent/test/classes/TestHarness.class 已编译（javac -cp agent/lib/netty-compile.jar）
    * PATH 里有 java / javac

用法：
    python agent/test/run_smoke.py
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent          # 项目根
AGENT_JAR = ROOT / "agent" / "mc-mirror-agent.jar"
HARNESS_CLS = ROOT / "agent" / "test" / "classes"
NETTY_JAR = ROOT / "agent" / "lib" / "netty-compile.jar"

sys.path.insert(0, str(ROOT))

import core.mirror as mirror_mod  # noqa: E402
from core.mirror import MirrorServer  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {msg}")
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


def compile_harness() -> bool:
    if (HARNESS_CLS / "TestHarness.class").exists():
        return True
    HARNESS_CLS.mkdir(parents=True, exist_ok=True)
    ret = subprocess.run(
        ["javac", "--release", "8", "-encoding", "UTF-8",
         "-cp", str(NETTY_JAR), "-d", str(HARNESS_CLS),
         str(ROOT / "agent" / "test" / "TestHarness.java")],
        capture_output=True, text=True,
    )
    if ret.returncode != 0:
        print("  编译 TestHarness 失败:\n" + ret.stderr[:2000])
        return False
    return True


async def main() -> int:
    if not AGENT_JAR.exists():
        print("  [FAIL] 未找到 agent/mc-mirror-agent.jar（先运行 agent/build.sh）")
        return 1
    if not compile_harness():
        return 1

    received: list[tuple[int, bytes]] = []
    original_feed = mirror_mod.MirrorSession._feed

    def recording_feed(self, frame, clientbound):
        # 记录用与线上协议一致的方向编码：0=clientbound, 1=serverbound
        received.append((0 if clientbound else 1, bytes(frame)))
        return original_feed(self, frame, clientbound)

    mirror_mod.MirrorSession._feed = recording_feed
    try:
        with tempfile.TemporaryDirectory(prefix="agent_smoke_") as tmp:
            server = MirrorServer(host="127.0.0.1", port=0, world_dir=tmp,
                                   allow_download=False)
            check(await server.start(), "镜像服务启动")

            port = server.port_number
            cp_sep = ";" if os.name == "nt" else ":"
            proc = subprocess.Popen(
                ["java", f"-javaagent:{AGENT_JAR}=127.0.0.1:{port}",
                 "-cp", f"{HARNESS_CLS}{cp_sep}{NETTY_JAR}",
                 "TestHarness"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, proc.communicate),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                proc.kill()
                print("  [FAIL] 测试桩超时")
                return 1
            if b"HARNESS:DONE" not in (out or b""):
                print("  测试桩输出（异常时排查用）:")
                print("    " + (out or b"").decode("gbk", errors="replace")[:1500])
            await asyncio.wait_for(server.shutdown(), timeout=10)

            expected = [
                (1, b"\x05hi"),     # 出站帧
                (0, b"\x07yo"),     # 入站帧
                (0, b"\x0czz"),     # 锚点重定位后的入站帧
            ]
            check(received == expected,
                  f"收到且仅收到 3 帧且顺序一致 (got {received})")
            check(server._ever_connected, "agent 曾成功连接")
    finally:
        mirror_mod.MirrorSession._feed = original_feed

    print(f"\nagent 冒烟: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
