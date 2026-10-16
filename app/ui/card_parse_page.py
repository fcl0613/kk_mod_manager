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
from loguru import logger

from app.services.card_service import (
    CardModItem,
    CardParseResult,
    CardModStatus,
    GameVersion,
    SceneSummary,
)


class CardParsePage(QWidget):
    """卡片解析页面"""

    # 信号：请求安装某个仓库中的 Mod
    install_mod_requested = pyqtSignal(int)  # mod_id
    # 信号：请求重新解析当前卡片
    reparse_requested = pyqtSignal()
    # 信号：新卡片拖入/选择，请求解析
    card_dropped = pyqtSignal(Path)
    # 信号：请求将二代卡转换为一代卡（输入路径, 输出路径）
    convert_to_kk_requested = pyqtSignal(Path, Path)

    INSTALLED_COLOR = "#a6e3a1"
    IN_REPO_COLOR = "#f9e2af"
    MISSING_COLOR = "#f38ba8"

    # ── 亮色主题边框样式 ──
    LIGHT_DROP_ZONE_STYLE = (
        "QLabel#cardDropZone {"
        "  background-color: #f5f5f5;"
        "  border: 2px dashed #b0b0b0;"
        "  border-radius: 8px;"
        "  color: #888888;"
        "  font-size: 14px;"
        "}"
    )
    LIGHT_IMAGE_STYLE = (
        "QLabel#cardImageLabel {"
        "  background-color: #fafafa;"
        "  border: 1px solid #c8c8c8;"
        "  border-radius: 4px;"
        "}"
    )
    LIGHT_CARD_TYPE_STYLE = "QLabel#cardTypeLabel { color: #888888; font-size: 12px; }"

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

        self.drop_zone = QLabel("🎴 拖入角色卡/服装卡/场景卡 (PNG) 进行 Mod 依赖分析")
        self.drop_zone.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_zone.setMinimumHeight(60)
        self.drop_zone.setMaximumHeight(80)
        self.drop_zone.setObjectName("cardDropZone")
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
        self.image_label.setObjectName("cardImageLabel")
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
        self.card_type_label.setObjectName("cardTypeLabel")
        left_panel.addWidget(self.card_type_label)

        # 游戏版本标签
        self.game_version_label = QLabel("")
        self.game_version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.game_version_label.setObjectName("cardTypeLabel")
        left_panel.addWidget(self.game_version_label)

        # 场景摘要标签（仅场景卡显示，F17-2）
        self.scene_summary_label = QLabel("")
        self.scene_summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scene_summary_label.setObjectName("cardTypeLabel")
        self.scene_summary_label.setWordWrap(True)
        left_panel.addWidget(self.scene_summary_label)

        # 转为 KK 卡按钮（仅二代卡显示）
        self.convert_to_kk_btn = QPushButton("转为 KK 卡")
        self.convert_to_kk_btn.setObjectName("primary")
        self.convert_to_kk_btn.setFixedHeight(30)
        self.convert_to_kk_btn.setVisible(False)
        self.convert_to_kk_btn.clicked.connect(self._on_convert_to_kk)
        left_panel.addWidget(self.convert_to_kk_btn)

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
    # 主题管理
    # ═══════════════════════════════════════════════════════

    def apply_theme(self, is_dark: bool):
        """响应主题切换，设置对应样式"""
        if is_dark:
            # 清空内联样式，让 dark.qss（MainWindow 级别）接管
            self.drop_zone.setStyleSheet("")
            self.image_label.setStyleSheet("")
            self.card_type_label.setStyleSheet("")
            self.game_version_label.setStyleSheet("")
            self.scene_summary_label.setStyleSheet("")
        else:
            # 亮色主题：设置可见边框
            self.drop_zone.setStyleSheet(self.LIGHT_DROP_ZONE_STYLE)
            self.image_label.setStyleSheet(self.LIGHT_IMAGE_STYLE)
            self.card_type_label.setStyleSheet(self.LIGHT_CARD_TYPE_STYLE)
            self.game_version_label.setStyleSheet(self.LIGHT_CARD_TYPE_STYLE)
            self.scene_summary_label.setStyleSheet(self.LIGHT_CARD_TYPE_STYLE)

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
        """拖放高亮（根据当前主题使用对应颜色）"""
        from app.config import get_config
        is_dark = get_config().theme == "dark"
        if active:
            if is_dark:
                self.drop_zone.setStyleSheet(
                    "QLabel#cardDropZone {"
                    "  background-color: #2a3a4a;"
                    "  border: 2px dashed #89b4fa;"
                    "  border-radius: 8px;"
                    "  color: #89b4fa;"
                    "  font-size: 14px;"
                    "}"
                )
            else:
                self.drop_zone.setStyleSheet(
                    "QLabel#cardDropZone {"
                    "  background-color: #e3f2fd;"
                    "  border: 2px dashed #1976d2;"
                    "  border-radius: 8px;"
                    "  color: #1976d2;"
                    "  font-size: 14px;"
                    "}"
                )
        else:
            # 恢复当前主题的默认样式
            if is_dark:
                self.drop_zone.setStyleSheet("")
            else:
                self.drop_zone.setStyleSheet(self.LIGHT_DROP_ZONE_STYLE)

    # ═══════════════════════════════════════════════════════
    # 卡片操作
    # ═══════════════════════════════════════════════════════

    def _on_select_card(self):
        """点击选择卡片按钮"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择角色卡/服装卡/场景卡",
            "",
            "PNG 文件 (*.png);;所有文件 (*.*)",
        )
        if file_path:
            logger.debug("用户选择卡片: {}", file_path)
            self._on_card_dropped(Path(file_path))

    def _on_card_dropped(self, card_path: Path):
        """卡片拖入/选择后，发射信号给 MainWindow 进行解析"""
        logger.info("收到卡片: {}", card_path)
        self.drop_zone.setText(f"正在解析：{card_path.name}...")
        self.card_dropped.emit(card_path)

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
        self.game_version_label.setText("")
        self.scene_summary_label.setText("")
        self.scene_summary_label.setToolTip("")
        self.convert_to_kk_btn.setVisible(False)
        self.stats_label.setText("拖入 PNG 卡片开始解析")
        self.install_all_btn.setVisible(False)
        self.copy_missing_btn.setVisible(False)
        self.drop_zone.setText("🎴 拖入角色卡/服装卡/场景卡 (PNG) 进行 Mod 依赖分析")

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

        # 场景摘要 / 游戏版本
        summary = self._result.scene_summary
        if summary is not None:
            # 场景卡：左列显示摘要；不提供转卡（F17-12），
            # KK/KKS 线索取自首个成功解析的内嵌角色
            self.convert_to_kk_btn.setVisible(False)
            if summary.game_version is not None:
                self.game_version_label.setText(
                    "游戏：" + ("KK" if summary.game_version == GameVersion.KK else "KKS")
                )
            else:
                self.game_version_label.setText("")
            self._fill_scene_summary(summary)
        elif self._result.game_version == GameVersion.KK:
            self.scene_summary_label.setText("")
            self.scene_summary_label.setToolTip("")
            self.game_version_label.setText("游戏：KK")
            self.convert_to_kk_btn.setVisible(False)
        elif self._result.game_version == GameVersion.KKS:
            self.scene_summary_label.setText("")
            self.scene_summary_label.setToolTip("")
            self.game_version_label.setText("游戏：KKS")
            self.convert_to_kk_btn.setVisible(True)
        else:
            self.scene_summary_label.setText("")
            self.scene_summary_label.setToolTip("")
            self.game_version_label.setText("")
            self.convert_to_kk_btn.setVisible(False)

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
                tags = self._source_tags(item)
                if tags:
                    info += f" {tags}"
                self._add_item(group, item.guid, info, "🟢 已安装", "")

        # 🟡 仓库有分组
        if in_repo:
            group = self._add_group(
                f"🟡 仓库有，可安装（{len(in_repo)} 个）",
                self.IN_REPO_COLOR,
            )
            for item in in_repo:
                info = f"{item.mod_author}/{item.mod_name} v{item.mod_version}"
                tags = self._source_tags(item)
                if tags:
                    info += f" {tags}"
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
                # 缺失行展示卡内 UAR 元数据（Name · Author · Website）作为下载线索（F17-7）
                info = " · ".join(filter(None, [
                    item.meta_name, item.meta_author, item.meta_website,
                ]))
                tags = self._source_tags(item)
                if tags:
                    info = f"{info} {tags}".strip()
                self._add_item(group, item.guid, info, "🔴 缺失", "")

        self.tree.expandAll()

        # 更新底部统计
        self._update_stats()

        # 按钮可见性
        self.install_all_btn.setVisible(len(in_repo) > 0)
        self.copy_missing_btn.setVisible(len(missing) > 0)

    def _fill_scene_summary(self, s: SceneSummary):
        """填充场景摘要（版本/角色/物品/地图/Timeline，F17-2）"""
        lines = [
            f"版本：{s.version}" if s.version else "版本：未知",
            f"角色：{s.character_count}  物品：{s.item_count}",
        ]
        if s.map_id != -1:
            lines.append(f"地图 ID：{s.map_id}")
        if s.has_timeline:
            lines.append("🎬 Timeline 动画")
        if s.skipped_characters:
            lines.append(f"⚠ {len(s.skipped_characters)} 个角色解析失败")
        self.scene_summary_label.setText("\n".join(lines))

        # tooltip：完整角色名单 + 被跳过的角色（BR-C08）
        tooltip_parts = []
        if s.character_names:
            tooltip_parts.append("角色名单：\n" + "\n".join(s.character_names))
        if s.skipped_characters:
            tooltip_parts.append("解析失败（已跳过）：\n" + "\n".join(s.skipped_characters))
        self.scene_summary_label.setToolTip("\n\n".join(tooltip_parts))

    @staticmethod
    def _source_tags(item: CardModItem) -> str:
        """依赖来源标签文本（场景卡专用；普通卡 sources 为空返回空串，Q-06）"""
        return " ".join(f"[{src}]" for src in item.sources)

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

        # 带元数据的行输出 "GUID（Name · Author · Website）"，便于按图索骥找 Mod（F17-7）
        lines = []
        for m in missing:
            meta = " · ".join(filter(None, [m.meta_name, m.meta_author, m.meta_website]))
            lines.append(f"{m.guid}（{meta}）" if meta else m.guid)
        text = "\n".join(lines)
        clipboard = QApplication.clipboard()
        clipboard.setText(text)

        QMessageBox.information(
            self,
            "已复制",
            f"已复制 {len(missing)} 个缺失 Mod 的 GUID 到剪贴板",
        )

    def _on_convert_to_kk(self):
        """转为 KK 卡按钮 — 弹出文件保存对话框，发射信号给 MainWindow 处理"""
        if self._result is None:
            return

        # 默认文件名：原文件名加 _kk 后缀
        original_name = self._result.card_path.stem
        default_name = f"{original_name}_kk.png"

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存为一代卡（KK）",
            str(self._result.card_path.parent / default_name),
            "PNG 文件 (*.png);;所有文件 (*.*)",
        )
        if not save_path:
            return

        logger.info("用户选择保存路径: {}", save_path)
        self.convert_to_kk_requested.emit(
            self._result.card_path,
            Path(save_path),
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
