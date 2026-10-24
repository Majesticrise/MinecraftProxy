"""
正版验证（online-mode）检测测试

行为约定：
  * 离线验证服务器：无需 mc-mirror-agent，代理模式直接可用（现有路径，不回归）
  * 正版验证服务器（无 agent）：代理检测到 Encryption Request 后**明确提示
    安装 mc-mirror-agent**，而不是默默无区块；转发不受影响
  * 镜像模式（有 agent）：检测到加密只记信息日志，不告警

覆盖：
  * 结构探测识别 Encryption Request（无精确 ID 表的版本，如 770）
  * 47 / 773 精确表（"Encryption Request" / "Encryption Begin"）指向同一处理器
  * 常见 LOGIN 包不被误判为 Encryption Request
"""

import logging
import os
import struct
import sys
import tempfile

# 让测试可以直接运行（把项目根目录加入 sys.path 以导入 core 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.datatypes import write_string, write_varint
from core.packet_ids import detect_login_clientbound
from core.protocol import ConnectionState, ProtocolParser

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


class _Capture(logging.Handler):
    """捕获 core.protocol 日志（propagate=False，须直接挂到该 logger 上）"""

    def __init__(self):
        super().__init__()
        self.records: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


def capture():
    cap = _Capture()
    lg = logging.getLogger("core.protocol")
    lg.addHandler(cap)
    return cap, lg


def make_encryption_request(server_id: str = "", key_len: int = 162,
                            token: bytes = b"\x01\x02\x03\x04") -> bytes:
    """构造 Encryption Request：serverId + publicKey(X.509 DER 风格) + verifyToken"""
    key = bytes([0x30, 0x81, 0x9C]) + bytes((i * 7 + 3) & 0xFF
                                            for i in range(key_len - 3))
    return (write_string(server_id) + write_varint(len(key)) + key
            + write_varint(len(token)) + token)


def new_parser(mirror: bool = False) -> ProtocolParser:
    parser = ProtocolParser(
        world_dir=tempfile.gettempdir(),
        allow_download=False,
        mirror_mode=mirror,
    )
    parser.state = ConnectionState.LOGIN
    return parser


# ---------------------------------------------------------------- 结构探测
payload = make_encryption_request()
check(detect_login_clientbound(payload) == "Encryption Request",
      "探测器识别标准 Encryption Request")
check(detect_login_clientbound(make_encryption_request(server_id="abc123"))
      == "Encryption Request", "非空 serverId 也可识别")

# 误判防护：常见 LOGIN 包不受影响
check(detect_login_clientbound(write_varint(256)) == "Set Compression",
      "Set Compression 仍优先识别")
login_success = b"\x01" * 16 + write_varint(5) + b"Notch"
check(detect_login_clientbound(login_success) == "Login Success",
      "Login Success 不被误判为加密请求")
check(detect_login_clientbound(write_string('{"text":"Server full"}'))
      == "Disconnect", "Disconnect 不被误判为加密请求")
check(detect_login_clientbound(struct.pack(">qq", 1, 2)) is None,
      "随机 long 对不误判")

# ---------------------------------------------------------------- 代理模式提示
cap, lg = capture()
try:
    parser = new_parser()
    parser.resolver.set_protocol_version(770)   # 无精确表 -> 结构探测
    parser.process_clientbound(0x01, payload)
    check("Encryption Request" in parser.counters, "探测模式识别 Encryption Request")
    check(parser._online_mode_reported, "已标记 online-mode")
    joined = "\n".join(msg for _lvl, msg in cap.records)
    check("online-mode" in joined and "agent" in joined,
          "代理模式输出安装 agent 提示")
    # 重复的加密请求只提示一次
    parser.process_clientbound(0x01, payload)
    parser.process_clientbound(0x01, payload)
    warn_count = sum(1 for lvl, _msg in cap.records
                     if lvl >= logging.WARNING and "online-mode" in _msg)
    check(warn_count == 1, f"提示只触发一次 (实际 {warn_count})")
finally:
    lg.removeHandler(cap)

# ---------------------------------------------------------------- 精确表别名
cap, lg = capture()
try:
    parser = new_parser()
    parser.resolver.set_protocol_version(773)   # 表内名称为 "Encryption Begin"
    parser.process_clientbound(0x01, payload)
    check("Encryption Begin" in parser.counters, "773 表识别 Encryption Begin")
    check(parser._online_mode_reported, "别名处理器 _on_encryption_begin 生效")

    parser47 = new_parser()
    parser47.resolver.set_protocol_version(47)  # 1.8 表名称为 "Encryption Request"
    parser47.process_clientbound(0x01, payload)
    check("Encryption Request" in parser47.counters, "47 表识别 Encryption Request")
finally:
    lg.removeHandler(cap)

# ---------------------------------------------------------------- 镜像模式
cap, lg = capture()
try:
    parser = new_parser(mirror=True)
    parser.resolver.set_protocol_version(770)
    parser.process_clientbound(0x01, payload)
    check(parser._online_mode_reported, "镜像模式同样标记（用于诊断）")
    infos = [msg for lvl, msg in cap.records
             if "镜像模式" in msg]
    check(bool(infos), "镜像模式输出信息性日志")
    check(all(lvl < logging.WARNING for lvl, _msg in cap.records),
          "镜像模式不产生告警")
finally:
    lg.removeHandler(cap)

print(f"\n正版服检测测试: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
