"""磁盘同步对话框

以磁盘文件为准增量同步数据库（GAME + REPO 两位置）：
  - 磁盘新增的 zipmod → 导入数据库
  - 磁盘已消失的文件 → 删除对应记录
  - 文件改名/移动/换版本/启用禁用 → 更新记录（保留备注）

后台 QThread 执行（复刻 BatchInstallDialog 的 worker 模式），
worker 线程内自建 session，不与主窗口共享。
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout,
    QLabel, QPushButton, QFrame, QTreeWidget, QTreeWidgetItem,
    QProgressBar, QHeaderView,
)
from PyQt6.QtCore import QThread, pyqtSignal, QObject
from PyQt6.QtGui import QColor

from app.database import get_session
from app.models.mod import ModLocation
from app.services.mod_service import (
    ModService,
    SyncReport,
    SyncChangeItem,
)


# ═══════════════════════════════════════════════════════════
# 后台同步线程
# ═══════════════════════════════════════════════════════════

class SyncWorker(QObject):
    """在后台线程中依次同步 REPO 与 GAME"""

    progress = pyqtSignal(int, int, str)      # (current, total, 带位置前缀的文件名)
    finished = pyqtSignal(object)             # list[SyncReport]
    error = pyqtSignal(str)

    def run(self):
        session = get_session()
        try:
            svc = ModService(session)
            reports: list[SyncReport] = []
            for loc, tag in ((ModLocation.REPO, "仓库"), (ModLocation.GAME, "游戏")):
                def on_progress(current: int, total: int, name: str, _tag=tag):
                    self.progress.emit(current, total, f"[{_tag}] {name}")
                reports.append(svc.sync_from_disk(loc, on_progress))
            self.finished.emit(reports)
        except Exception as e:  # noqa: BLE001 - 后台线程兜底，任何异常都要回传 UI
            self.error.emit(str(e))
        finally:
            session.close()


# ═══════════════════════════════════════════════════════════
# 同步主对话框
# ═══════════════════════════════════════════════════════════

# 明细树中操作类型 → (显示文本, 前景色)
_ACTION_STYLES = {
    "added": ("➕ 新增", QColor("#2e7d32")),
    "removed": ("🗑 删除", QColor("#c62828")),
    "updated": ("✏ 更新", QColor("#1565c0")),
    "parse_failed": ("⚠ 解析失败", QColor("#e65100")),
}


class SyncDialog(QDialog):
    """磁盘同步对话框（打开即自动开始同步）"""

    # 完成信号（外部连接以刷新列表）
    sync_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("同步磁盘 Mod")
        self.setMinimumSize(640, 420)
        self.setModal(True)

        self._thread: QThread | None = None
        self._worker: SyncWorker | None = None
        self._had_changes = False

        self._setup_ui()
        self._start()

    # ── UI 构建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("正在以磁盘文件为准同步数据库（不改动磁盘文件）...")
        title.setWordWrap(True)
        layout.addWidget(title)

        self._progress_bar = QProgressBar()
        layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("准备扫描...")
        self._progress_label.setWordWrap(True)
        layout.addWidget(self._progress_label)

        # 结果区（同步完成后显示）
        self._result_frame = QFrame()
        self._result_frame.setVisible(False)
        result_layout = QVBoxLayout(self._result_frame)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        result_layout.addWidget(self._summary_label)

        self._detail_tree = QTreeWidget()
        self._detail_tree.setHeaderLabels(["操作", "Mod 名称", "文件", "说明"])
        header = self._detail_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._detail_tree.setRootIsDecorated(False)
        result_layout.addWidget(self._detail_tree)

        layout.addWidget(self._result_frame, stretch=1)

        self._close_btn = QPushButton("关闭")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self._on_close)
        layout.addWidget(self._close_btn)

    # ── 线程启动 / 结束 ─────────────────────────────────────

    def _start(self):
        self._thread = QThread()
        self._worker = SyncWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_progress(self, current: int, total: int, name: str):
        if total > 0:
            self._progress_bar.setValue(int(current / total * 100))
        if name.endswith("完成"):
            self._progress_label.setText("正在整理结果...")
        else:
            self._progress_label.setText(f"正在扫描 [{current + 1}/{total}]：{name}")

    def _on_finished(self, reports: list[SyncReport]):
        self._progress_bar.setValue(100)
        self._progress_label.setText("✅ 同步完成")

        summaries = [r.summary() for r in reports]
        self._summary_label.setText("\n".join(summaries))

        for report in reports:
            groups = (
                ("added", report.added),
                ("removed", report.removed),
                ("updated", report.updated),
                ("parse_failed", report.parse_failures),
            )
            for action, items in groups:
                for item in items:
                    self._append_detail(action, item)

        if self._detail_tree.topLevelItemCount() == 0:
            self._detail_tree.setVisible(False)
            self._summary_label.setText(
                "\n".join(summaries) + "\n\n磁盘与数据库已一致，无需变更。"
            )

        self._had_changes = any(r.change_count for r in reports)
        self._result_frame.setVisible(True)
        self._close_btn.setEnabled(True)

        if self._had_changes or any(r.parse_failures for r in reports):
            self.sync_finished.emit()

    def _on_error(self, message: str):
        self._progress_label.setText(f"❌ 同步失败：{message}")
        self._close_btn.setEnabled(True)

    def _append_detail(self, action: str, item: SyncChangeItem):
        text, color = _ACTION_STYLES.get(action, (action, QColor(0, 0, 0)))
        tree_item = QTreeWidgetItem([text, item.name, item.file_name, item.detail])
        tree_item.setForeground(0, color)
        self._detail_tree.addTopLevelItem(tree_item)

    # ── 关闭 ─────────────────────────────────────────────────

    def _on_close(self):
        if self._thread and self._thread.isRunning():
            return  # 运行中禁止关闭（按钮此时已禁用，防御性兜底）
        self.accept()

    def closeEvent(self, event):
        if self._thread and self._thread.isRunning():
            event.ignore()
            return
        event.accept()
