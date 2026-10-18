# -*- coding: utf-8 -*-
"""操作日志非模态对话框（F10-3）。

形态沿革：顶层第三页签 → 底部停靠面板 → 独立对话框。停靠面板的浮动
QDockWidget 在 Qt6/Windows 下没有可靠的「双击标题栏回坞」入口（浮动后
是系统原生标题栏，Qt 收不到双击），改为普通对话框：默认居中于主窗口
弹出，✕ / Esc 隐藏（实例保留，本次会话内重开仍在原位、保留过滤/分页
状态），不持久化几何。
"""

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QWidget

from app.services.mod_service import ModService
from app.ui.operation_log_page import OperationLogPage


class LogDialog(QDialog):
    """📜 操作日志对话框：非模态、属于主窗口（悬于其上、不占任务栏）"""

    visibility_changed = pyqtSignal(bool)

    def __init__(self, mod_service: ModService, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("📜 操作日志")
        self.resize(820, 520)
        self.setMinimumSize(480, 280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.log_page = OperationLogPage(mod_service)
        layout.addWidget(self.log_page)

        # 对话框内 F5 刷新（主窗口的 F5 只作用于当前主页面）
        sc = QShortcut(QKeySequence("F5"), self)
        sc.activated.connect(self.log_page.refresh)

    def showEvent(self, event):
        """每次显示拉取最新日志（✕ 关闭后重开等）"""
        self.log_page.refresh()
        self.visibility_changed.emit(True)
        super().showEvent(event)

    def hideEvent(self, event):
        """隐藏（✕ / Esc）：通知主窗口同步工具栏按钮勾选态"""
        self.visibility_changed.emit(False)
        super().hideEvent(event)
