"""批量添加 Mod 对话框

功能：
  - 选择多个 .zipmod 文件，或整个文件夹（递归扫描）
  - 显示进度，逐个处理
  - 完成后将"已存在同版本（跳过）"和"版本冲突"的 Mod 汇总展示
  - 用户可在冲突列表中选择如何处理（升级/降级/忽略）
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QTreeWidget, QTreeWidgetItem,
    QProgressBar, QFileDialog, QMessageBox,
     QHeaderView
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QColor

from app.config import describe_category
from app.models.mod import ModLocation
from app.utils.version_utils import display_version
from app.services.mod_service import (
    ModService,
    BatchInstallResult,
    BatchItemStatus,
)
from app.services.manifest_service import DuplicateResult


# ═══════════════════════════════════════════════════════════
# 后台安装线程
# ═══════════════════════════════════════════════════════════

class BatchInstallWorker(QObject):
    """在后台线程中执行批量安装"""

    progress = pyqtSignal(int, int, str)      # (current, total, file_name)
    finished = pyqtSignal(object)             # BatchInstallResult

    def __init__(
        self,
        mod_service: ModService,
        file_list: list[Path],
        location: ModLocation,
        category: str | None = None,
    ):
        super().__init__()
        self._service = mod_service
        self._file_list = file_list
        self._location = location
        self._category = category  # F19 目标分类（仅 GAME 端使用）

    def run(self):
        result = self._service.batch_install(
            self._file_list,
            self._location,
            progress_callback=self._on_progress,
            category=self._category,
        )
        self.finished.emit(result)

    def _on_progress(self, current: int, total: int, name: str):
        self.progress.emit(current, total, name)


# ═══════════════════════════════════════════════════════════
# 批量添加主对话框
# ═══════════════════════════════════════════════════════════

class BatchInstallDialog(QDialog):
    """批量添加 Mod 对话框"""

    # 完成信号（外部可连接以刷新列表）
    install_finished = pyqtSignal()

    def __init__(
        self,
        mod_service: ModService,
        location: ModLocation,
        parent=None,
        category: str | None = None,
    ):
        super().__init__(parent)
        self._service = mod_service
        self._location = location
        self._category = category  # F19 目标分类（仅 GAME 端使用）
        self._file_list: list[Path] = []
        self._result: BatchInstallResult | None = None
        self._worker: BatchInstallWorker | None = None
        self._thread: QThread | None = None

        loc_label = "游戏" if location == ModLocation.GAME else "仓库"
        self.setWindowTitle(f"批量添加 Mod 到{loc_label}")
        self.setMinimumSize(700, 500)
        self.setModal(True)

        self._setup_ui()

    # ── UI 构建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # ── 标题 ──
        loc_text = "游戏 Mod 目录" if self._location == ModLocation.GAME else "本地仓库"
        if self._location == ModLocation.GAME and self._category:
            loc_text += f"（分类：{describe_category(self._category)}/）"
        title = QLabel(f"📦 批量添加 Mod → {loc_text}")
        title.setObjectName("title")
        layout.addWidget(title)

        self._add_separator(layout)

        # ── 选择区 ──
        select_frame = QFrame()
        select_frame.setObjectName("card")
        select_layout = QVBoxLayout(select_frame)
        select_layout.setSpacing(8)

        hint = QLabel("选择要添加的 .zipmod 文件（可多选），或选择一个文件夹（自动扫描其中所有 .zipmod）：")
        hint.setWordWrap(True)
        select_layout.addWidget(hint)

        btn_row = QHBoxLayout()
        self._select_files_btn = QPushButton("📄 选择文件（可多选）")
        self._select_files_btn.clicked.connect(self._on_select_files)
        btn_row.addWidget(self._select_files_btn)

        self._select_folder_btn = QPushButton("📂 选择文件夹")
        self._select_folder_btn.clicked.connect(self._on_select_folder)
        btn_row.addWidget(self._select_folder_btn)

        btn_row.addStretch()

        self._clear_btn = QPushButton("🗑 清空列表")
        self._clear_btn.clicked.connect(self._on_clear)
        self._clear_btn.setEnabled(False)
        btn_row.addWidget(self._clear_btn)

        select_layout.addLayout(btn_row)

        # 文件列表预览
        self._file_tree = QTreeWidget()
        self._file_tree.setHeaderLabels(["文件名", "路径"])
        self._file_tree.setColumnWidth(0, 260)
        self._file_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._file_tree.setMaximumHeight(140)
        # self._file_tree.setAlternatingRowColors(True)
        select_layout.addWidget(self._file_tree)

        self._file_count_label = QLabel("已选 0 个文件")
        self._file_count_label.setObjectName("subtitle")
        select_layout.addWidget(self._file_count_label)

        layout.addWidget(select_frame)

        # ── 进度区 ──
        self._progress_frame = QFrame()
        self._progress_frame.setObjectName("card")
        progress_layout = QVBoxLayout(self._progress_frame)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        progress_layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("等待开始...")
        self._progress_label.setObjectName("subtitle")
        progress_layout.addWidget(self._progress_label)

        self._progress_frame.setVisible(False)
        layout.addWidget(self._progress_frame)

        # ── 结果区（完成后显示）──
        self._result_frame = QFrame()
        self._result_frame.setObjectName("card")
        result_layout = QVBoxLayout(self._result_frame)
        result_layout.setSpacing(8)

        # 统计摘要
        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        result_layout.addWidget(self._summary_label)

        self._add_separator_widget(result_layout)

        # 冲突 / 跳过列表
        self._conflict_tree = QTreeWidget()
        self._conflict_tree.setHeaderLabels(["文件名", "Mod 名称", "已有版本", "待添加版本", "状态"])
        self._conflict_tree.setColumnWidth(0, 180)
        self._conflict_tree.setColumnWidth(1, 160)
        self._conflict_tree.setColumnWidth(2, 80)
        self._conflict_tree.setColumnWidth(3, 80)
        self._conflict_tree.setColumnWidth(4, 80)
        self._conflict_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # self._conflict_tree.setAlternatingRowColors(True)
        self._conflict_tree.setMaximumHeight(180)
        result_layout.addWidget(self._conflict_tree)

        # 冲突处理提示
        self._conflict_hint = QLabel(
            "💡 以上冲突项可在 Mod 管理页手动处理，"
            "或点击下方按钮一键升级所有冲突项（替换为待添加版本）。"
        )
        self._conflict_hint.setWordWrap(True)
        self._conflict_hint.setObjectName("subtitle")
        result_layout.addWidget(self._conflict_hint)

        # 冲突处理按钮
        conflict_btn_row = QHBoxLayout()
        self._upgrade_all_btn = QPushButton("⬆ 全部升级（替换为新版本）")
        self._upgrade_all_btn.setObjectName("success")
        self._upgrade_all_btn.clicked.connect(self._on_upgrade_all_conflicts)
        conflict_btn_row.addWidget(self._upgrade_all_btn)

        self._ignore_all_btn = QPushButton("忽略所有冲突")
        self._ignore_all_btn.clicked.connect(self._on_ignore_conflicts)
        conflict_btn_row.addWidget(self._ignore_all_btn)
        conflict_btn_row.addStretch()

        result_layout.addLayout(conflict_btn_row)

        self._result_frame.setVisible(False)
        layout.addWidget(self._result_frame)

        layout.addStretch()

        # ── 底部按钮 ──
        self._add_separator(layout)
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()

        self._start_btn = QPushButton("▶ 开始添加")
        self._start_btn.setObjectName("primary")
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        btn_bar.addWidget(self._start_btn)

        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self._on_close)
        btn_bar.addWidget(self._close_btn)

        layout.addLayout(btn_bar)

    # ── 辅助 ────────────────────────────────────────────────

    def _add_separator(self, layout: QVBoxLayout):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #45475a;")
        layout.addWidget(line)

    def _add_separator_widget(self, layout: QVBoxLayout):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #45475a;")
        layout.addWidget(line)

    # ── 选择文件 ─────────────────────────────────────────────

    def _on_select_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 zipmod 文件（可多选）",
            "",
            "zipmod 文件 (*.zipmod);;所有文件 (*.*)",
        )
        if not paths:
            return
        new_files = [Path(p) for p in paths]
        self._add_files(new_files)

    def _on_select_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择包含 zipmod 文件的文件夹",
            "",
        )
        if not folder:
            return
        found = self._service.collect_zipmods([Path(folder)])
        if not found:
            QMessageBox.information(self, "提示", f"文件夹中未找到任何 .zipmod 文件：\n{folder}")
            return
        self._add_files(found)

    def _add_files(self, new_files: list[Path]):
        """去重后追加文件到列表"""
        existing = {f.resolve() for f in self._file_list}
        added = 0
        for f in new_files:
            if f.resolve() not in existing:
                self._file_list.append(f)
                existing.add(f.resolve())
                added += 1

        self._refresh_file_list()
        if added == 0:
            self.statusBar().showMessage("所选文件已全部在列表中") if hasattr(self, "statusBar") else None

    def _on_clear(self):
        self._file_list.clear()
        self._refresh_file_list()

    def _refresh_file_list(self):
        self._file_tree.clear()
        for fp in self._file_list:
            item = QTreeWidgetItem()
            item.setText(0, fp.name)
            item.setText(1, str(fp.parent))
            self._file_tree.addTopLevelItem(item)

        count = len(self._file_list)
        self._file_count_label.setText(f"已选 {count} 个文件")
        self._start_btn.setEnabled(count > 0)
        self._clear_btn.setEnabled(count > 0)

    # ── 安装 ─────────────────────────────────────────────────

    def _on_start(self):
        if not self._file_list:
            return

        # 切换到进度模式
        self._select_files_btn.setEnabled(False)
        self._select_folder_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        self._start_btn.setEnabled(False)
        self._result_frame.setVisible(False)
        self._progress_frame.setVisible(True)
        self._progress_bar.setValue(0)
        self._progress_label.setText(f"准备处理 {len(self._file_list)} 个文件...")

        # 启动后台线程
        self._thread = QThread()
        self._worker = BatchInstallWorker(
            self._service,
            list(self._file_list),
            self._location,
            category=self._category,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, current: int, total: int, name: str):
        if total > 0:
            pct = int(current / total * 100)
            self._progress_bar.setValue(pct)
        if name == "完成":
            self._progress_label.setText("处理完成，正在整理结果...")
        else:
            self._progress_label.setText(
                f"正在处理 [{current + 1}/{total}]：{name}"
            )

    def _on_finished(self, result: BatchInstallResult):
        self._result = result
        self._progress_bar.setValue(100)
        self._progress_label.setText("✅ 全部处理完成")

        # 恢复按钮
        self._select_files_btn.setEnabled(True)
        self._select_folder_btn.setEnabled(True)

        # 展示结果
        self._show_result(result)

        # 通知主窗口刷新列表
        self.install_finished.emit()

    # ── 结果展示 ──────────────────────────────────────────────

    def _show_result(self, result: BatchInstallResult):
        """展示批量安装结果"""
        loc_text = "游戏" if self._location == ModLocation.GAME else "仓库"

        # 摘要文字
        parts = [
            f"<b>处理完毕（目标：{loc_text}）</b>",
            f"总计 {result.total} 个文件：",
            f"✅ 成功添加 {result.success_count} 个",
        ]
        if result.skipped_count:
            parts.append(f"⏭ 跳过（已存在同版本）{result.skipped_count} 个")
        if result.conflict_count:
            parts.append(f"⚠ 版本冲突 {result.conflict_count} 个（需处理）")
        if result.failed_count:
            parts.append(f"❌ 失败 {result.failed_count} 个")

        self._summary_label.setText("   |   ".join(parts[1:]))
        self._summary_label.setWordWrap(True)

        # 填充冲突/跳过/失败列表
        self._conflict_tree.clear()
        has_conflict = False

        for item in result.items:
            if item.status == BatchItemStatus.SUCCESS:
                continue  # 成功的不展示

            tree_item = QTreeWidgetItem()
            tree_item.setText(0, item.file_path.name)
            tree_item.setText(1, item.manifest.name if item.manifest else "—")

            if item.status == BatchItemStatus.SKIPPED:
                dup = item.duplicate_report
                tree_item.setText(2, display_version(dup.existing_version) if dup else "—")
                tree_item.setText(3, display_version(dup.incoming_version) if dup else "—")
                tree_item.setText(4, "⏭ 已跳过")
                tree_item.setForeground(4, QColor("#a6adc8"))

            elif item.status == BatchItemStatus.CONFLICT:
                dup = item.duplicate_report
                existing_v = display_version(dup.existing_version) if dup else "—"
                incoming_v = display_version(dup.incoming_version) if dup else "—"
                tree_item.setText(2, existing_v)
                tree_item.setText(3, incoming_v)

                if dup and dup.result == DuplicateResult.NEWER_VERSION:
                    tree_item.setText(4, "🔄 可升级")
                    tree_item.setForeground(4, QColor("#a6e3a1"))
                elif dup and dup.result == DuplicateResult.OLDER_VERSION:
                    tree_item.setText(4, "⬇ 低版本")
                    tree_item.setForeground(4, QColor("#f9e2af"))
                else:
                    tree_item.setText(4, "❓ 版本未知")
                    tree_item.setForeground(4, QColor("#f9e2af"))

                # 存储原始数据用于"全部升级"操作
                tree_item.setData(0, Qt.ItemDataRole.UserRole, item)
                has_conflict = True

            elif item.status == BatchItemStatus.FAILED:
                tree_item.setText(2, "—")
                tree_item.setText(3, "—")
                tree_item.setText(4, "❌ 失败")
                tree_item.setForeground(4, QColor("#f38ba8"))
                tree_item.setToolTip(0, item.message)

            self._conflict_tree.addTopLevelItem(tree_item)

        # 显隐冲突处理按钮
        self._upgrade_all_btn.setVisible(has_conflict)
        self._ignore_all_btn.setVisible(has_conflict)
        self._conflict_hint.setVisible(
            result.conflict_count > 0 or result.skipped_count > 0 or result.failed_count > 0
        )

        self._result_frame.setVisible(True)

    # ── 冲突处理 ──────────────────────────────────────────────

    def _on_upgrade_all_conflicts(self):
        """强制升级所有冲突项（无论是升级还是降级，都替换）"""
        if self._result is None:
            return

        conflicts = self._result.conflicts
        if not conflicts:
            return

        count = len(conflicts)
        confirm = QMessageBox.question(
            self,
            "确认替换",
            f"将强制替换所有 {count} 个冲突 Mod（无论版本高低均会替换）。\n\n确认继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        success_count = 0
        fail_msgs: list[str] = []

        for item in conflicts:
            if item.manifest is None:
                continue
            # 使用 confirm_upgrade=True + confirm_downgrade=True 强制安装
            if self._location == ModLocation.GAME:
                result = self._service.install_to_game(
                    item.file_path,
                    confirm_upgrade=True,
                    confirm_downgrade=True,
                )
            else:
                result = self._service.add_to_repo(
                    item.file_path,
                    confirm_upgrade=True,
                    confirm_downgrade=True,
                )

            if result.success:
                success_count += 1
                # 更新列表中该项状态
                item.status = BatchItemStatus.SUCCESS
                item.message = result.message
            else:
                fail_msgs.append(f"{item.file_path.name}: {result.message}")

        # 更新统计
        self._result.success_count += success_count
        self._result.conflict_count -= success_count

        msg = f"替换完成：{success_count} 个成功"
        if fail_msgs:
            msg += f"\n以下文件处理失败：\n" + "\n".join(fail_msgs)

        QMessageBox.information(self, "替换结果", msg)

        # 重新渲染结果
        self._show_result(self._result)
        self.install_finished.emit()

    def _on_ignore_conflicts(self):
        """忽略所有冲突（直接关闭提示区）"""
        self._upgrade_all_btn.setVisible(False)
        self._ignore_all_btn.setVisible(False)
        self._conflict_hint.setVisible(False)

    # ── 关闭 ─────────────────────────────────────────────────

    def _on_close(self):
        if self._thread and self._thread.isRunning():
            QMessageBox.warning(self, "提示", "正在处理中，请等待完成后再关闭。")
            return
        self.accept()

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            event.ignore()
            QMessageBox.warning(self, "提示", "正在处理中，请等待完成后再关闭。")
            return
        event.accept()
