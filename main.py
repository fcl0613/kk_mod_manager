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
from app.logger import setup_logging


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
