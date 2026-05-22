"""日志模块

基于 loguru 的统一日志管理。

功能：
  - 控制台输出（INFO 级别，带颜色）
  - 文件输出（DEBUG 级别，按天滚动，保留 30 天）
  - 日志目录：exe 同级 / 项目根目录下的 logs/

用法：
    from app.logger import setup_logging

    setup_logging()
"""

import sys as _sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path | None = None):
    """配置 loguru 日志。

    Args:
        log_dir: 日志目录。为 None 时自动检测（exe 目录或项目根目录）。
    """
    # 确定日志目录
    if log_dir is None:
        if getattr(_sys, 'frozen', False):
            # PyInstaller 打包后：exe 同级目录
            log_dir = Path(_sys.executable).parent / "logs"
        else:
            # 开发模式：logger.py 所在目录的上两级（项目根目录）
            log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 移除默认 handler
    logger.remove()

    # 控制台输出（带颜色，开发友好）
    # 打包后 --windowed 模式没有 stderr，直接跳过控制台日志
    if _sys.stderr is not None:
        logger.add(
            _sys.stderr,
            level="INFO",
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
            colorize=True,
        )

    # 文件输出（完整调试信息，按天滚动）
    logger.add(
        log_dir / "kk_mod_manager_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="00:00",       # 每天午夜滚动
        retention="30 days",    # 保留 30 天
        encoding="utf-8",
    )

    logger.info("日志模块初始化完成，日志目录: {}", log_dir)
