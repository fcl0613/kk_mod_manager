"""Mod 详情面板（F05-5）

需求文档 v1.3:
  - F05-5: 选中 Mod 后右侧展示详情（GUID/描述/网站/完整路径）
  - F06-1/2: 卸载/移除按钮
  - F08-1/2: 启用/禁用按钮
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QScrollArea,
    QMessageBox,
)
from PyQt6.QtCore import Qt, pyqtSignal

from app.models.mod import Mod, ModLocation, ModStatus
from app.config import get_config


class ModDetailPanel(QWidget):
    """Mod 详情面板"""

    # 信号
    uninstall_requested = pyqtSignal(int)           # mod_id
    enable_requested = pyqtSignal(int, bool)        # mod_id, enabled
    install_to_game_requested = pyqtSignal(int)     # mod_id（从仓库安装到游戏）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_mod: Mod | None = None
        self._setup_ui()

    def _setup_ui(self):
        self.setObjectName("detailPanel")
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # 标题
        title = QLabel("Mod 详情")
        title.setObjectName("title")
        layout.addWidget(title)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #45475a;")
        layout.addWidget(line)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setSpacing(8)

        # 字段标签
        self.fields: dict[str, QLabel] = {}
        for key in ["name", "guid", "version", "author", "description",
                     "website", "file_name", "location", "status",
                     "installed_at", "full_path"]:
            label = QLabel("")
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.fields[key] = label
            self.content_layout.addWidget(label)

        self.content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        # 操作按钮区
        btn_frame = QFrame()
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setSpacing(8)

        self.enable_btn = QPushButton("启用")
        self.enable_btn.setObjectName("success")
        self.enable_btn.clicked.connect(lambda: self._on_enable(True))
        self.enable_btn.setVisible(False)
        btn_layout.addWidget(self.enable_btn)

        self.disable_btn = QPushButton("禁用")
        self.disable_btn.clicked.connect(lambda: self._on_enable(False))
        self.disable_btn.setVisible(False)
        btn_layout.addWidget(self.disable_btn)

        self.install_game_btn = QPushButton("安装到游戏")
        self.install_game_btn.setObjectName("primary")
        self.install_game_btn.clicked.connect(self._on_install_to_game)
        self.install_game_btn.setVisible(False)
        btn_layout.addWidget(self.install_game_btn)

        self.uninstall_btn = QPushButton("卸载")
        self.uninstall_btn.setObjectName("danger")
        self.uninstall_btn.clicked.connect(self._on_uninstall)
        self.uninstall_btn.setVisible(False)
        btn_layout.addWidget(self.uninstall_btn)

        btn_layout.addStretch()
        layout.addWidget(btn_frame)

    def show_mod(self, mod: Mod):
        """展示 Mod 详情"""
        self._current_mod = mod
        config = get_config()

        # 基本信息
        self.fields["name"].setText(f"<b>名称：</b>{mod.name}")
        self.fields["guid"].setText(f"<b>GUID：</b>{mod.guid}")
        self.fields["version"].setText(f"<b>版本：</b>{mod.version}")
        self.fields["author"].setText(f"<b>作者：</b>{mod.author or 'Unknown'}")
        self.fields["description"].setText(
            f"<b>描述：</b>{mod.description or '(无)'}"
        )
        self.fields["website"].setText(
            f"<b>网站：</b>{mod.website or '(无)'}"
        )

        # 文件信息
        self.fields["file_name"].setText(f"<b>文件名：</b>{mod.file_name}")

        # 位置
        loc_text = "游戏已装" if mod.location == ModLocation.GAME else "仓库"
        self.fields["location"].setText(f"<b>位置：</b>{loc_text}")

        # 状态
        status_text = "✅ 已启用" if mod.status == ModStatus.ENABLED else "⛔ 已禁用"
        self.fields["status"].setText(f"<b>状态：</b>{status_text}")

        # 时间
        installed = mod.installed_at.strftime("%Y-%m-%d %H:%M") if mod.installed_at else "未知"
        self.fields["installed_at"].setText(f"<b>安装时间：</b>{installed}")

        # 完整路径
        base_path = (
            config.game_mod_path if mod.location == ModLocation.GAME
            else config.repo_path
        )
        full_path = f"{base_path}/{mod.author or 'Unknown'}/{mod.current_file_name}"
        self.fields["full_path"].setText(f"<b>完整路径：</b>{full_path}")

        # 按钮显示
        is_game = mod.location == ModLocation.GAME
        self.enable_btn.setVisible(is_game and mod.status == ModStatus.DISABLED)
        self.disable_btn.setVisible(is_game and mod.status == ModStatus.ENABLED)
        self.install_game_btn.setVisible(not is_game)
        self.uninstall_btn.setVisible(True)

        if is_game:
            self.uninstall_btn.setText("卸载")
        else:
            self.uninstall_btn.setText("从仓库移除")

    def clear(self):
        """清空详情"""
        self._current_mod = None
        for label in self.fields.values():
            label.setText("")
        self.enable_btn.setVisible(False)
        self.disable_btn.setVisible(False)
        self.install_game_btn.setVisible(False)
        self.uninstall_btn.setVisible(False)

    def _on_enable(self, enabled: bool):
        if self._current_mod is None:
            return
        self.enable_requested.emit(self._current_mod.id, enabled)

    def _on_uninstall(self):
        if self._current_mod is None:
            return

        config = get_config()
        if config.confirm_before_uninstall:
            loc_text = "卸载" if self._current_mod.location == ModLocation.GAME else "移除"
            reply = QMessageBox.question(
                self,
                f"确认{loc_text}",
                f"确定要{loc_text}以下 Mod 吗？\n\n"
                f"名称：{self._current_mod.name}\n"
                f"版本：{self._current_mod.version}\n"
                f"作者：{self._current_mod.author or 'Unknown'}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.uninstall_requested.emit(self._current_mod.id)

    def _on_install_to_game(self):
        """从仓库安装到游戏"""
        if self._current_mod is None:
            return

        # 确认对话框
        reply = QMessageBox.question(
            self,
            "安装到游戏",
            f"确定要将以下 Mod 从仓库安装到游戏吗？\n\n"
            f"名称：{self._current_mod.name}\n"
            f"版本：{self._current_mod.version}\n"
            f"作者：{self._current_mod.author or 'Unknown'}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.install_to_game_requested.emit(self._current_mod.id)
