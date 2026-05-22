"""设置对话框（F01 / F16）

需求文档 v1.3:
  - 游戏 Mod 路径配置（F01）
  - 本地仓库路径配置（F01）
  - 远程仓库 URL 配置（F16，P2，可选）
  - 行为配置：卸载前确认、清理空目录
  - 主题选择
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QComboBox,
    QGroupBox, QFormLayout, QFileDialog, QMessageBox,
)
from PyQt6.QtCore import Qt

from app.config import AppConfig, get_config, save_config


class SettingsDialog(QDialog):
    """设置对话框"""

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

        idx = self.theme_combo.findData(cfg.theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)

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
        self._config.theme = self.theme_combo.currentData()

        save_config(self._config)
        self.accept()
