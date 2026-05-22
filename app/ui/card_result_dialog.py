"""卡片解析结果面板（F15）

需求文档 v1.3:
  - F15-4: 列表展示依赖 Mod，标注状态（已安装/仓库有/缺失）
  - F15-5: "仓库有"的 Mod 可一键安装
  - F15-6: 缺失 Mod 汇总列表，支持复制
  - F15-7: 底部统计
  - F15-9: 卡片图片渲染（左列）
  - F15-10: 图片固定最大尺寸 200×300px，保比例缩放
  - F15-11: 图片在结果出现前完成渲染
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
    QSplitter, QFrame, QMessageBox, QApplication,
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPixmap, QFont

from app.services.card_service import (
    CardParseResult,
    CardModItem,
    CardModStatus,
)


class CardResultDialog(QDialog):
    """卡片解析结果对话框"""

    INSTALLED_COLOR = "#a6e3a1"
    IN_REPO_COLOR = "#f9e2af"
    MISSING_COLOR = "#f38ba8"

    def __init__(self, result: CardParseResult, parent=None):
        super().__init__(parent)
        self.result = result
        self._setup_ui()
        self._populate()

    def _setup_ui(self):
        self.setWindowTitle(f"卡片解析结果：{self.result.card_name}")
        self.setMinimumSize(900, 600)
        self.setModal(False)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        # ── 顶部标题 ──
        header = QLabel(f"📋 卡片解析结果：{self.result.card_name}")
        header.setObjectName("title")
        main_layout.addWidget(header)

        # ── 分隔线 ──
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #45475a;")
        main_layout.addWidget(line)

        # ── 错误提示 ──
        if not self.result.is_valid and self.result.error_message:
            error_label = QLabel(f"⚠ {self.result.error_message}")
            error_label.setStyleSheet(f"color: {self.MISSING_COLOR}; font-size: 14px;")
            main_layout.addWidget(error_label)
            return

        # ── 左右分栏 ──
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
        left_panel.addWidget(self.image_label)

        # 文件名标签
        name_label = QLabel(self._truncate_name(self.result.card_name, 25))
        name_label.setObjectName("subtitle")
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setWordWrap(True)
        left_panel.addWidget(name_label)

        left_widget = QFrame()
        left_widget.setLayout(left_panel)
        left_widget.setFixedWidth(240)
        splitter.addWidget(left_widget)

        # 右列：Mod 列表
        right_panel = QVBoxLayout()
        right_panel.setSpacing(8)

        # 列表控件
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["状态", "Mod 信息", "GUID", "操作"])
        self.tree.setColumnWidth(0, 60)
        self.tree.setColumnWidth(1, 280)
        self.tree.setColumnWidth(2, 250)
        self.tree.setColumnWidth(3, 100)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(16)
        right_panel.addWidget(self.tree)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.install_all_btn = QPushButton("全部安装（仓库有）")
        self.install_all_btn.setObjectName("success")
        self.install_all_btn.clicked.connect(self._on_install_all)
        btn_layout.addWidget(self.install_all_btn)

        self.copy_missing_btn = QPushButton("复制缺失列表")
        self.copy_missing_btn.clicked.connect(self._on_copy_missing)
        btn_layout.addWidget(self.copy_missing_btn)

        right_panel.addLayout(btn_layout)

        right_widget = QFrame()
        right_widget.setLayout(right_panel)
        splitter.addWidget(right_widget)

        splitter.setSizes([240, 640])
        main_layout.addWidget(splitter, 1)

        # ── 底部统计 ──
        stats = (
            f"总计：{self.result.total_count}  |  "
            f"🟢 已安装：{self.result.installed_count}  |  "
            f"🟡 仓库有：{self.result.in_repo_count}  |  "
            f"🔴 缺失：{self.result.missing_count}"
        )
        stats_label = QLabel(stats)
        stats_label.setObjectName("statLabel")
        stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(stats_label)

    def _populate(self):
        """填充数据"""
        # 加载图片
        self._load_card_image()

        if not self.result.is_valid:
            return

        # 分组
        installed = [m for m in self.result.mods if m.status == CardModStatus.INSTALLED]
        in_repo = [m for m in self.result.mods if m.status == CardModStatus.IN_REPO]
        missing = [m for m in self.result.mods if m.status == CardModStatus.MISSING]

        # 🟢 已安装
        if installed:
            group = self._add_group(
                f"🟢 已安装（{len(installed)} 个）", self.INSTALLED_COLOR
            )
            for item in installed:
                self._add_item(
                    group,
                    "已安装",
                    f"{item.mod_author}/{item.mod_name} v{item.mod_version}",
                    item.guid,
                    "",
                )

        # 🟡 仓库有
        if in_repo:
            group = self._add_group(
                f"🟡 仓库有，可安装（{len(in_repo)} 个）", self.IN_REPO_COLOR
            )
            for item in in_repo:
                self._add_item(
                    group,
                    "仓库有",
                    f"{item.mod_author}/{item.mod_name} v{item.mod_version}",
                    item.guid,
                    "安装",
                    item.mod_id,
                )

        # 🔴 缺失
        if missing:
            group = self._add_group(
                f"🔴 缺失（{len(missing)} 个）", self.MISSING_COLOR
            )
            for item in missing:
                self._add_item(group, "缺失", "", item.guid, "")

        self.tree.expandAll()

        # 如果没有仓库有的 Mod，隐藏全部安装按钮
        if not in_repo:
            self.install_all_btn.setVisible(False)

    def _load_card_image(self):
        """加载卡片 PNG 图片（F15-9/10/11）"""
        try:
            pixmap = QPixmap(str(self.result.card_path))
            if pixmap.isNull():
                self.image_label.setText("图片加载失败")
                return

            # 缩放到最大 200×300，保比例
            scaled = pixmap.scaled(
                QSize(200, 300),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.image_label.setPixmap(scaled)
        except Exception:
            self.image_label.setText("图片加载失败")

    def _add_group(self, text: str, color: str) -> QTreeWidgetItem:
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
        status: str,
        info: str,
        guid: str,
        action: str,
        mod_id: int | None = None,
    ):
        item = QTreeWidgetItem(parent)
        item.setText(0, "")
        item.setText(1, info)
        item.setText(2, guid)
        item.setText(3, action)

        if status == "已安装":
            item.setForeground(1, Qt.GlobalColor.white)
        elif status == "仓库有":
            item.setForeground(1, Qt.GlobalColor.white)
        elif status == "缺失":
            item.setForeground(2, Qt.GlobalColor.white)

        if action == "安装" and mod_id is not None:
            item.mod_id = mod_id
            btn = QPushButton("安装")
            btn.setFixedSize(60, 24)
            btn.setStyleSheet(
                "QPushButton { background-color: #a6e3a1; color: #1e1e2e; "
                "border: none; border-radius: 3px; font-size: 11px; }"
                "QPushButton:hover { background-color: #94e2d5; }"
            )
            btn.clicked.connect(lambda checked, mid=mod_id: self._on_install_one(mid))
            self.tree.setItemWidget(item, 3, btn)

    def _on_install_one(self, mod_id: int):
        """单个安装按钮的回调 - 发射信号通知主窗口"""
        from PyQt6.QtCore import pyqtSignal
        # 实际上通过 parent 来处理
        # 这里通过对话框的 accepted 信号携带信息
        self._pending_install_id = mod_id
        # 尝试找主窗口来执行安装
        main_win = self.parent()
        if main_win and hasattr(main_win, 'install_repo_mod'):
            success, msg = main_win.install_repo_mod(mod_id)
            QMessageBox.information(
                self,
                "安装结果",
                msg,
            )
            # 刷新当前对话框
            if success and hasattr(main_win, 'reparse_card'):
                new_result = main_win.reparse_card()
                if new_result:
                    self.result = new_result
                    self.tree.clear()
                    self._populate()
                    self._update_stats()

    def _on_install_all(self):
        """全部安装按钮"""
        reply = QMessageBox.question(
            self,
            "确认安装",
            "确定要安装所有仓库中已有的 Mod 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        main_win = self.parent()
        if not main_win or not hasattr(main_win, 'install_repo_mod'):
            return

        installed_count = 0
        for item in self.result.mods:
            if item.status == CardModStatus.IN_REPO and item.mod_id is not None:
                success, _ = main_win.install_repo_mod(item.mod_id)
                if success:
                    installed_count += 1

        QMessageBox.information(
            self,
            "批量安装完成",
            f"成功安装 {installed_count} 个 Mod",
        )

        # 刷新
        if hasattr(main_win, 'reparse_card'):
            new_result = main_win.reparse_card()
            if new_result:
                self.result = new_result
                self.tree.clear()
                self._populate()
                self._update_stats()

    def _on_copy_missing(self):
        """复制缺失 Mod 列表到剪贴板"""
        missing = [m for m in self.result.mods if m.status == CardModStatus.MISSING]
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
        for widget in self.children():
            if isinstance(widget, QLabel) and widget.objectName() == "statLabel":
                widget.setText(
                    f"总计：{self.result.total_count}  |  "
                    f"🟢 已安装：{self.result.installed_count}  |  "
                    f"🟡 仓库有：{self.result.in_repo_count}  |  "
                    f"🔴 缺失：{self.result.missing_count}"
                )

    @staticmethod
    def _truncate_name(name: str, max_len: int) -> str:
        if len(name) > max_len:
            return name[:max_len - 3] + "..."
        return name
