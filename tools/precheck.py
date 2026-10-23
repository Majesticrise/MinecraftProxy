"""
MinecraftProxy —— 目标服务器预检工具

在正式跑代理之前，先用「服务器列表 ping」快速确认目标服是否可用于抓取。

检查项：
  1. TCP 可达性 + 服务器状态（MOTD / 版本名 / 协议号 / 在线人数）
  2. 协议号是否在我们支持的范围内
  3. 协议号是否属于「有区块解析器」的区间
  4. 给出该协议号对应的区块格式家族与 DataVersion

说明：本工具只发 Status(1) 请求，**不做登录**，因此无法直接判断
online-mode。要确认离线/在线，必须真正走一遍登录（见 README 测试章节）。

用法:
    python tools/precheck.py 127.0.0.1:25565
    python tools/precheck.py mc.example.com
    python tools/precheck.py a:25565 b:25566 c:25567

    # 真实登录探测（验证线上实际接受的协议号；可检测 online-mode）
    python tools/precheck.py mc.example.com --probe-login
    python tools/precheck.py mc.example.com --probe-login --protocol 773

依赖: 仅标准库（Python >= 3.10）
"""

import json
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.packet_ids import PROTOCOL_VERSION_NAMES  # noqa: E402
from core.protocol import chunk_format_family  # noqa: E402
from core.varint import VarInt  # noqa: E402

DEFAULT_PORT = 25565
TIMEOUT = 8.0

# 有区块解析器的协议号区间（闭区间）。1.9~1.15.2 目前没有解析器。
_SUPPORTED_RANGES = (
    (47, 47),          # 1.8 ~ 1.8.9
    (735, 756),        # 1.16 ~ 1.17.1
    (757, 774),        # 1.18 ~ 1.21.11
    (777, 777),        # 26.3
)


