"""设置对话框（F01 / F16 / F19 / F23 / F25）

需求文档 v1.5:
  - 游戏 Mod 路径配置（F01）
  - 本地仓库路径配置（F01）
  - 远程仓库 URL 配置（F16，P2，可选）
  - 行为配置：卸载前确认、清理空目录、应用内回收站开关（F25）
  - 分类目录配置（F19）：安装开关、分类清单、角色类/工作室类落点
  - Sideloader 日志路径（F23）：BepInEx LogOutput.log，留空自动探测
  - 主题选择
"""

from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QCheckBox, QComboBox,
    QGroupBox, QFormLayout, QFileDialog, QPlainTextEdit, QLabel, QWidget, QMessageBox
)

from app.config import describe_category, get_config, save_config
from app.utils.file_utils import (
    empty_trash, ensure_dir, fmt_size, get_trash_dir, trash_stats
)


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

        # BepInEx 游戏日志路径（F23：从游戏日志读取缺失）
        sidelog_layout = QHBoxLayout()
        self.sidelog_path_edit = QLineEdit()
        self.sidelog_path_edit.setPlaceholderText(
            "留空 = 按 Mod 目录布局自动探测 BepInEx/LogOutput.log..."
        )
        sidelog_layout.addWidget(self.sidelog_path_edit)
        self.sidelog_browse_btn = QPushButton("浏览...")
        self.sidelog_browse_btn.clicked.connect(self._browse_sideloader_log_path)
        sidelog_layout.addWidget(self.sidelog_browse_btn)
        path_form.addRow("游戏日志文件:", sidelog_layout)

        layout.addWidget(path_group)

        # ── 远程仓库（P2）─ ─
        remote_group = QGroupBox("远程仓库（可选）")
        remote_form = QFormLayout(remote_group)
        self.remote_url_edit = QLineEdit()
        self.remote_url_edit.setPlaceholderText("https://example.com/mods/")
        remote_form.addRow("远程仓库 URL:", self.remote_url_edit)
        layout.addWidget(remote_group)

        # ── 分类目录（F19）──
        category_group = QGroupBox("分类目录（游戏端安装）")
        category_group.setToolTip(
            "开启后安装到游戏的 Mod 落入 {游戏Mod路径}/{分类}/{作者}/ 三段式目录；\n"
            "仓库端不分类。未知顶层目录视为手动分类，照常扫描。"
        )
        category_layout = QVBoxLayout(category_group)

        self.category_enabled_cb = QCheckBox("游戏分类目录安装（安装到 {分类}/{作者}/ 三段式目录）")
        self.category_enabled_cb.setChecked(True)
        category_layout.addWidget(self.category_enabled_cb)

        category_form = QFormLayout()
        category_form.addRow("分类目录清单:", self._make_categories_edit())

        # 落点为可编辑下拉：编辑/保存的都是原始目录名，下方灰字仅作中文说明注记
        self.chara_category_combo = QComboBox()
        self.chara_category_combo.setEditable(True)
        self.chara_category_hint = QLabel("")
        category_form.addRow(
            "角色类分类落点:",
            self._wrap_category_combo(self.chara_category_combo, self.chara_category_hint),
        )

        self.studio_category_combo = QComboBox()
        self.studio_category_combo.setEditable(True)
        self.studio_category_hint = QLabel("")
        category_form.addRow(
            "工作室类分类落点:",
            self._wrap_category_combo(self.studio_category_combo, self.studio_category_hint),
        )

        for combo in (self.chara_category_combo, self.studio_category_combo):
            combo.currentTextChanged.connect(lambda _: self._update_category_hints())

        category_layout.addLayout(category_form)
        layout.addWidget(category_group)

        # ── 行为设置 ──
        behavior_group = QGroupBox("行为设置")
        behavior_layout = QVBoxLayout(behavior_group)

        self.confirm_uninstall_cb = QCheckBox("卸载/移除前弹出确认对话框")
        self.confirm_uninstall_cb.setChecked(True)
        behavior_layout.addWidget(self.confirm_uninstall_cb)

        self.clean_empty_cb = QCheckBox("卸载/移除后自动清理空目录")
        self.clean_empty_cb.setChecked(True)
        behavior_layout.addWidget(self.clean_empty_cb)

        # 应用内回收站（F25）：开关 + 统计 + 打开 / 清空
        self.use_trash_cb = QCheckBox(
            "删除操作移入应用内回收站 data/.trash/（关闭则硬删除，F25）"
        )
        self.use_trash_cb.setChecked(True)
        behavior_layout.addWidget(self.use_trash_cb)

        trash_row = QHBoxLayout()
        self.trash_stats_label = QLabel()
        self.open_trash_btn = QPushButton("📂 打开回收站目录")
        self.open_trash_btn.clicked.connect(self._open_trash)
        self.empty_trash_btn = QPushButton("🗑 清空回收站")
        self.empty_trash_btn.setObjectName("danger")
        self.empty_trash_btn.clicked.connect(self._empty_trash)
        trash_row.addWidget(self.trash_stats_label)
        trash_row.addStretch(1)
        trash_row.addWidget(self.open_trash_btn)
        trash_row.addWidget(self.empty_trash_btn)
        behavior_layout.addLayout(trash_row)

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

    def _make_categories_edit(self) -> QPlainTextEdit:
        """分类目录清单多行编辑框（每行一个分类名，7.3）"""
        self.categories_edit = QPlainTextEdit()
        self.categories_edit.setPlaceholderText("每行一个分类目录名…")
        self.categories_edit.setMaximumHeight(110)
        self.categories_edit.textChanged.connect(self._refresh_category_combos)
        return self.categories_edit

    def _wrap_category_combo(self, combo: QComboBox, hint: QLabel) -> QWidget:
        """下拉 + 中文说明灰字打包为表单行容器（说明仅展示，不影响编辑值）"""
        hint.setStyleSheet("color: #888; font-size: 11px;")
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        box.addWidget(combo)
        box.addWidget(hint)
        wrapper = QWidget()
        wrapper.setLayout(box)
        return wrapper

    def _update_category_hints(self):
        """刷新落点下拉下方的中文说明（已知分类才显示，自定义分类留空）"""
        for combo, hint in (
            (self.chara_category_combo, self.chara_category_hint),
            (self.studio_category_combo, self.studio_category_hint),
        ):
            name = combo.currentText().strip()
            desc = describe_category(name)
            hint.setText(desc if name and desc != name else "")

    def _refresh_category_combos(self):
        """按清单刷新两个落点下拉的候选项（保留当前选择/自定义输入）"""
        names = self._category_names_from_edit()
        for combo in (self.chara_category_combo, self.studio_category_combo):
            current = combo.currentText().strip()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentText(current)
            combo.blockSignals(False)
        # blockSignals 期间 currentTextChanged 不触发，这里手动补一次
        self._update_category_hints()

    def _category_names_from_edit(self) -> list[str]:
        seen: set[str] = set()
        names: list[str] = []
        for line in self.categories_edit.toPlainText().splitlines():
            name = line.strip().rstrip("/").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return names

    def _load_config(self):
        """加载当前配置到 UI"""
        cfg = self._config
        self.game_path_edit.setText(cfg.game_mod_path)
        self.repo_path_edit.setText(cfg.repo_path)
        self.sidelog_path_edit.setText(cfg.sideloader_log_path)
        self.remote_url_edit.setText(cfg.remote_repo_url)
        self.confirm_uninstall_cb.setChecked(cfg.confirm_before_uninstall)
        self.clean_empty_cb.setChecked(cfg.clean_empty_dirs)
        self.use_trash_cb.setChecked(cfg.use_trash)
        self._refresh_trash_stats()

        # 分类目录（F19）：清单载入为多行文本，落点下拉候选项来自清单
        cat = cfg.category
        self.category_enabled_cb.setChecked(cat.game_category_enabled)
        self.categories_edit.setPlainText("\n".join(cat.category_names))
        self.chara_category_combo.setCurrentText(cat.category_chara)
        self.studio_category_combo.setCurrentText(cat.category_studio)

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

    def _browse_sideloader_log_path(self):
        """选择 BepInEx 日志文件（F23；留空回到自动探测）"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 BepInEx 日志文件",
            self.sidelog_path_edit.text() or str(Path.home()),
            "日志文件 (*.log);;所有文件 (*.*)",
        )
        if path:
            self.sidelog_path_edit.setText(path)

    # ── 应用内回收站（F25）──────────────────────────────────

    def _refresh_trash_stats(self):
        """刷新回收站统计行（文件数 / 占用空间）"""
        count, size = trash_stats()
        self.trash_stats_label.setText(f"回收站：{count} 个文件 · {fmt_size(size)}")

    def _open_trash(self):
        """在系统资源管理器中打开回收站目录（不存在则先创建）"""
        ensure_dir(get_trash_dir())
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_trash_dir())))

    def _empty_trash(self):
        """清空回收站（二次确认后硬删全部内容，F25-3）"""
        count, size = trash_stats()
        if count == 0:
            QMessageBox.information(self, "回收站", "回收站是空的。")
            return
        answer = QMessageBox.question(
            self,
            "清空回收站",
            f"将永久删除回收站中的 {count} 个文件（{fmt_size(size)}）。\n"
            "此操作不可恢复，确认清空？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = empty_trash()
        except OSError as e:
            QMessageBox.critical(self, "清空失败", f"清空回收站失败：{e}")
            return
        QMessageBox.information(self, "回收站", f"已清空回收站，共删除 {removed} 个文件。")
        self._refresh_trash_stats()

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
        # 留空 = 回到自动探测（F23-2）
        self._config.sideloader_log_path = self.sidelog_path_edit.text().strip()
        self._config.remote_repo_url = remote_url
        self._config.confirm_before_uninstall = self.confirm_uninstall_cb.isChecked()
        self._config.clean_empty_dirs = self.clean_empty_cb.isChecked()
        self._config.use_trash = self.use_trash_cb.isChecked()
        self._config.theme = self.theme_combo.currentData()

        # 分类目录（F19）：清单多行文本以分号拼回；落点为空回退默认
        names = self._category_names_from_edit()
        cat = self._config.category
        cat.game_category_enabled = self.category_enabled_cb.isChecked()
        cat.game_categories = ";".join(names)
        cat.category_chara = self.chara_category_combo.currentText().strip() or cat.category_chara
        cat.category_studio = self.studio_category_combo.currentText().strip() or cat.category_studio

        save_config(self._config)
        self.accept()
