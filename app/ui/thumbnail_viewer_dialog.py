"""Mod 缩略图大图查看器（F12-1）

需求文档 v1.3 §4.13:
  - F12-1: 详情面板缩略图点击后弹出的原始分辨率查看器
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout,
    QLabel, QPushButton,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QGuiApplication


class ThumbnailViewerDialog(QDialog):
    """缩略图大图查看对话框（原始分辨率，超屏则缩至可用区域 85%）"""

    def __init__(
        self,
        pixmap: QPixmap,
        entry_name: str,
        mod_name: str,
        parent=None,
    ):
        super().__init__(parent)
        self._pixmap = pixmap
        self._entry_name = entry_name
        self._mod_name = mod_name
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle(f"{self._mod_name} - 缩略图")
        self.resize(900, 700)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # ── 图片区 ──
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(
            "background-color: #11111b; border: 1px solid #313244; border-radius: 4px;"
        )
        layout.addWidget(self.image_label, stretch=1)
        self._load_image()

        # ── 来源条目名 ──
        caption = QLabel(f"来源：{self._entry_name}")
        caption.setObjectName("subtitle")
        caption.setWordWrap(True)
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        caption.setStyleSheet("color: #888;")
        layout.addWidget(caption)

        # ── 关闭按钮 ──
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def _load_image(self):
        if self._pixmap is None or self._pixmap.isNull():
            self.image_label.setText("图片加载失败")
            return

        # 原始分辨率展示；超过屏幕可用区域 85% 时保比例缩小
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            max_size = screen.availableGeometry().size() * 0.85
            if (
                self._pixmap.width() > max_size.width()
                or self._pixmap.height() > max_size.height()
            ):
                self._pixmap = self._pixmap.scaled(
                    max_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        self.image_label.setPixmap(self._pixmap)