def parse_address(addr: str) -> tuple[str, int]:
    """解析 "host" 或 "host:port" """
    if addr.startswith("["):  # [::1]:25565
        host, _, rest = addr[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else DEFAULT_PORT
        return host, port
    if addr.count(":") == 1:
        host, port_str = addr.rsplit(":", 1)
        if port_str.isdigit():
            return host, int(port_str)
    return addr, DEFAULT_PORT


def _read_varint_sock(sock: socket.socket) -> int:
    """从 socket 逐字节读一个 VarInt（Status 响应很小，无需缓冲池）"""
    result = 0
    for i in range(5):
        raw = sock.recv(1)
        if not raw:
            raise ConnectionError("连接在读取 VarInt 时中断")
        byte = raw[0]
        result |= (byte & 0x7F) << (7 * i)
        if (byte & 0x80) == 0:
            return result
    raise ValueError("VarInt 超过 5 字节")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < n:
        raw = sock.recv(n - len(chunks))
        if not raw:
            raise ConnectionError(f"期望 {n} 字节，实际收到 {len(chunks)}")
        chunks += raw
    return bytes(chunks)


def query_status(host: str, port: int, timeout: float = TIMEOUT) -> dict:
    """发送 Status 请求，返回服务器返回的 JSON（dict）"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)

        # 1) Handshake: proto=-1(探测) host port next_state=1
        host_bytes = host.encode("utf-8")
        body = (
            VarInt.write(0x00)
            + VarInt.write(-1)
            + VarInt.write(len(host_bytes)) + host_bytes
            + struct.pack(">H", port)
            + VarInt.write(1)
        )
        sock.sendall(VarInt.write(len(body)) + body)

        # 2) Status Request
        sock.sendall(VarInt.write(1) + VarInt.write(0x00))

        # 3) Status Response
        _read_varint_sock(sock)             # 整包长度
        packet_id = _read_varint_sock(sock)
        if packet_id != 0x00:
            raise ValueError(f"非预期包 ID: 0x{packet_id:02X}")
        str_len = _read_varint_sock(sock)
        if str_len <= 0 or str_len > 1 << 20:
            raise ValueError(f"JSON 长度异常: {str_len}")
        raw = _recv_exact(sock, str_len)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("状态响应不是 JSON 对象")
        return parsed


def classify(protocol: int | None) -> tuple[str, str]:
    """返回 (支持状态, 说明)"""
    if protocol is None:
        return "?", "服务器未返回协议号"
    name = PROTOCOL_VERSION_NAMES.get(protocol)
    family = chunk_format_family(protocol)
    if any(lo <= protocol <= hi for lo, hi in _SUPPORTED_RANGES):
        label = name or f"未知（{protocol}）"
        return "✅ 可抓取", f"{label} · {family} 家族"
    if protocol < 47:
        return "❌ 太旧", f"协议 {protocol} 早于 1.8，无解析器"
    if 107 <= protocol <= 578:
        return "❌ 无解析器", f"协议 {protocol}（1.9~1.15.2）暂无区块解析器"
    if 775 <= protocol <= 776:
        return "⚠️ 未验证", f"协议 {protocol}（{name or '?'}）会走结构探测"
    return "⚠️ 未验证", f"协议 {protocol} 未知，将走结构探测"


def probe_login_protocol(
    host: str,
    port: int,
    protocol: int,
    timeout: float = TIMEOUT,
) -> str:
    """
    用**指定协议号**发起一次真实登录（只到 Login Start 之后的第一个响应），
    用来验证服务端在线上实际接受哪种格式。

    这对「装了 ViaVersion / 代理转发」的服务器特别有用：服务器列表里显示的
    版本名可能是核心版本，但线上实际走的是另一套协议。

    返回可读结论；失败时返回带原因的字符串。
    """
    import random
    import string

    name = "ProxyProbe" + "".join(random.choices(string.ascii_lowercase, k=3))
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            host_bytes = host.encode("utf-8")

            # Handshake: next_state = 2 (login)
            body = (
                VarInt.write(0x00)
                + VarInt.write(protocol)
                + VarInt.write(len(host_bytes)) + host_bytes
                + struct.pack(">H", port)
                + VarInt.write(2)
            )
            sock.sendall(VarInt.write(len(body)) + body)

            # Login Start: name(String) + UUID(16B, 1.19.3+/1.20.2+ 有)
            if protocol >= 764:
                uuid_bytes = bytes(random.getrandbits(8) for _ in range(16))
                ls = VarInt.write(0x00) + VarInt.write(len(name)) + name.encode() + uuid_bytes
            else:
                ls = VarInt.write(0x00) + VarInt.write(len(name)) + name.encode()
            sock.sendall(VarInt.write(len(ls)) + ls)

            # 读第一个响应包（不做完整解析，只看能否拿到包）
            try:
                total = _read_varint_sock(sock)
            except Exception as exc:
                return f"❌ 无响应（{exc}）"
            if total <= 0:
                return "❌ 收到空包"
            packet_id = _read_varint_sock(sock)
            rest = max(0, total - 1)
            payload = _recv_exact(sock, rest) if rest else b""

            # 登录阶段 clientbound: 0x00=Disconnect, 0x01=Encryption Request,
            #                       0x02=Login Success, 0x03=Set Compression
            if packet_id == 0x00:
                try:
                    text = _decode_string(payload)
                except Exception:
                    text = ""
                low = text.lower()
                if "outdated" in low or "version" in low or "protocol" in low:
                    return f"⚠️ 版本不匹配（服务器拒绝协议 {protocol}）：{text[:80]}"
                return f"❌ 被断开：{text[:100]}"
            if packet_id == 0x01:
                return "🔐 要求加密（online-mode=true）→ 本代理无法处理"
            if packet_id == 0x02:
                return "✅ 接受该协议号（Login Success）"
            if packet_id == 0x03:
                return "✅ 接受该协议号（Set Compression）"
            return f"✅ 有响应（clientbound 0x{packet_id:02X}）"
    except TimeoutError:
        return "❌ 连接超时"
    except (socket.gaierror, OSError) as exc:
        return f"❌ 无法连接：{exc}"
    except Exception as exc:
        return f"❌ 探测失败：{exc}"


def _decode_string(data: bytes) -> str:
    """读一个 VarInt 长度前缀的 UTF-8 字符串"""
    length = 0
    for i in range(5):
        if i >= len(data):
            break
        byte = data[i]
        length |= (byte & 0x7F) << (7 * i)
        if (byte & 0x80) == 0:
            return data[i + 1:i + 1 + length].decode("utf-8", errors="replace")
    return ""


def describe_motd(description: object) -> str:
    """MOTD 可能是字符串，也可能是 chat component 对象"""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        text = description.get("text", "")
        extra = description.get("extra") or []
        for item in extra:
            if isinstance(item, dict):
                text += item.get("text", "")
            elif isinstance(item, str):
                text += item
        return str(text)
    return ""


def check(addr: str, probe_login: bool = False, protocol: int | None = None) -> bool:
    """检查单个地址，返回是否可抓取"""
    host, port = parse_address(addr)
    print(f"\n{'=' * 66}")
    print(f"  目标: {host}:{port}")
    print(f"{'=' * 66}")

    try:
        data = query_status(host, port)
    except TimeoutError:
        print("  ❌ 连接超时（端口可能未开放或被防火墙拦截）")
        return False
    except (socket.gaierror, OSError) as exc:
        print(f"  ❌ 无法连接: {exc}")
        return False
    except Exception as exc:
        print(f"  ❌ 查询失败: {exc}")
        return False

    version = data.get("version") or {}
    players = data.get("players") or {}
    reported = version.get("protocol")
    if not isinstance(reported, int):
        reported = None

    motd = describe_motd(data.get("description"))
    online = players.get("online")
    maximum = players.get("max")

    print(f"  MOTD      : {motd!r}")
    print(f"  版本名    : {version.get('name', '?')}")
    print(f"  协议号    : {reported if reported is not None else '?'}")
    if online is not None and maximum is not None:
        print(f"  在线人数  : {online} / {maximum}")

    status, detail = classify(reported)
    print(f"  抓取支持  : {status}  {detail}")

    # 真实登录探测：验证线上实际接受的协议号（对 ViaVersion / 代理转发很有用）
    if probe_login:
        target_proto = protocol if protocol is not None else reported
        if target_proto is None:
            print("  登录探测  : 跳过（未知协议号，可用 --protocol 指定）")
        else:
            result = probe_login_protocol(host, port, target_proto)
            print(f"  登录探测  : 协议 {target_proto} -> {result}")
            if "要求加密" in result:
                print("\n  → 该服为 online-mode=true，本代理无法处理。")
                return False

    if status.startswith("✅"):
        print("\n  → 可以用于抓取。注意仍需确认服务器为 offline-mode（离线验证）。")
        return True

    print("\n  → 不建议用于抓取（见上面的原因）。")
    return False


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2

    probe_login = "--probe-login" in args
    protocol: int | None = None
    if "--protocol" in args:
        idx = args.index("--protocol")
        if idx + 1 < len(args):
            try:
                protocol = int(args[idx + 1])
            except ValueError:
                print(f"  ❌ --protocol 需要整数，得到 {args[idx + 1]!r}")
                return 2
    targets = [a for a in args if not a.startswith("--") and a != str(protocol)]
    if not targets:
        print(__doc__)
        return 2

    results = [check(addr, probe_login=probe_login, protocol=protocol) for addr in targets]

    print(f"\n{'=' * 66}")
    ok = sum(1 for r in results if r)
    print(f"  汇总: {ok} / {len(results)} 个目标可用于抓取")
    print(f"{'=' * 66}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
