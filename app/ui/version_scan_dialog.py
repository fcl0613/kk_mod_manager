"""仓库版本巡检对话框（F24）

全表对比 GAME 与 REPO 同 GUID Mod 的版本差异：
  - 仓库更新（可升级）：勾选 + 一键升级，逐项复用 install_from_repo
    （F04-8 防丢序：先复制新版本文件，成功后才删旧版）
  - 游戏更新（降级方向）：仅参考展示，不提供操作（F24-3）

后台 QThread 执行（复刻 SyncDialog 的 worker 模式），
worker 线程内自建 session，不与主窗口共享。
"""

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
    BatchUpgradeResult,
    ModService,
    VersionDiffItem,
    VersionScanReport,
)
from app.utils.version_utils import display_version

# 状态列前景色（对齐 card_result_dialog 的语义色）
_GREEN = QColor("#a6e3a1")     # 已升级 / 成功
_YELLOW = QColor("#f9e2af")    # 待升级
_GRAY = QColor("#a6adc8")      # 跳过 / 仅参考
_RED = QColor("#f38ba8")       # 失败


# ═══════════════════════════════════════════════════════════
# 后台线程 worker（SyncWorker 模式：自建 session）
# ═══════════════════════════════════════════════════════════

class VersionScanWorker(QObject):
    """在后台线程中执行仓库版本巡检（纯 DB 对比 + 聚合日志）"""

    finished = pyqtSignal(object)             # VersionScanReport
    error = pyqtSignal(str)

    def run(self):
        session = get_session()
        try:
            self.finished.emit(ModService(session).scan_repo_versions())
        except Exception as e:  # noqa: BLE001 - 后台线程兜底，任何异常都要回传 UI
            self.error.emit(str(e))
        finally:
            session.close()


class BatchUpgradeWorker(QObject):
    """在后台线程中批量升级（逐项复用 install_from_repo）"""

    progress = pyqtSignal(int, int, str)      # (current, total, name)，末次哨兵名 "完成"
    finished = pyqtSignal(object)             # BatchUpgradeResult
    error = pyqtSignal(str)

    def __init__(self, repo_mod_ids: list[int]):
        super().__init__()
        self._repo_mod_ids = repo_mod_ids

    def run(self):
        session = get_session()
        try:
            result = ModService(session).batch_upgrade_from_repo(
                self._repo_mod_ids,
                progress_callback=lambda c, t, n: self.progress.emit(c, t, n),
            )
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001 - 后台线程兜底，任何异常都要回传 UI
            self.error.emit(str(e))
        finally:
            session.close()


# ═══════════════════════════════════════════════════════════
# 巡检主对话框（四阶段：scanning → results → upgrading → done）
# ═══════════════════════════════════════════════════════════

