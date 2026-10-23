"""
日志模块
配置统一的日志格式和输出
"""

import logging
import sys


def setup_logger(
    name: str | None = None,
    level: int = logging.INFO,
    log_file: str | None = None
) -> logging.Logger:
    """
    配置并返回日志记录器

    参数:
        name: 日志记录器名称
        level: 日志级别
        log_file: 日志文件路径（可选）

    返回:
        配置好的 Logger 实例
    """
    # 创建日志记录器
    logger = logging.getLogger(name if name else __name__)
    logger.setLevel(level)
    logger.propagate = False

    # 清空现有的处理器
    logger.handlers.clear()

    # 配置格式
    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件处理器（可选）
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """获取已配置的日志记录器"""
    return logging.getLogger(name)


def set_global_level(level: int) -> None:
    """
    统一调整所有已创建日志记录器（含其处理器）的级别。
    用于在运行时开启/关闭 DEBUG 输出。
    """
    for logger_obj in logging.Logger.manager.loggerDict.values():
        if not isinstance(logger_obj, logging.Logger):
            continue
        logger_obj.setLevel(level)
        for handler in logger_obj.handlers:
            handler.setLevel(level)
