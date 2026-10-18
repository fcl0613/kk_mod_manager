"""重复项检查 / 清理对话框（F22）

扫描 GAME / REPO 磁盘文件并解析 manifest，按 GUID 分组找出**同位置内**
的多个文件（判重以磁盘为准，不查库——DB 唯一索引会漏报同位置多文件）：
  - 按 GUID 分组列出文件 / 版本 / 大小 / 修改时间 / 禁用态 / MD5 相同标注
  - 默认预勾选「保留最新版」的建议删除项（F22-4），用户确认后才执行
  - 首期硬删除（F22-5），删除后自动增量同步数据库对账

后台 QThread 执行（复刻 VersionScanDialog 的 worker 模式），
worker 线程内自建 session，不与主窗口共享。
"""

from datetime import datetime

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QFrame,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QHeaderView,
)

from app.database import get_session
from app.services.mod_service import (
    BatchItemStatus,
    DuplicateCleanupResult,
    DuplicateFileEntry,
    DuplicateGroup,
    DuplicateScanReport,
    ModService,
)
from app.utils.version_utils import display_version

# 标注列前景色（对齐 version_scan_dialog 的语义色）
_GREEN = QColor("#a6e3a1")     # 建议保留 / 已删除
_YELLOW = QColor("#f9e2af")    # MD5 相同副本 / 禁用
_GRAY = QColor("#a6adc8")      # 跳过
_RED = QColor("#f38ba8")       # 失败


def _fmt_size(size: int) -> str:
    """字节数 → 人类可读（对齐文件管理器口径）"""
    if size >= 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024 / 1024:.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


# ═══════════════════════════════════════════════════════════
# 后台线程 worker（VersionScanWorker 模式：自建 session）
# ═══════════════════════════════════════════════════════════

class DuplicateScanWorker(QObject):
    """在后台线程中执行重复项扫描（磁盘扫描 + manifest 解析 + 组内 MD5）"""

    progress = pyqtSignal(int, int, str)      # (current, total, name)
    finished = pyqtSignal(object)             # DuplicateScanReport
    error = pyqtSignal(str)

    def run(self):
        session = get_session()
        try:
            report = ModService(session).scan_duplicates(
                progress_callback=lambda c, t, n: self.progress.emit(c, t, n),
            )
            self.finished.emit(report)
        except Exception as e:  # noqa: BLE001 - 后台线程兜底，任何异常都要回传 UI
            self.error.emit(str(e))
        finally:
            session.close()


class DuplicateCleanupWorker(QObject):
    """在后台线程中删除所选文件并增量对账数据库"""

    progress = pyqtSignal(int, int, str)      # (current, total, name)
    finished = pyqtSignal(object)             # DuplicateCleanupResult
    error = pyqtSignal(str)

    def __init__(self, entries: list[DuplicateFileEntry]):
        super().__init__()
        self._entries = entries

    def run(self):
        session = get_session()
        try:
            result = ModService(session).cleanup_duplicates(
                self._entries,
                progress_callback=lambda c, t, n: self.progress.emit(c, t, n),
            )
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001 - 后台线程兜底，任何异常都要回传 UI
            self.error.emit(str(e))
        finally:
            session.close()


# ═══════════════════════════════════════════════════════════
# 主对话框（四阶段：scanning → results → cleaning → done）
# ═══════════════════════════════════════════════════════════