class VersionScanDialog(QDialog):
    """仓库版本巡检 / 一键升级（打开即自动开始巡检）"""

    # 升级成功后通知外部刷新（worker 用独立 session 提交，外部需 expire 后重查）
    upgrade_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("仓库版本巡检")
        self.setMinimumSize(760, 560)
        self.setModal(True)

        self._report: VersionScanReport | None = None
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._running = False
        self._row_by_guid: dict[str, QTreeWidgetItem] = {}  # 仓库更新区 guid → 行

        self._setup_ui()
        self._start_scan()

    # ── UI 构建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("🔍 仓库版本巡检")
        title.setObjectName("title")
        layout.addWidget(title)

        self._status_label = QLabel("正在对比游戏与仓库的同 GUID Mod 版本...")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # 扫描期不确定模式
        layout.addWidget(self._progress_bar)

        # 结果区（巡检完成后显示）
        self._result_frame = QFrame()
        self._result_frame.setObjectName("card")
        self._result_frame.setVisible(False)
        result_layout = QVBoxLayout(self._result_frame)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        result_layout.addWidget(self._summary_label)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["", "Mod 名称", "作者", "游戏版本", "仓库版本", "状态"])
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._tree.itemChanged.connect(self._on_item_changed)
        result_layout.addWidget(self._tree, stretch=1)

        # 勾选操作行：全选 / 清空 / 已选计数
        select_row = QHBoxLayout()
        self._select_all_btn = QPushButton("全选")
        self._select_all_btn.clicked.connect(self._select_all)
        self._clear_btn = QPushButton("清空选择")
        self._clear_btn.clicked.connect(self._clear_selection)
        self._checked_label = QLabel("已选 0 / 0 项")
        select_row.addWidget(self._select_all_btn)
        select_row.addWidget(self._clear_btn)
        select_row.addStretch(1)
        select_row.addWidget(self._checked_label)
        result_layout.addLayout(select_row)

        self._upgrade_btn = QPushButton("⬆ 一键升级")
        self._upgrade_btn.setObjectName("success")
        self._upgrade_btn.clicked.connect(self._on_upgrade)
        result_layout.addWidget(self._upgrade_btn)

        layout.addWidget(self._result_frame, stretch=1)

        self._close_btn = QPushButton("关闭")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self._on_close)
        layout.addWidget(self._close_btn)

    # ── 阶段 1：巡检 ────────────────────────────────────────

    def _start_scan(self):
        self._running = True
        self._thread = QThread()
        self._worker = VersionScanWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_scan_finished(self, report: VersionScanReport):
        self._report = report
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(100)
        self._status_label.setText("✅ 巡检完成")
        self._running = False  # 先解除运行态，_build_tree 末尾的按钮联动才能生效

        self._summary_label.setText(
            report.summary()
            + ("" if (report.repo_newer or report.game_newer)
               else "\n\n游戏与仓库版本一致，无需升级。")
        )
        self._build_tree(report)

        empty = not report.repo_newer
        if empty:
            self._upgrade_btn.setVisible(False)
            self._select_all_btn.setVisible(False)
            self._clear_btn.setVisible(False)
            self._checked_label.setVisible(False)

        self._result_frame.setVisible(True)
        self._close_btn.setEnabled(True)

    def _build_tree(self, report: VersionScanReport):
        self._tree.blockSignals(True)  # 建树过程中的勾选设置不触发计数刷新
        self._tree.clear()

        if report.repo_newer:
            group = self._add_group(f"🔄 仓库更新（可升级）· {len(report.repo_newer)} 项")
            for item in report.repo_newer:
                child = QTreeWidgetItem([
                    "", item.name, item.author,
                    display_version(item.game_version),
                    display_version(item.repo_version),
                    "待升级",
                ])
                child.setCheckState(0, Qt.CheckState.Checked)  # 默认全勾（F24-2）
                child.setData(0, Qt.ItemDataRole.UserRole, item)
                child.setToolTip(0, f"GUID: {item.guid}")
                child.setForeground(5, _YELLOW)
                group.addChild(child)
                self._row_by_guid[item.guid] = child
            group.setExpanded(True)

        if report.game_newer:
            # 降级方向：仅参考，不带勾选框（F24-3）
            group = self._add_group(f"⬇ 游戏更新（仅参考，不可操作）· {len(report.game_newer)} 项")
            for item in report.game_newer:
                child = QTreeWidgetItem([
                    "", item.name, item.author,
                    display_version(item.game_version),
                    display_version(item.repo_version),
                    "游戏侧更新（仅参考）",
                ])
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                child.setToolTip(0, f"GUID: {item.guid}")
                child.setForeground(5, _GRAY)
                group.addChild(child)
            group.setExpanded(True)

        self._tree.blockSignals(False)
        self._refresh_checked_state()

    def _add_group(self, text: str) -> QTreeWidgetItem:
        """分组表头（对齐 CardResultDialog._add_group 惯例）"""
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
            return  # 分组表头 / 仅参考区不参与计数
        self._refresh_checked_state()

    def _refresh_checked_state(self):
        children = self._upgradable_items()
        checked = sum(
            1 for c in children if c.checkState(0) == Qt.CheckState.Checked
        )
        self._checked_label.setText(f"已选 {checked} / {len(children)} 项")
        self._upgrade_btn.setEnabled(checked > 0 and not self._running)

    def _upgradable_items(self) -> list[QTreeWidgetItem]:
        """仓库更新组的子项（第一个顶层组）"""
        if self._tree.topLevelItemCount() == 0:
            return []
        group = self._tree.topLevelItem(0)
        return [group.child(i) for i in range(group.childCount())]

    def _select_all(self):
        self._tree.blockSignals(True)
        for child in self._upgradable_items():
            child.setCheckState(0, Qt.CheckState.Checked)
        self._tree.blockSignals(False)
        self._refresh_checked_state()

    def _clear_selection(self):
        self._tree.blockSignals(True)
        for child in self._upgradable_items():
            child.setCheckState(0, Qt.CheckState.Unchecked)
        self._tree.blockSignals(False)
        self._refresh_checked_state()

    # ── 阶段 2/3：一键升级 ─────────────────────────────────

    def _on_upgrade(self):
        checked_items = [
            c for c in self._upgradable_items()
            if c.checkState(0) == Qt.CheckState.Checked
        ]
        if not checked_items or self._running:
            return
        count = len(checked_items)

        answer = QMessageBox.question(
            self,
            "确认一键升级",
            f"将把选中的 {count} 个 Mod 升级到仓库版本：\n\n"
            "  • 先复制新版本文件到游戏目录，成功后才删除旧版本（防丢序）\n"
            "  • 每项独立提交并记录操作日志，单项失败不影响其余项\n\n"
            "确认继续升级？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        repo_mod_ids = [
            c.data(0, Qt.ItemDataRole.UserRole).repo_mod_id for c in checked_items
        ]
        self._select_all_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        self._upgrade_btn.setEnabled(False)
        self._status_label.setText("正在升级...")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._running = True
        self._close_btn.setEnabled(False)

        self._thread = QThread()
        self._worker = BatchUpgradeWorker(repo_mod_ids)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_upgrade_progress)
        self._worker.finished.connect(self._on_upgrade_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_upgrade_progress(self, current: int, total: int, name: str):
        if total > 0:
            self._progress_bar.setValue(int(current / total * 100))
        if name == "完成":
            self._status_label.setText("升级执行完毕，正在整理结果...")
        else:
            self._status_label.setText(f"正在升级 [{current + 1}/{total}]：{name}")

    def _on_upgrade_finished(self, result: BatchUpgradeResult):
        self._progress_bar.setValue(100)
        self._status_label.setText(
            f"✅ 升级完成：成功 {result.success_count} · 跳过 {result.skipped_count} · 失败 {result.failed_count}"
        )

        status_styles = {
            BatchItemStatus.SUCCESS: ("✅ 已升级", _GREEN),
            BatchItemStatus.SKIPPED: ("⏭ 已跳过（游戏已是该版本）", _GRAY),
            BatchItemStatus.FAILED: ("❌ 失败", _RED),
        }
        for item in result.items:
            row = self._row_by_guid.get(item.guid)
            if row is None:
                continue
            text, color = status_styles.get(item.status, (str(item.status), _GRAY))
            row.setText(5, text)
            row.setForeground(5, color)
            if item.message:
                row.setToolTip(5, item.message)
            # 升级完成后移除勾选能力，防止对已处理项再次升级
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

        self._checked_label.setVisible(False)
        if result.failed_count > 0:
            QMessageBox.information(
                self,
                "升级结果",
                f"成功 {result.success_count} 项，失败 {result.failed_count} 项。\n"
                "失败原因请将鼠标悬停在对应行的「状态」列查看。",
            )

        self._running = False
        self._close_btn.setEnabled(True)
        if result.success_count > 0:
            self.upgrade_finished.emit()

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
