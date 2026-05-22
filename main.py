"""KK Mod Manager — 入口文件

Illusion 系列游戏（Koikatsu/KK）桌面端 Mod 管理工具。

用法:
    python main.py

要求:
    - Python 3.11+
    - .venv 虚拟环境中运行
    - pip install -r requirements.txt
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from PyQt6.QtWidgets import QApplication
from loguru import logger

from app.ui.main_window import MainWindow
from app.config import get_config


def setup_logging():
    """配置日志（按日期滚动，存储在 data/logs/）"""
    import sys as _sys

    # 确定日志目录
    if getattr(_sys, 'frozen', False):
        log_dir = Path(_sys.executable).parent / "logs"
    else:
        log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # 移除默认 handler
    logger.remove()

    # 控制台输出（开发模式）
    logger.add(
        _sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )

    # 文件输出（按天滚动）
    logger.add(
        log_dir / "kk_mod_manager_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="00:00",    # 每天午夜滚动
        retention="30 days",  # 保留 30 天
        encoding="utf-8",
    )

    logger.info("KK Mod Manager 启动")


def main():
    """应用主入口"""
    setup_logging()

    try:
        app = QApplication(sys.argv)
        app.setApplicationName("KK Mod Manager")
        app.setOrganizationName("KKModManager")

        # 高 DPI 支持
        app.setStyle("Fusion")

        # 加载配置中的主题
        config = get_config()
        if config.theme == "light":
            app.setStyleSheet("")  # 使用系统默认亮色主题

        window = MainWindow()
        window.show()

        logger.info("主窗口已显示")
        sys.exit(app.exec())

    except Exception as e:
        logger.exception(f"应用启动失败: {e}")
        raise


if __name__ == "__main__":
    main()
