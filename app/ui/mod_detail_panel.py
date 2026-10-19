"""Mod 详情面板（F05-5）

需求文档 v1.3:
  - F05-5: 选中 Mod 后右侧展示详情（GUID/描述/网站/完整路径）
  - F06-1/2: 卸载/移除按钮
  - F08-1/2: 启用/禁用按钮
  - F12-1: zipmod 内缩略图展示（点击放大，见 ThumbnailService）
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QScrollArea,
    QMessageBox,
)
from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QCursor, QPixmap

from app.models.mod import Mod, ModLocation, ModStatus
from app.config import get_config
from app.services.thumbnail_service import ThumbnailService
from app.ui.thumbnail_viewer_dialog import ThumbnailViewerDialog
from app.utils.file_utils import (
    resolve_game_mod_path,
    resolve_repo_mod_path,
)


class ClickableLabel(QLabel):
    """可点击的 QLabel（缩略图点击放大用）"""

    clicked = pyqtSignal()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(ev)


class ModDetailPanel(QWidget):
    """Mod 详情面板"""

    # 信号
    uninstall_requested = pyqtSignal(int)           # mod_id
    enable_requested = pyqtSignal(int, bool)        # mod_id, enabled
    install_to_game_requested = pyqtSignal(int)     # mod_id（从仓库安装到游戏）
    add_to_repo_requested = pyqtSignal(int)         # mod_id（从游戏添加到仓库）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_mod: Mod | None = None
        self._is_in_game: bool = False   # 仓库mod是否已在游戏中
        self._is_in_repo: bool = False   # 游戏mod是否已在仓库中
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

        # 缩略图区域（F12-1：zipmod 内提取，无图时整体隐藏不占位）
        self._thumb_service = ThumbnailService()
        self._thumb_pixmap: QPixmap | None = None
        self._thumb_entry: str = ""
        self.thumbnail_label = ClickableLabel()
        self.thumbnail_label.setObjectName("modThumbnail")
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setFixedHeight(220)
        self.thumbnail_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.thumbnail_label.clicked.connect(self._show_thumbnail_viewer)
        self.thumbnail_label.setVisible(False)
        self.content_layout.addWidget(self.thumbnail_label)

        # 字段标签
        self.fields: dict[str, QLabel] = {}
        for key in ["name", "guid", "version", "author", "description",
                     "website", "file_name", "location", "status",
                     "installed_at", "full_path", "repo_relative_path",
                     "game_relative_path"]:
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

        self.add_to_repo_btn = QPushButton("加入本地仓库")
        self.add_to_repo_btn.setObjectName("primary")
        self.add_to_repo_btn.clicked.connect(self._on_add_to_repo)
        self.add_to_repo_btn.setVisible(False)
        btn_layout.addWidget(self.add_to_repo_btn)

        self.uninstall_btn = QPushButton("卸载")
        self.uninstall_btn.setObjectName("danger")
        self.uninstall_btn.clicked.connect(self._on_uninstall)
        self.uninstall_btn.setVisible(False)
        btn_layout.addWidget(self.uninstall_btn)

        btn_layout.addStretch()
        layout.addWidget(btn_frame)

    def show_mod(self, mod: Mod, is_in_game: bool = False, is_in_repo: bool = False):
        """展示 Mod 详情

        Args:
            mod: 要展示的 Mod 对象
            is_in_game: （仓库mod时）是否已安装到游戏中
            is_in_repo: （游戏mod时）是否已存在于仓库中
        """
        self._current_mod = mod
        self._is_in_game = is_in_game
        self._is_in_repo = is_in_repo
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

        # 完整路径（GAME 端 rel path 优先定位，F19-6/9）
        full_path = str(self._resolve_mod_file_path(mod, config))
        self.fields["full_path"].setText(f"<b>完整路径：</b>{full_path}")

        # 仓库相对路径（仅 REPO 记录有意义）
        if mod.location == ModLocation.REPO:
            if mod.repo_relative_path:
                self.fields["repo_relative_path"].setText(
                    f"<b>仓库相对路径：</b>{mod.repo_relative_path}"
                )
            else:
                self.fields["repo_relative_path"].setText(
                    "<b>仓库相对路径：</b><span style='color:#888'>(未记录，按作者目录定位)</span>"
                )
            self.fields["repo_relative_path"].setVisible(True)
        else:
            self.fields["repo_relative_path"].setVisible(False)

        # 游戏相对路径（仅 GAME 记录有意义，F19-9）
        if mod.location == ModLocation.GAME:
            if mod.game_relative_path:
                self.fields["game_relative_path"].setText(
                    f"<b>游戏相对路径：</b>{mod.game_relative_path}"
                )
            else:
                self.fields["game_relative_path"].setText(
                    "<b>游戏相对路径：</b><span style='color:#888'>(未记录，按作者目录定位)</span>"
                )
            self.fields["game_relative_path"].setVisible(True)
        else:
            self.fields["game_relative_path"].setVisible(False)

        # 按钮显示
        is_game = mod.location == ModLocation.GAME
        self.enable_btn.setVisible(is_game and mod.status == ModStatus.DISABLED)
        self.disable_btn.setVisible(is_game and mod.status == ModStatus.ENABLED)
        # 仓库 Mod：未安装到游戏时才显示"安装到游戏"
        self.install_game_btn.setVisible(not is_game and not is_in_game)
        # 游戏 Mod：仓库中不存在时才显示"加入本地仓库"
        self.add_to_repo_btn.setVisible(is_game and not is_in_repo)
        self.uninstall_btn.setVisible(True)

        if is_game:
            self.uninstall_btn.setText("卸载")
        else:
            self.uninstall_btn.setText("从仓库移除")

        # 缩略图（F12-1）
        self._load_thumbnail(Path(full_path))

    def clear(self):
        """清空详情"""
        self._current_mod = None
        self._is_in_game = False
        self._is_in_repo = False
        for label in self.fields.values():
            label.setText("")
        self.enable_btn.setVisible(False)
        self.disable_btn.setVisible(False)
        self.install_game_btn.setVisible(False)
        self.add_to_repo_btn.setVisible(False)
        self.uninstall_btn.setVisible(False)
        # 缩略图（F12-1）
        self._thumb_pixmap = None
        self._thumb_entry = ""
        self.thumbnail_label.setVisible(False)
        self.thumbnail_label.setPixmap(QPixmap())   # 清掉旧图，避免残留

    def _resolve_mod_file_path(self, mod: Mod, config) -> Path:
        """解析 Mod 的 zipmod 文件实际路径（GAME/REPO 各自 rel path 优先，
        回退作者目录定位；.zipmod_disabled 由 current_file_name 处理）"""
        if mod.location == ModLocation.GAME:
            return resolve_game_mod_path(mod, Path(config.game_mod_path))
        return resolve_repo_mod_path(mod, Path(config.repo_path))

    def _load_thumbnail(self, path: Path):
        """加载并展示 zipmod 内缩略图（F12-1，best-effort，失败仅隐藏）"""
        # 先重置，避免快速切换选中时残留上一张
        self._thumb_pixmap = None
        self._thumb_entry = ""
        self.thumbnail_label.setPixmap(QPixmap())

        thumb = self._thumb_service.extract(path)
        if thumb is None:
            self.thumbnail_label.setVisible(False)
            return

        pm = QPixmap()
        if not pm.loadFromData(thumb.data) or pm.isNull():
            # 解码失败（格式插件缺失等）→ 静默隐藏
            self.thumbnail_label.setVisible(False)
            return

        self._thumb_pixmap = pm
        self._thumb_entry = thumb.entry_name
        # 固定缩放盒：show_mod 时 label 尚未布局，width() 不可靠
        self.thumbnail_label.setPixmap(pm.scaled(
            QSize(320, 208),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self.thumbnail_label.setVisible(True)

    def _show_thumbnail_viewer(self):
        """点击缩略图 → 大图查看器（原始分辨率）"""
        if self._thumb_pixmap is None or self._thumb_pixmap.isNull():
            return
        mod_name = self._current_mod.name if self._current_mod else ""
        ThumbnailViewerDialog(
            self._thumb_pixmap,
            entry_name=self._thumb_entry,
            mod_name=mod_name,
            parent=self,
        ).exec()

    def _on_enable(self, enabled: bool):
        if self._current_mod is None:
            return
        self.enable_requested.emit(self._current_mod.id, enabled)

    def _on_uninstall(self):
        if self._current_mod is None:
            return

        config = get_config()
        if config.confirm_before_uninstall:
            is_repo = self._current_mod.location == ModLocation.REPO
            if is_repo and self._is_in_game:
                # 仓库 Mod 且已安装到游戏中 → 特殊提示
                reply = QMessageBox.question(
                    self,
                    "确认删除",
                    "当前 Mod 已安装到游戏中，是否继续删除仓库中的 Mod？\n\n"
                    f"名称：{self._current_mod.name}\n"
                    f"版本：{self._current_mod.version}\n"
                    f"作者：{self._current_mod.author or 'Unknown'}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
            elif is_repo:
                # 仓库 Mod 未安装到游戏中 → 普通确认
                reply = QMessageBox.question(
                    self,
                    "确认删除",
                    f"是否确认删除该 Mod？\n\n"
                    f"名称：{self._current_mod.name}\n"
                    f"版本：{self._current_mod.version}\n"
                    f"作者：{self._current_mod.author or 'Unknown'}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
            else:
                # 游戏 Mod → 卸载确认
                reply = QMessageBox.question(
                    self,
                    "确认卸载",
                    f"确定要卸载以下 Mod 吗？\n\n"
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

    def _on_add_to_repo(self):
        """从游戏添加到本地仓库"""
        if self._current_mod is None:
            return

        reply = QMessageBox.question(
            self,
            "加入本地仓库",
            f"确定要将以下 Mod 从游戏加入到本地仓库吗？\n\n"
            f"名称：{self._current_mod.name}\n"
            f"版本：{self._current_mod.version}\n"
            f"作者：{self._current_mod.author or 'Unknown'}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.add_to_repo_requested.emit(self._current_mod.id)
