"""设置对话框（F01 / F16）

需求文档 v1.3:
  - 游戏 Mod 路径配置（F01）
  - 本地仓库路径配置（F01）
  - 远程仓库 URL 配置（F16，P2，可选）
  - 行为配置：卸载前确认、清理空目录
  - 主题选择
  - 更新检查配置
"""

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QCheckBox, QComboBox,
    QGroupBox, QFormLayout, QFileDialog, QLabel
)

from app import __version__
from app.config import get_config, save_config
from app.services.update_service import RELEASES_PAGE_URL, is_newer_version


def _format_check_time(iso_str: str) -> str:
    """格式化 ISO 时间戳为友好显示"""
    if not iso_str:
        return "从未"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_str


class SettingsDialog(QDialog):
    """设置对话框"""

    # 手动检查更新信号（主窗口连接此信号触发实际检查）
    update_check_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._config = get_config()
        self._setup_ui()
        self._load_config()

    def _setup_ui(self):
        self.setWindowTitle("设置")
        self.setMinimumWidth(550)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── 路径配置 ──
        path_group = QGroupBox("路径配置")
        path_form = QFormLayout(path_group)

        # 游戏 Mod 路径
        game_layout = QHBoxLayout()
        self.game_path_edit = QLineEdit()
        self.game_path_edit.setPlaceholderText("选择游戏 Sideloader Mods 目录...")
        game_layout.addWidget(self.game_path_edit)
        self.game_browse_btn = QPushButton("浏览...")
        self.game_browse_btn.clicked.connect(self._browse_game_path)
        game_layout.addWidget(self.game_browse_btn)
        path_form.addRow("游戏 Mod 路径:", game_layout)

        # 仓库路径
        repo_layout = QHBoxLayout()
        self.repo_path_edit = QLineEdit()
        self.repo_path_edit.setPlaceholderText("选择本地 Mod 仓库目录...")
        repo_layout.addWidget(self.repo_path_edit)
        self.repo_browse_btn = QPushButton("浏览...")
        self.repo_browse_btn.clicked.connect(self._browse_repo_path)
        repo_layout.addWidget(self.repo_browse_btn)
        path_form.addRow("本地仓库路径:", repo_layout)

        layout.addWidget(path_group)

        # ── 远程仓库（P2）─ ─
        remote_group = QGroupBox("远程仓库（可选）")
        remote_form = QFormLayout(remote_group)
        self.remote_url_edit = QLineEdit()
        self.remote_url_edit.setPlaceholderText("https://example.com/mods/")
        remote_form.addRow("远程仓库 URL:", self.remote_url_edit)
        layout.addWidget(remote_group)

        # ── 行为设置 ──
        behavior_group = QGroupBox("行为设置")
        behavior_layout = QVBoxLayout(behavior_group)

        self.confirm_uninstall_cb = QCheckBox("卸载/移除前弹出确认对话框")
        self.confirm_uninstall_cb.setChecked(True)
        behavior_layout.addWidget(self.confirm_uninstall_cb)

        self.clean_empty_cb = QCheckBox("卸载/移除后自动清理空目录")
        self.clean_empty_cb.setChecked(True)
        behavior_layout.addWidget(self.clean_empty_cb)

        layout.addWidget(behavior_group)

        # ── 更新检查 ──
        update_group = QGroupBox("更新")
        update_layout = QVBoxLayout(update_group)

        # 版本信息表：当前版本 / 最新版本 / 最近检查
        version_form = QFormLayout()
        version_form.setContentsMargins(0, 0, 0, 4)
        version_form.setSpacing(6)

        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setStyleSheet("color: #cdd6f4; font-weight: bold;")
        version_form.addRow("当前版本:", self.version_label)

        self.latest_version_label = QLabel("未知")
        self.latest_version_label.setStyleSheet("color: #a6adc8;")
        version_form.addRow("最新版本:", self.latest_version_label)

        self.last_check_label = QLabel("从未")
        self.last_check_label.setStyleSheet("color: #a6adc8;")
        version_form.addRow("最近检查:", self.last_check_label)

        update_layout.addLayout(version_form)

        # 启动时自动检查
        self.auto_check_cb = QCheckBox("启动时自动检查更新")
        self.auto_check_cb.setChecked(True)
        update_layout.addWidget(self.auto_check_cb)

        # 立即检查更新 / 前往下载
        check_btn_layout = QHBoxLayout()
        check_btn_layout.addStretch()
        self.check_now_btn = QPushButton("🔍 立即检查更新")
        self.check_now_btn.clicked.connect(self.update_check_requested.emit)
        check_btn_layout.addWidget(self.check_now_btn)

        self.download_btn = QPushButton("🚀 前往下载页面")
        self.download_btn.setObjectName("primary")
        self.download_btn.setToolTip("在浏览器中打开 GitHub Release 页面")
        self.download_btn.clicked.connect(self._open_release_page)
        self.download_btn.hide()  # 仅在存在新版本时显示
        check_btn_layout.addWidget(self.download_btn)

        update_layout.addLayout(check_btn_layout)

        layout.addWidget(update_group)

        # ── 界面主题 ──
        theme_group = QGroupBox("界面")
        theme_form = QFormLayout(theme_group)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("暗色", "dark")
        self.theme_combo.addItem("亮色", "light")
        theme_form.addRow("主题:", self.theme_combo)
        layout.addWidget(theme_group)

        # ── 按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _load_config(self):
        """加载当前配置到 UI"""
        cfg = self._config
        self.game_path_edit.setText(cfg.game_mod_path)
        self.repo_path_edit.setText(cfg.repo_path)
        self.remote_url_edit.setText(cfg.remote_repo_url)
        self.confirm_uninstall_cb.setChecked(cfg.confirm_before_uninstall)
        self.clean_empty_cb.setChecked(cfg.clean_empty_dirs)
        self.auto_check_cb.setChecked(cfg.update_check_enabled)
        self.refresh_update_info()

        idx = self.theme_combo.findData(cfg.theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)

    def refresh_update_info(self):
        """刷新版本记录显示（主窗口在检查完成后也会调用）

        读取全局配置单例而非构造时的引用，保证检查结果能即时反映到界面。
        """
        cfg = get_config()
        self.last_check_label.setText(_format_check_time(cfg.update_last_check_at))

        latest = (cfg.update_latest_version or "").strip()
        if not latest:
            self.latest_version_label.setText("未知（尚未检查）")
            self.latest_version_label.setStyleSheet("color: #a6adc8;")
            self.download_btn.hide()
            return

        suffix = "（预览版）" if cfg.update_latest_is_prerelease else ""
        if is_newer_version(latest, __version__):
            self.latest_version_label.setText(f"v{latest}{suffix} — 有新版本可用")
            self.latest_version_label.setStyleSheet(
                "color: #a6e3a1; font-weight: bold;"
            )
            self.download_btn.show()
        else:
            self.latest_version_label.setText(f"v{latest}{suffix} — 已是最新")
            self.latest_version_label.setStyleSheet("color: #a6adc8;")
            self.download_btn.hide()

    def _open_release_page(self):
        """在系统浏览器中打开 Release 页面"""
        url = (get_config().update_latest_url or "").strip() or RELEASES_PAGE_URL
        QDesktopServices.openUrl(QUrl(url))

    def _browse_game_path(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择游戏 Mod 路径", self.game_path_edit.text() or str(Path.home())
        )
        if path:
            self.game_path_edit.setText(path)

    def _browse_repo_path(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择本地仓库路径", self.repo_path_edit.text() or str(Path.home())
        )
        if path:
            self.repo_path_edit.setText(path)

    def _save(self):
        """保存配置"""
        # 基本校验
        game_path = self.game_path_edit.text().strip()
        repo_path = self.repo_path_edit.text().strip()
        remote_url = self.remote_url_edit.text().strip()

        if not game_path:
            self.game_path_edit.setFocus()
            return

        if not repo_path:
            self.repo_path_edit.setFocus()
            return

        # 更新配置
        self._config.game_mod_path = game_path
        self._config.repo_path = repo_path
        self._config.remote_repo_url = remote_url
        self._config.confirm_before_uninstall = self.confirm_uninstall_cb.isChecked()
        self._config.clean_empty_dirs = self.clean_empty_cb.isChecked()
        self._config.update_check_enabled = self.auto_check_cb.isChecked()
        self._config.theme = self.theme_combo.currentData()

        save_config(self._config)
        self.accept()