class DuplicateScanDialog(QDialog):
    """重复项检查 / 清理（打开即自动开始扫描）"""

    # 清理成功后通知外部刷新（worker 用独立 session 提交，外部需 expire 后重查）
    cleanup_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("重复项检查")
        self.setMinimumSize(860, 600)
        self.setModal(True)

        self._report: DuplicateScanReport | None = None
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._running = False
        self._row_by_path: dict[str, QTreeWidgetItem] = {}  # 绝对路径 → 行

        self._setup_ui()
        self._start_scan()

    # ── UI 构建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("🧹 重复项检查")
        title.setObjectName("title")
        layout.addWidget(title)

        self._status_label = QLabel("正在扫描游戏与仓库的磁盘文件并解析 manifest...")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # 扫描期不确定模式
        layout.addWidget(self._progress_bar)

        # 结果区（扫描完成后显示）
        self._result_frame = QFrame()
        self._result_frame.setObjectName("card")
        self._result_frame.setVisible(False)
        result_layout = QVBoxLayout(self._result_frame)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        result_layout.addWidget(self._summary_label)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["", "文件", "版本", "大小", "修改时间", "标注"])
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.itemChanged.connect(self._on_item_changed)
        result_layout.addWidget(self._tree, stretch=1)

        # 勾选操作行：恢复建议 / 清空 / 已选计数
        select_row = QHBoxLayout()
        self._select_suggested_btn = QPushButton("勾选建议删除项")
        self._select_suggested_btn.clicked.connect(self._select_suggested)
        self._clear_btn = QPushButton("清空选择")
        self._clear_btn.clicked.connect(self._clear_selection)
        self._checked_label = QLabel("已选 0 项")
        select_row.addWidget(self._select_suggested_btn)
        select_row.addWidget(self._clear_btn)
        select_row.addStretch(1)
        select_row.addWidget(self._checked_label)
        result_layout.addLayout(select_row)

        self._delete_btn = QPushButton("🗑 删除所选文件")
        self._delete_btn.setObjectName("danger")
        self._delete_btn.clicked.connect(self._on_delete)
        result_layout.addWidget(self._delete_btn)

        layout.addWidget(self._result_frame, stretch=1)

        self._close_btn = QPushButton("关闭")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self._on_close)
        layout.addWidget(self._close_btn)

    # ── 阶段 1：扫描 ────────────────────────────────────────

    def _start_scan(self):
        self._running = True
        self._thread = QThread()
        self._worker = DuplicateScanWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_scan_progress(self, current: int, total: int, name: str):
        if total > 0:
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(int(current / total * 100))
            self._status_label.setText(f"正在扫描 [{current + 1}/{total}]：{name}")

    def _on_scan_finished(self, report: DuplicateScanReport):
        self._report = report
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(100)
        self._status_label.setText("✅ 扫描完成")
        self._running = False  # 先解除运行态，_build_tree 末尾的按钮联动才能生效

        self._summary_label.setText(
            report.summary()
            + ("" if report.groups else "\n\n游戏与仓库内均未发现同 GUID 的重复文件。")
        )
        self._build_tree(report)

        empty = not report.groups
        if empty:
            self._tree.setVisible(False)
            self._select_suggested_btn.setVisible(False)
            self._clear_btn.setVisible(False)
            self._checked_label.setVisible(False)
            self._delete_btn.setVisible(False)

        self._result_frame.setVisible(True)
        self._close_btn.setEnabled(True)

    @staticmethod
    def _group_header_text(group: DuplicateGroup) -> str:
        loc = "🎮 游戏" if group.location.value == "game" else "🗄 仓库"
        author = group.author.strip() or "Unknown"
        return f"{loc} · {group.name}（{author}）· {len(group.files)} 个文件"

    @staticmethod
    def _entry_badge(entry: DuplicateFileEntry) -> str:
        """标注列文本：保留建议 + 禁用态 + MD5 相同（F22-4）"""
        parts = ["⭐ 建议保留" if entry.suggested_keep else "建议删除"]
        if entry.has_identical_twin:
            parts.append("🔁 MD5 相同")
        if entry.is_disabled:
            parts.append("🚫 已禁用")
        return " · ".join(parts)

    def _build_tree(self, report: DuplicateScanReport):
        self._tree.blockSignals(True)  # 建树过程中的勾选设置不触发计数刷新
        self._tree.clear()
        self._row_by_path.clear()

        for group in report.groups:
            header_item = self._add_group(self._group_header_text(group))
            header_item.setToolTip(
                0, f"GUID: {group.guid}\n位置: {group.location.value}"
            )
            for entry in group.files:
                mtime_text = datetime.fromtimestamp(entry.mtime).strftime(
                    "%Y-%m-%d %H:%M"
                )
                child = QTreeWidgetItem([
                    "",
                    entry.relative_path,
                    display_version(entry.version),
                    _fmt_size(entry.size),
                    mtime_text,
                    self._entry_badge(entry),
                ])
                # 默认预勾选建议删除项，保留项不勾（F22-4；用户仍可手动改选）
                child.setCheckState(
                    0,
                    Qt.CheckState.Unchecked if entry.suggested_keep
                    else Qt.CheckState.Checked,
                )
                child.setData(0, Qt.ItemDataRole.UserRole, entry)
                child.setToolTip(
                    0,
                    f"{entry.path}\nMD5: {entry.md5 or '(计算失败)'}",
                )
                if entry.suggested_keep:
                    child.setForeground(5, _GREEN)
                elif entry.has_identical_twin:
                    child.setForeground(5, _YELLOW)
                header_item.addChild(child)
                self._row_by_path[str(entry.path)] = child
            header_item.setExpanded(True)

        self._tree.blockSignals(False)
        self._refresh_checked_state()

    def _add_group(self, text: str) -> QTreeWidgetItem:
        """分组表头（对齐 VersionScanDialog._add_group 惯例）"""
        group = QTreeWidgetItem([text])
        font = group.font(0)
        font.setBold(True)
        group.setFont(0, font)
        group.setFirstColumnSpanned(True)
        self._tree.addTopLevelItem(group)
        return group

    # ── 勾选联动 ────────────────────────────────────────────

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int):
        if item.data(0, Qt.ItemDataRole.UserRole) is None:
            return  # 分组表头不参与计数
        self._refresh_checked_state()

    def _refresh_checked_state(self):
        entries = self._file_entries()
        checked = [
            e for e in entries
            if self._row_by_path[str(e.path)].checkState(0) == Qt.CheckState.Checked
        ]
        size = sum(e.size for e in checked)
        self._checked_label.setText(
            f"已选 {len(checked)} / {len(entries)} 项 · 约 {_fmt_size(size)}"
        )
        self._delete_btn.setEnabled(len(checked) > 0 and not self._running)

    def _file_entries(self) -> list[DuplicateFileEntry]:
        """扫描报告中的全部文件项（按组顺序）"""
        if self._report is None:
            return []
        return [f for g in self._report.groups for f in g.files]

    def _select_suggested(self):
        """恢复默认勾选：建议删除项全选、保留项不勾（F22-4）"""
        self._tree.blockSignals(True)
        for entry in self._file_entries():
            row = self._row_by_path[str(entry.path)]
            row.setCheckState(
                0,
                Qt.CheckState.Unchecked if entry.suggested_keep
                else Qt.CheckState.Checked,
            )
        self._tree.blockSignals(False)
        self._refresh_checked_state()

    def _clear_selection(self):
        self._tree.blockSignals(True)
        for entry in self._file_entries():
            self._row_by_path[str(entry.path)].setCheckState(0, Qt.CheckState.Unchecked)
        self._tree.blockSignals(False)
        self._refresh_checked_state()

    # ── 阶段 2/3：删除所选 ──────────────────────────────────

    def _checked_entries(self) -> list[DuplicateFileEntry]:
        return [
            e for e in self._file_entries()
            if self._row_by_path[str(e.path)].checkState(0) == Qt.CheckState.Checked
        ]

    def _fully_selected_group_count(self, entries: list[DuplicateFileEntry]) -> int:
        """全部文件都被勾选的组数（该 Mod 将被彻底移除，确认时警示）"""
        if self._report is None:
            return 0
        selected = {str(e.path) for e in entries}
        return sum(
            1 for g in self._report.groups
            if all(str(f.path) in selected for f in g.files)
        )

    def _on_delete(self):
        checked = self._checked_entries()
        if not checked or self._running:
            return
        size = sum(e.size for e in checked)

        warning = ""
        full_groups = self._fully_selected_group_count(checked)
        if full_groups:
            warning += (
                f"\n⚠ 其中有 {full_groups} 组的所有文件都被选中，"
                f"这些 Mod 将被彻底移除！"
            )

        answer = QMessageBox.question(
            self,
            "确认删除重复文件",
            f"将永久删除选中的 {len(checked)} 个文件（约 {_fmt_size(size)}）：\n\n"
            "  • 首期为硬删除，文件不会进入回收站，删除后无法恢复\n"
            "  • 删除完成后将自动同步数据库，把记录对准磁盘幸存文件\n"
            f"{warning}\n确认继续删除？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._select_suggested_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        self._delete_btn.setEnabled(False)
        self._status_label.setText("正在删除所选文件并对账数据库...")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._running = True
        self._close_btn.setEnabled(False)

        self._thread = QThread()
        self._worker = DuplicateCleanupWorker(checked)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_cleanup_progress)
        self._worker.finished.connect(self._on_cleanup_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_cleanup_progress(self, current: int, total: int, name: str):
        if total > 0:
            self._progress_bar.setValue(int(current / total * 100))
        if name == "完成":
            self._status_label.setText("删除执行完毕，正在对账数据库...")
        else:
            self._status_label.setText(f"正在处理 [{current + 1}/{total}]：{name}")

    def _on_cleanup_finished(self, result: DuplicateCleanupResult):
        self._progress_bar.setValue(100)
        status_text = (
            f"✅ 清理完成：删除 {result.success_count} · 跳过 {result.skipped_count} · "
            f"失败 {result.failed_count}"
        )
        for sync_report in result.sync_reports:
            status_text += f"\n{sync_report.summary()}"
        if result.sync_errors:
            status_text += (
                "\n⚠ 数据库对账失败：" + "；".join(result.sync_errors)
                + "——请稍后手动执行「同步磁盘」"
            )
        self._status_label.setText(status_text)

        status_styles = {
            BatchItemStatus.SUCCESS: ("✅ 已删除", _GREEN),
            BatchItemStatus.SKIPPED: ("⏭ 已跳过", _GRAY),
            BatchItemStatus.FAILED: ("❌ 失败", _RED),
        }
        for item in result.items:
            row = self._row_by_path.get(str(item.entry.path))
            if row is None:
                continue
            text, color = status_styles.get(
                item.status, (str(item.status), _GRAY)
            )
            row.setText(5, text)
            row.setForeground(5, color)
            if item.message:
                row.setToolTip(5, item.message)
            # 清理完成后移除勾选能力，防止对已处理项再次删除
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

        self._checked_label.setVisible(False)
        if result.failed_count > 0 or result.sync_errors:
            QMessageBox.information(
                self,
                "清理结果",
                f"删除 {result.success_count} 项，跳过 {result.skipped_count} 项，"
                f"失败 {result.failed_count} 项。\n"
                "失败原因请将鼠标悬停在对应行的「标注」列查看。",
            )

        self._running = False
        self._close_btn.setEnabled(True)
        if result.success_count > 0:
            self.cleanup_finished.emit()

    def _on_error(self, message: str):
        self._progress_bar.setRange(0, 100)
        self._status_label.setText(f"❌ 执行失败：{message}")
        self._running = False
        self._close_btn.setEnabled(True)

    # ── 关闭（运行中阻断，无中途取消）──────────────────────

    def _on_close(self):
        if self._thread and self._thread.isRunning():
            return  # 运行中禁止关闭（按钮此时已禁用，防御性兜底）
        self.accept()

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            event.ignore()
            return
        event.accept()
