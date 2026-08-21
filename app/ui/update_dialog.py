"""新版本通知对话框

发现新版本时弹出，展示：
  - 当前版本 → 最新版本对比
  - 变更日志（Markdown 渲染）
  - "前往下载"按钮（打开 GitHub release 页面）
  - "稍后提醒"按钮（关闭）
"""

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QTextBrowser,
)

from app.services.update_service import UpdateResult, UpdateStatus


class UpdateDialog(QDialog):
    """新版本通知对话框"""

    def __init__(self, result: UpdateResult, parent=None):
        super().__init__(parent)
        self._result = result
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(520)
        self.setMinimumHeight(360)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── 标题 ──
        title = QLabel("🎉 发现新版本")
        title.setObjectName("title")
        layout.addWidget(title)

        # ── 分隔线 ──
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #45475a;")
        layout.addWidget(line)

        # ── 版本对比 ──
        version_label = QLabel(
            f"当前版本：<b>v{self._result.current}</b><br>"
            f"最新版本：<b style='color:#a6e3a1;'>v{self._result.latest}</b>"
        )
        version_label.setTextFormat(Qt.TextFormat.RichText)
        version_label.setWordWrap(True)
        layout.addWidget(version_label)

        # ── 变更日志 ──
        changelog_label = QLabel("📋 变更日志：")
        layout.addWidget(changelog_label)

        self.changelog_view = QTextBrowser()
        self.changelog_view.setOpenExternalLinks(True)
        if self._result.changelog:
            # setMarkdown 支持 GitHub release body 的基础 Markdown
            self.changelog_view.setMarkdown(self._result.changelog)
        else:
            self.changelog_view.setPlainText("（该版本未提供变更日志）")
        self.changelog_view.setMinimumHeight(180)
        layout.addWidget(self.changelog_view, 1)

        # ── 按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        later_btn = QPushButton("稍后提醒")
        later_btn.clicked.connect(self.reject)
        btn_layout.addWidget(later_btn)

        download_btn = QPushButton("🚀 前往下载")
        download_btn.setObjectName("primary")
        download_btn.setDefault(True)
        download_btn.clicked.connect(self._open_release_url)
        btn_layout.addWidget(download_btn)

        layout.addLayout(btn_layout)

    def _open_release_url(self):
        """打开 GitHub release 页面"""
        url = self._result.release_url
        if url:
            QDesktopServices.openUrl(QUrl(url))
        self.accept()
