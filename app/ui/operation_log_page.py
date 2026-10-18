"""操作日志页面 — 顶层第三个页签（F10-3，候选清单 A2）

时间倒序列表 + 分页 + 按操作类型过滤（按 GUID 检索二期）；
查询走主窗口持有的 ModService（同步、LIMIT 限定，无需后台线程）。
"""

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app.services.mod_service import ModService

# 语义色（对齐 duplicate_scan_dialog / version_scan_dialog 口径）
_GREEN = QColor("#a6e3a1")   # 成功
_RED = QColor("#f38ba8")     # 失败

# 操作类型 → 展示标签（与 OperationLog.operation 的枚举口径一致；
# 未收录的未来类型原样展示，不丢行）
OPERATION_LABELS: dict[str, str] = {
    "install": "📦 安装到游戏",
    "uninstall": "🗑 从游戏卸载",
    "repo_add": "➕ 添加到仓库",
    "repo_remove": "🗑 从仓库移除",
    "enable": "✅ 启用",
    "disable": "🚫 禁用",
    "upgrade": "⬆ 升级",
    "sync": "🔄 磁盘同步",
    "dedup": "🧹 重复项清理",
}

PAGE_SIZE = 100


def operation_label(operation: str) -> str:
    """操作类型 → 展示标签（未收录类型原样返回）"""
    return OPERATION_LABELS.get(operation, operation)


class OperationLogPage(QWidget):
    """操作日志页面（切换到页签 / F5 时 refresh 拉最新数据）"""

    def __init__(self, mod_service: ModService, parent=None):
        super().__init__(parent)
        self._svc = mod_service
        self._page = 1
        self._total = 0

        self._setup_ui()
        self.refresh()

    # ── UI 构建 ─────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # 过滤行：类型下拉 + 刷新 + 总数
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("操作类型:"))

        self._type_combo = QComboBox()
        self._type_combo.addItem("全部类型", "")
        for op, label in OPERATION_LABELS.items():
            self._type_combo.addItem(label, op)
        self._type_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._type_combo)

        self._refresh_btn = QPushButton("🔄 刷新")
        self._refresh_btn.clicked.connect(self.refresh)
        filter_row.addWidget(self._refresh_btn)

        filter_row.addStretch(1)
        self._total_label = QLabel()
        filter_row.addWidget(self._total_label)
        layout.addLayout(filter_row)

        # 日志列表
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["时间", "类型", "Mod 名称", "位置", "结果", "详情"])
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(False)
        layout.addWidget(self._tree, stretch=1)

        # 分页导航
        pagination_row = QHBoxLayout()
        self._prev_btn = QPushButton("◀ 上一页")
        self._prev_btn.setFixedWidth(90)
        self._prev_btn.clicked.connect(self._on_prev_page)
        pagination_row.addWidget(self._prev_btn)

        self._page_label = QLabel("第 1 / 1 页")
        self._page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_label.setMinimumWidth(100)
        pagination_row.addWidget(self._page_label)

        self._next_btn = QPushButton("下一页 ▶")
        self._next_btn.setFixedWidth(90)
        self._next_btn.clicked.connect(self._on_next_page)
        pagination_row.addWidget(self._next_btn)

        pagination_row.addStretch(1)
        pagination_row.addWidget(QLabel(f"每页: {PAGE_SIZE}"))
        layout.addLayout(pagination_row)

    # ── 数据加载 ────────────────────────────────────────────

    def refresh(self):
        """按当前过滤条件与页码重查日志"""
        operation = self._type_combo.currentData() if self._type_combo.count() else ""
        self._total = self._svc.get_operation_logs_count(operation)
        logs = self._svc.get_operation_logs(
            operation, limit=PAGE_SIZE, offset=(self._page - 1) * PAGE_SIZE
        )

        self._tree.clear()
        for log in logs:
            time_text = log.created_at.strftime("%Y-%m-%d %H:%M:%S")
            loc_text = "🎮 游戏" if log.location == "game" else "🗄 仓库"
            if log.result == "success":
                result_text, result_color = "✅ 成功", _GREEN
            else:
                result_text, result_color = "❌ 失败", _RED
            item = QTreeWidgetItem([
                time_text,
                operation_label(log.operation),
                log.mod_name,
                loc_text,
                result_text,
                log.detail,
            ])
            item.setForeground(4, result_color)
            # GUID 不占列，挂 tooltip（按 GUID 检索留二期，F10-3）
            item.setToolTip(
                2, f"GUID: {log.mod_guid or '(无)'}" if log.mod_guid else "GUID: (无)"
            )
            self._tree.addTopLevelItem(item)

        self._update_pagination()

    def _update_pagination(self):
        pages = max(1, -(-self._total // PAGE_SIZE))  # ceil
        if self._page > pages:
            self._page = pages
        self._page_label.setText(f"第 {self._page} / {pages} 页")
        self._total_label.setText(f"共 {self._total} 条")
        self._prev_btn.setEnabled(self._page > 1)
        self._next_btn.setEnabled(self._page < pages)

    # ── 交互 ────────────────────────────────────────────────

    def _on_filter_changed(self):
        self._page = 1  # 切类型回到第一页
        self.refresh()

    def _on_prev_page(self):
        if self._page > 1:
            self._page -= 1
            self.refresh()

    def _on_next_page(self):
        pages = max(1, -(-self._total // PAGE_SIZE))
        if self._page < pages:
            self._page += 1
            self.refresh()
