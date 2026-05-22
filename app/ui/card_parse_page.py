"""卡片解析页面 — 嵌入主窗口的单页面组件

布局参考 card_result_dialog，修改点：
  - mod 列表列改为: GUID | Mod名称 | 状态 | 操作
  - 不再弹出对话框，结果内嵌展示
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
    QSplitter, QFrame, QMessageBox, QApplication,
    QFileDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QPixmap, QDragEnterEvent, QDropEvent

from app.services.card_service import (
    CardParseResult,
    CardModStatus,
    CardModItem,
)


class CardParsePage(QWidget):
    """卡片解析页面"""

    # 信号：请求安装某个仓库中的 Mod
    install_mod_requested = pyqtSignal(int)  # mod_id
    # 信号：请求重新解析当前卡片
    reparse_requested = pyqtSignal()

    INSTALLED_COLOR = "#a6e3a1"
    IN_REPO_COLOR = "#f9e2af"
    MISSING_COLOR = "#f38ba8"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: CardParseResult | None = None
        self._setup_ui()

    # ═══════════════════════════════════════════════════════
    # UI 构建
    # ═══════════════════════════════════════════════════════

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # ── 顶部：拖放区 + 选择文件按钮 ──
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.drop_zone = QLabel("🎴 拖入角色卡/服装卡 (PNG) 进行 Mod 依赖分析")
        self.drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_zone.setMinimumHeight(60)
        self.drop_zone.setMaximumHeight(80)
        self.drop_zone.setStyleSheet(
            "QLabel {"
            "  background-color: #252540;"
            "  border: 2px dashed #45475a;"
            "  border-radius: 8px;"
            "  color: #a6adc8;"
            "  font-size: 14px;"
            "}"
        )
        self.drop_zone.setAcceptDrops(True)
        top_bar.addWidget(self.drop_zone, 1)

        select_btn = QPushButton("📂 选择卡片")
        select_btn.setObjectName("primary")
        select_btn.setFixedHeight(50)
        select_btn.clicked.connect(self._on_select_card)
        top_bar.addWidget(select_btn)

        main_layout.addLayout(top_bar)

        # ── 主内容区：左右分栏 ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左列：图片预览
        left_panel = QVBoxLayout()
        left_panel.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.image_label = QLabel()
        self.image_label.setFixedSize(220, 320)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(
            "background-color: #313244; border: 1px solid #45475a; border-radius: 4px;"
        )
        self.image_label.setText("暂无卡片")
        left_panel.addWidget(self.image_label)

        self.card_name_label = QLabel("")
        self.card_name_label.setObjectName("subtitle")
        self.card_name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_name_label.setWordWrap(True)
        left_panel.addWidget(self.card_name_label)

        # 卡片类型标签
        self.card_type_label = QLabel("")
        self.card_type_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_type_label.setStyleSheet("color: #a6adc8; font-size: 12px;")
        left_panel.addWidget(self.card_type_label)

        left_widget = QFrame()
        left_widget.setLayout(left_panel)
        left_widget.setFixedWidth(240)
        splitter.addWidget(left_widget)

        # 右列：Mod 列表
        right_panel = QVBoxLayout()
        right_panel.setSpacing(8)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["GUID", "Mod 名称", "状态", "操作"])
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 240)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 100)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(16)
        right_panel.addWidget(self.tree)

        right_widget = QFrame()
        right_widget.setLayout(right_panel)
        splitter.addWidget(right_widget)

        splitter.setSizes([240, 640])
        main_layout.addWidget(splitter, 1)

        # ── 底部统计 ──
        self.stats_label = QLabel("拖入 PNG 卡片开始解析")
        self.stats_label.setObjectName("statLabel")
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.stats_label)

        # ── 操作按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.install_all_btn = QPushButton("全部安装（仓库有）")
        self.install_all_btn.setObjectName("success")
        self.install_all_btn.clicked.connect(self._on_install_all)
        self.install_all_btn.setVisible(False)
        btn_layout.addWidget(self.install_all_btn)

        self.copy_missing_btn = QPushButton("复制缺失列表")
        self.copy_missing_btn.clicked.connect(self._on_copy_missing)
        self.copy_missing_btn.setVisible(False)
        btn_layout.addWidget(self.copy_missing_btn)

        main_layout.addLayout(btn_layout)

    # ═══════════════════════════════════════════════════════
    # 拖放处理
    # ═══════════════════════════════════════════════════════

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = Path(url.toLocalFile())
                if path.suffix.lower() == ".png":
                    event.acceptProposedAction()
                    self._highlight_drop_zone(True)
                    return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._highlight_drop_zone(False)

    def dropEvent(self, event: QDropEvent):
        self._highlight_drop_zone(False)
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = Path(url.toLocalFile())
                if path.suffix.lower() == ".png":
                    self._on_card_dropped(path)
                    break

    def _highlight_drop_zone(self, active: bool):
        if active:
            self.drop_zone.setStyleSheet(
                "QLabel {"
                "  background-color: #2a3a4a;"
                "  border: 2px dashed #89b4fa;"
                "  border-radius: 8px;"
                "  color: #89b4fa;"
                "  font-size: 14px;"
                "}"
            )
        else:
            self.drop_zone.setStyleSheet(
                "QLabel {"
                "  background-color: #252540;"
                "  border: 2px dashed #45475a;"
                "  border-radius: 8px;"
                "  color: #a6adc8;"
                "  font-size: 14px;"
                "}"
            )

    # ═══════════════════════════════════════════════════════
    # 卡片操作
    # ═══════════════════════════════════════════════════════

    def _on_select_card(self):
        """点击选择卡片按钮"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择角色卡/服装卡",
            str(Path.home()),
            "PNG 文件 (*.png);;所有文件 (*.*)",
        )
        if file_path:
            self._on_card_dropped(Path(file_path))

    def _on_card_dropped(self, card_path: Path):
        """卡片拖入后的处理 — 由外部（MainWindow）调用解析服务"""
        # 通知 MainWindow 进行解析
        # 这里暂时只更新 UI 占位，实际解析由 MainWindow 的
        # show_card_result / set_card_result 方法完成
        self.drop_zone.setText(f"正在解析：{card_path.name}...")

    def set_card_result(self, result: CardParseResult):
        """由 MainWindow 调用，展示解析结果"""
        self._result = result
        self._populate()

    def clear_result(self):
        """清空解析结果"""
        self._result = None
        self.tree.clear()
        self.image_label.setText("暂无卡片")
        self.card_name_label.setText("")
        self.card_type_label.setText("")
        self.stats_label.setText("拖入 PNG 卡片开始解析")
        self.install_all_btn.setVisible(False)
        self.copy_missing_btn.setVisible(False)
        self.drop_zone.setText("🎴 拖入角色卡/服装卡 (PNG) 进行 Mod 依赖分析")

    # ═══════════════════════════════════════════════════════
    # 数据填充
    # ═══════════════════════════════════════════════════════

    def _populate(self):
        """填充卡片解析结果到 UI"""
        if self._result is None:
            return

        self.tree.clear()

        # 更新拖放区
        self.drop_zone.setText(f"📋 当前卡片：{self._result.card_name}  （拖入新卡片重新解析）")

        # 加载图片
        self._load_card_image()

        # 文件名
        self.card_name_label.setText(
            self._truncate_name(self._result.card_name, 25)
        )

        # 卡片类型
        self.card_type_label.setText(f"类型：{self._result.card_type}")

        if not self._result.is_valid:
            self.stats_label.setText(f"⚠ {self._result.error_message}")
            self.stats_label.setStyleSheet(f"color: {self.MISSING_COLOR};")
            return

        # 分组
        installed = [m for m in self._result.mods
                     if m.status == CardModStatus.INSTALLED]
        in_repo = [m for m in self._result.mods
                   if m.status == CardModStatus.IN_REPO]
        missing = [m for m in self._result.mods
                   if m.status == CardModStatus.MISSING]

        # 🟢 已安装分组
        if installed:
            group = self._add_group(
                f"🟢 已安装（{len(installed)} 个）",
                self.INSTALLED_COLOR,
            )
            for item in installed:
                info = f"{item.mod_author}/{item.mod_name} v{item.mod_version}"
                self._add_item(group, item.guid, info, "🟢 已安装", "")

        # 🟡 仓库有分组
        if in_repo:
            group = self._add_group(
                f"🟡 仓库有，可安装（{len(in_repo)} 个）",
                self.IN_REPO_COLOR,
            )
            for item in in_repo:
                info = f"{item.mod_author}/{item.mod_name} v{item.mod_version}"
                self._add_item(
                    group, item.guid, info, "🟡 仓库有", "安装",
                    mod_id=item.mod_id,
                )

        # 🔴 缺失分组
        if missing:
            group = self._add_group(
                f"🔴 缺失（{len(missing)} 个）",
                self.MISSING_COLOR,
            )
            for item in missing:
                self._add_item(group, item.guid, "", "🔴 缺失", "")

        self.tree.expandAll()

        # 更新底部统计
        self._update_stats()

        # 按钮可见性
        self.install_all_btn.setVisible(len(in_repo) > 0)
        self.copy_missing_btn.setVisible(len(missing) > 0)

    def _load_card_image(self):
        """加载卡片 PNG 图片预览"""
        if self._result is None:
            return
        try:
            pixmap = QPixmap(str(self._result.card_path))
            if pixmap.isNull():
                self.image_label.setText("图片加载失败")
                return

            scaled = pixmap.scaled(
                QSize(200, 300),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.image_label.setPixmap(scaled)
        except Exception:
            self.image_label.setText("图片加载失败")

    def _add_group(self, text: str, color: str) -> QTreeWidgetItem:
        """添加分组行（状态分组标题）"""
        item = QTreeWidgetItem(self.tree)
        item.setText(0, text)
        item.setForeground(0, Qt.GlobalColor.white)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setFirstColumnSpanned(True)
        return item

    def _add_item(
        self,
        parent: QTreeWidgetItem,
        guid: str,
        mod_info: str,
        status_text: str,
        action_text: str,
        mod_id: int | None = None,
    ):
        """添加 Mod 行（列序: GUID | Mod名称 | 状态 | 操作）"""
        item = QTreeWidgetItem(parent)
        item.setText(0, guid)
        item.setText(1, mod_info)
        item.setText(2, status_text)
        item.setText(3, "")

        # 根据状态着色
        if "已安装" in status_text:
            item.setForeground(2, Qt.GlobalColor.white)
        elif "仓库有" in status_text:
            item.setForeground(2, Qt.GlobalColor.white)
        elif "缺失" in status_text:
            item.setForeground(0, Qt.GlobalColor.white)

        # 操作列：嵌入安装按钮
        if action_text == "安装" and mod_id is not None:
            btn = QPushButton("安装")
            btn.setFixedSize(60, 24)
            btn.setStyleSheet(
                "QPushButton { background-color: #a6e3a1; color: #1e1e2e; "
                "border: none; border-radius: 3px; font-size: 11px; }"
                "QPushButton:hover { background-color: #94e2d5; }"
            )
            btn.clicked.connect(
                lambda checked, mid=mod_id: self._on_install_one(mid)
            )
            self.tree.setItemWidget(item, 3, btn)

    # ═══════════════════════════════════════════════════════
    # 操作回调
    # ═══════════════════════════════════════════════════════

    def _on_install_one(self, mod_id: int):
        """单个安装按钮 — 发射信号给 MainWindow 处理"""
        self.install_mod_requested.emit(mod_id)

    def _on_install_all(self):
        """全部安装按钮"""
        if self._result is None:
            return
        reply = QMessageBox.question(
            self,
            "确认安装",
            "确定要安装所有仓库中已有的 Mod 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        in_repo = [m for m in self._result.mods
                   if m.status == CardModStatus.IN_REPO and m.mod_id is not None]
        for item in in_repo:
            self.install_mod_requested.emit(item.mod_id)

        # 请求 MainWindow 重新解析卡片
        self.reparse_requested.emit()

    def _on_copy_missing(self):
        """复制缺失 Mod GUID 列表到剪贴板"""
        if self._result is None:
            return

        missing = [m for m in self._result.mods
                   if m.status == CardModStatus.MISSING]
        if not missing:
            QMessageBox.information(self, "提示", "没有缺失的 Mod")
            return

        text = "\n".join(m.guid for m in missing)
        clipboard = QApplication.clipboard()
        clipboard.setText(text)

        QMessageBox.information(
            self,
            "已复制",
            f"已复制 {len(missing)} 个缺失 Mod 的 GUID 到剪贴板",
        )

    def _update_stats(self):
        """更新底部统计文本"""
        if self._result is None:
            return
        self.stats_label.setText(
            f"总计：{self._result.total_count}  |  "
            f"🟢 已安装：{self._result.installed_count}  |  "
            f"🟡 仓库有：{self._result.in_repo_count}  |  "
            f"🔴 缺失：{self._result.missing_count}"
        )

    @staticmethod
    def _truncate_name(name: str, max_len: int) -> str:
        if len(name) > max_len:
            return name[:max_len - 3] + "..."
        return name
