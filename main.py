"""
MinecraftProxy —— Minecraft 协议代理（单文件程序入口）

在客户端与服务器之间做字节透明转发，同时旁路解析区块数据包，
把世界导出为可被 Minecraft 直接加载的 .mca 区域文件。

用法:
    python main.py --target mc.example.com:25565
    python main.py --target 127.0.0.1:25565 --world-dir ./my_world --debug

依赖: 仅标准库（Python >= 3.10）
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# 把程序所在目录加入 sys.path，保证从任意 cwd 运行都能找到 core 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 以下导入必须在上面的 sys.path 设置之后（否则从其它 cwd 运行会找不到 core）
from core.logger import set_global_level, setup_logger  # noqa: E402
from core.proxy import MinecraftProxy  # noqa: E402

# 用程序名作为日志前缀，避免出现 "__main__" 这种不专业的名字
logger = setup_logger("MinecraftProxy")

DEFAULT_PORT = 25565


def parse_server_address(addr):
    """解析 "host" 或 "host:port"，返回 (host, port)；端口默认 25565。"""
    if ":" in addr:
        host, port_str = addr.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError as exc:
            raise ValueError(f"无效端口: {port_str}") from exc
        return host, port
    return addr, DEFAULT_PORT


def interactive_input(prompt, default="", required=True):
    """交互式输入，支持默认值"""
    if default:
        prompt = f"{prompt} (默认: {default})"
    while True:
        value = input(prompt + ": ").strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print("输入不能为空，请重新输入。")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="MinecraftProxy",
        description="Minecraft 协议代理：拦截流量并把区块导出为可加载的 .mca 世界",
    )
    parser.add_argument("--local-host", default=None, help="本地监听地址 (默认: 127.0.0.1)")
    parser.add_argument("--local-port", type=int, default=None, help="本地监听端口 (默认: 25565)")
    parser.add_argument(
        "--target",
        default=None,
        help="目标服务器地址，格式: host[:port] (例如: mc.example.com:25565)",
    )
    parser.add_argument(
        "--world-dir",
        default="./downloaded_world",
        help="保存世界文件的目录 (默认: ./downloaded_world)",
    )
    parser.add_argument(
        "--block-map",
        default=None,
        help="方块数据文件 (blocks.json)，用于把方块状态 ID 转成方块名称",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="禁止自动下载方块数据表（离线环境使用）",
    )
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    parser.add_argument(
        "--max-connections",
        type=int,
        default=8,
        help="同时允许的客户端连接数上限 (默认: 8)",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="不进行交互式提问（缺少参数时直接报错，适合脚本调用）",
    )
    return parser


def _resolve_target(args, no_prompt):
    """确定目标地址字符串"""
    if isinstance(args.target, str) and args.target:
        return args.target
    if no_prompt:
        return None
    return interactive_input("目标服务器地址 (格式: host[:port])", default="localhost")


def install_signal_handlers(loop, callback):
    """
    安装 SIGINT / SIGTERM 处理器，返回已安装列表 [(signal, kind)]。

    优先用 loop.add_signal_handler（POSIX，可在事件循环中安全回调）；
    Windows 不支持该 API，退回 signal.signal（仅主线程可用）。
    """
    installed: list[tuple[int, str]] = []
    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        try:
            loop.add_signal_handler(sig, callback, name)
            installed.append((sig, "loop"))
        except (NotImplementedError, AttributeError, ValueError, RuntimeError):
            try:
                signal.signal(sig, lambda *_, _n=name: callback(_n))
                installed.append((sig, "signal"))
            except (ValueError, OSError):
                pass
    return installed


def remove_signal_handlers(loop, installed) -> None:
    """恢复之前安装的信号处理器"""
    for sig, kind in installed:
        if kind == "loop":
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, AttributeError, ValueError):
                pass
        else:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (ValueError, OSError):
                pass


async def run(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    no_prompt = args.no_prompt

    print("\n=== Minecraft 代理配置 ===\n")

    local_host = args.local_host
    if local_host is None:
        if no_prompt:
            local_host = "127.0.0.1"
        else:
            local_host = (
                interactive_input("本地监听地址", default="127.0.0.1", required=False)
                or "127.0.0.1"
            )

    local_port = args.local_port
    if local_port is None:
        if no_prompt:
            local_port = DEFAULT_PORT
        else:
            local_port = int(interactive_input("本地监听端口", default=str(DEFAULT_PORT)))

    target_addr = _resolve_target(args, no_prompt)
    if not target_addr:
        logger.error("未指定目标服务器地址（使用 --target host[:port]）")
        return 2

    try:
        target_host, target_port = parse_server_address(target_addr)
    except ValueError as exc:
        logger.error(f"服务器地址解析失败: {exc}")
        return 2

    if args.debug:
        set_global_level(logging.DEBUG)
        logger.info("已启用调试日志")

    # 创建世界目录（模组维度会在运行时按需创建）
    world_path = Path(args.world_dir)
    world_path.mkdir(parents=True, exist_ok=True)
    for dim in ("", "DIM-1", "DIM1"):
        (world_path / dim / "region").mkdir(parents=True, exist_ok=True)

    logger.info(f"世界目录: {world_path.absolute()}")
    logger.info(f"代理监听: {local_host}:{local_port}")
    logger.info(f"目标服务器: {target_addr}")

    proxy = MinecraftProxy(
        local_host=local_host,
        local_port=local_port,
        target_host=target_host,
        target_port=target_port,
        target_addr_display=target_addr,
        world_dir=args.world_dir,
        debug=args.debug,
        block_map=args.block_map,
        allow_download=not args.no_download,
        max_sessions=max(1, args.max_connections),
    )

    # 优雅关闭：收到 SIGINT/SIGTERM 时取消 serve_forever，进入 finally 做收尾
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(signame: str) -> None:
        logger.info(f"收到 {signame}，正在关闭...")
        stop_event.set()

    installed_signals = install_signal_handlers(loop, _request_stop)

    proxy_task = asyncio.create_task(proxy.run(), name="proxy")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    try:
        done, _ = await asyncio.wait(
            {proxy_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not proxy_task.done():
            proxy_task.cancel()
        for task in (proxy_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(proxy_task, stop_task, return_exceptions=True)
        # 代理自身异常退出（而非收到信号）时视为失败
        if proxy_task in done and stop_task not in done and not proxy_task.cancelled():
            error = proxy_task.exception()
            if error is not None:
                raise error
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    except Exception as exc:
        logger.error(f"代理运行异常: {exc}", exc_info=True)
        return 1
    finally:
        remove_signal_handlers(loop, installed_signals)
        await proxy.shutdown()
        logger.info("代理已关闭")
    return 0


def main(argv=None):
    try:
        return asyncio.run(run(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
