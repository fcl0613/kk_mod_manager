"""主窗口

需求文档 v1.3 - 7.1 主窗口布局：
  - 顶部工具栏（安装/添加/设置按钮，搜索框）
  - 顶层 TabBar（Mod管理 / 解析卡片）
  - Mod管理页：左侧 Mod 列表 + 右侧详情面板
  - 解析卡片页：图片预览 + Mod 依赖列表（GUID|Mod名称|状态|操作）
  - 底部状态栏
"""

from enum import Enum
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QToolBar, QPushButton, QLineEdit, QLabel,
    QTreeWidget, QTreeWidgetItem, QSplitter,
    QTabWidget, QTabBar, QStackedWidget,
    QStatusBar, QFileDialog, QMessageBox, QProgressDialog,
    QToolButton, QMenu,
)
from PyQt6.QtCore import Qt, QSize, QTimer, QThread
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QShortcut, QKeySequence
from loguru import logger

from app.config import get_config, save_config, reload_config
from app.database import init_db, get_session, get_db_path
from app.models.mod import Mod, ModLocation, ModStatus, OperationLog
from app.services.mod_service import ModService, InstallResult
from app.services.card_service import CardService, CardParseResult
from app.services.sideloader_log_service import (
    LogEntryKind,
    LogMissingEntry,
    SideloaderLogService,
)
from app.ui.settings_dialog import SettingsDialog
from app.ui.install_dialog import InstallConfirmDialog, CategoryChoiceDialog
from app.ui.mod_detail_panel import ModDetailPanel
from app.ui.card_result_dialog import CardResultDialog
from app.ui.card_parse_page import CardParsePage
from app.ui.batch_install_dialog import BatchInstallDialog
from app.ui.sync_dialog import SyncDialog
from app.ui.version_scan_dialog import VersionScanDialog
from app.ui.duplicate_scan_dialog import DuplicateScanDialog
from app.ui.log_dialog import LogDialog
from app.ui.card_parse_worker import CardParseWorker
from app.utils.version_utils import display_version


class PendingAction(Enum):
    """重试操作类型"""
    INSTALL = "install"
    INSTALL_AND_COLLECT = "install_and_collect"


class MainWindow(QMainWindow):
    """主窗口"""

    def __init__(self):
        super().__init__()
        self._config = get_config()
        self._card_result: CardParseResult | None = None  # 最近一次卡片解析结果
        self._pending_path: Path | None = None  # 等待重试的原始文件路径
        self._pending_location: ModLocation | None = None  # 等待重试的目标位置
        self._pending_action: PendingAction | None = None  # 等待重试的操作类型
        self._pending_category: str | None = None  # 等待重试的 F19 目标分类（None = 平铺）

        # 分页状态
        self._page_size = 200  # 每页默认 200 个 Mod
        self._game_page = 1
        self._repo_page = 1
        self._game_total = 0
        self._repo_total = 0

        # 初始化数据库
        init_db()
        self._mod_service = ModService(get_session())
        self._card_service = CardService()
        self._sideloader_log_service = SideloaderLogService()

        # F10-4：启动时自动清理过期操作日志（保留近一个月，暂定值）
        self._mod_service.cleanup_old_logs()

        # 游戏日志解析状态（F23）：最近一次日志来源与其结果视图
        self._log_path: Path | None = None
        self._log_view: tuple[CardParseResult, dict[str, LogMissingEntry]] | None = None

        # 卡片解析后台线程（F17-11；同一时刻仅一次解析）
        self._card_parse_thread: QThread | None = None
        self._card_parse_worker: CardParseWorker | None = None

        self._setup_ui()
        self._load_theme()
        self._check_first_run()

        # 首次启动加载已有数据
        self._refresh_game_list()
        self._update_status_bar()

    # ═══════════════════════════════════════════════════════
    # UI 构建
    # ═══════════════════════════════════════════════════════

    def _setup_ui(self):
        self.setWindowTitle("KK Mod Manager")
        self.resize(
            self._config.window_width,
            self._config.window_height,
        )

        # 允许拖放
        self.setAcceptDrops(True)

        # ── 工具栏 ──
        self._create_toolbar()

        # ── 中央部件 ──
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 顶层 Tab 导航栏 ──
        self._create_top_tab_bar(main_layout)

        # ── 页面栈 ──
        self.page_stack = QStackedWidget()

        # Page 0: Mod 管理页面
        self.mod_page = self._create_mod_page()
        self.page_stack.addWidget(self.mod_page)

        # Page 1: 解析卡片页面
        self.card_page = CardParsePage()
        self.card_page.install_mod_requested.connect(self._on_card_page_install)
        self.card_page.reparse_requested.connect(self._on_card_page_reparse)
        self.card_page.card_dropped.connect(self._parse_card)
        self.card_page.convert_to_kk_requested.connect(self._on_convert_to_kk)
        self.card_page.game_log_requested.connect(self._on_game_log_parse)
        self.page_stack.addWidget(self.card_page)

        main_layout.addWidget(self.page_stack, 1)

        # ── 操作日志对话框（F10-3）──
        self._create_log_dialog()

        # ── 状态栏 ──
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._update_status_bar()

    def _create_top_tab_bar(self, parent_layout: QVBoxLayout):
        """创建顶层 Tab 导航栏"""
        self.top_tab_bar = QTabBar()
        self.top_tab_bar.addTab("📦 Mod 管理")
        self.top_tab_bar.addTab("📋 解析卡片")
        self.top_tab_bar.currentChanged.connect(self._on_top_tab_changed)

        # 设置 TabBar 样式
        self.top_tab_bar.setStyleSheet(
            "QTabBar::tab {"
            "  padding: 8px 20px;"
            "  font-size: 14px;"
            "}"
        )
        parent_layout.addWidget(self.top_tab_bar)

    def _create_mod_page(self) -> QWidget:
        """创建 Mod 管理页面（原有内容）"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ── 主内容区（左右分栏）──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左列：Tab 切换游戏/仓库
        left_panel = self._create_left_panel()
        splitter.addWidget(left_panel)

        # 右列：详情面板
        self.detail_panel = ModDetailPanel()
        self.detail_panel.uninstall_requested.connect(self._do_uninstall)
        self.detail_panel.enable_requested.connect(self._do_set_enabled)
        self.detail_panel.install_to_game_requested.connect(self._do_install_from_repo)
        self.detail_panel.add_to_repo_requested.connect(self._do_add_to_repo)
        splitter.addWidget(self.detail_panel)

        splitter.setSizes([500, 350])
        layout.addWidget(splitter, 1)

        return page

    def _create_toolbar(self):
        """创建顶部工具栏（分组下拉布局）

        [➕ 安装 ▾] [🗂 批量 ▾] [🔄 数据 ▾] │ 搜索 [______] │ [⚙]
        """
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)

        def make_menu_btn(text: str, tooltip: str) -> QToolButton:
            """统一构造 InstantPopup 下拉按钮（文字带 ▾，箭头由 QSS 隐藏）"""
            btn = QToolButton()
            btn.setText(text)
            btn.setToolTip(tooltip)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            btn.setMenu(QMenu(btn))
            return btn

        # ➕ 安装
        install_btn = make_menu_btn("➕ 安装 ▾", "安装 / 收藏 zipmod")
        install_btn.menu().addAction(
            "📦 安装到游戏", lambda: self._install_mod(ModLocation.GAME))
        install_btn.menu().addAction(
            "📁 添加到仓库", lambda: self._install_mod(ModLocation.REPO))
        install_btn.menu().addAction("📦+📁 安装并收藏", self._install_and_collect)
        toolbar.addWidget(install_btn)

        # 🗂 批量
        batch_btn = make_menu_btn("🗂 批量 ▾", "批量添加多个 .zipmod 文件")
        batch_btn.menu().addAction(
            "批量安装到游戏", lambda: self._batch_install(ModLocation.GAME))
        batch_btn.menu().addAction(
            "批量添加到仓库", lambda: self._batch_install(ModLocation.REPO))
        toolbar.addWidget(batch_btn)

        # 🔄 数据
        data_btn = make_menu_btn("🔄 数据 ▾", "数据库与磁盘的同步 / 重建")
        data_btn.menu().addAction("⟳ 同步磁盘（保留备注）", self._sync_mods)
        data_btn.menu().addAction("🔍 仓库版本巡检", self._version_scan)
        data_btn.menu().addAction("🧹 重复项检查", self._duplicate_scan)
        data_btn.menu().addSeparator()
        data_btn.menu().addAction("⚠ 初始化MOD（清空重建）", self._init_mod)
        toolbar.addWidget(data_btn)

        toolbar.addSeparator()

        # 搜索框（占位符已说明用途，不再加标签）
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索：名称/作者/GUID...")
        self.search_edit.setFixedWidth(260)
        self.search_edit.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search_edit)

        toolbar.addSeparator()

        # 操作日志（开关式，控制日志对话框，F10-3）
        self.log_btn = QToolButton()
        self.log_btn.setText("📜")
        self.log_btn.setToolTip("操作日志（Ctrl+L）")
        self.log_btn.setCheckable(True)
        self.log_btn.clicked.connect(self._toggle_log_dialog)
        toolbar.addWidget(self.log_btn)

        # 设置（图标式）
        settings_btn = QToolButton()
        settings_btn.setText("⚙")
        settings_btn.setToolTip("设置")
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)

        # 刷新（独立按钮，高频操作一步直达）
        refresh_btn = QToolButton()
        refresh_btn.setText("🔄 刷新")
        refresh_btn.setToolTip("刷新当前列表（F5）")
        refresh_btn.clicked.connect(self._refresh_current)
        toolbar.addWidget(refresh_btn)

        # ── 快捷键 ──────────────────────────────────────────
        refresh_sc = QShortcut(QKeySequence("F5"), self)
        refresh_sc.activated.connect(self._refresh_current)

        search_sc = QShortcut(QKeySequence("Ctrl+F"), self)
        search_sc.activated.connect(self.search_edit.setFocus)
        search_sc.activated.connect(self.search_edit.selectAll)

        log_sc = QShortcut(QKeySequence("Ctrl+L"), self)
        log_sc.activated.connect(
            lambda: self._toggle_log_dialog(not self.log_dialog.isVisible()))

    def _create_left_panel(self) -> QWidget:
        """创建左侧面板（Tab 切换游戏/仓库的 Mod 列表）"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tab_widget = QTabWidget()

        # 游戏 Mod 列表
        self.game_tree = self._create_mod_tree()
        self.tab_widget.addTab(self.game_tree, "🎮 游戏已装 Mod")

        # 仓库 Mod 列表
        self.repo_tree = self._create_mod_tree()
        self.tab_widget.addTab(self.repo_tree, "📁 仓库 Mod")

        self.tab_widget.currentChanged.connect(self._on_mod_tab_changed)
        layout.addWidget(self.tab_widget)

        # 分页导航栏
        self._create_pagination_bar(layout)

        return panel

    def _create_pagination_bar(self, parent_layout: QVBoxLayout):
        """创建分页导航栏"""
        pagination_widget = QWidget()
        pagination_layout = QHBoxLayout(pagination_widget)
        pagination_layout.setContentsMargins(4, 2, 4, 2)
        pagination_layout.setSpacing(8)

        # 上一页按钮
        self.prev_page_btn = QPushButton("◀ 上一页")
        self.prev_page_btn.setFixedWidth(80)
        self.prev_page_btn.clicked.connect(self._on_prev_page)
        pagination_layout.addWidget(self.prev_page_btn)

        # 页码信息
        self.page_label = QLabel("第 1 / 1 页")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setMinimumWidth(100)
        pagination_layout.addWidget(self.page_label)

        # 下一页按钮
        self.next_page_btn = QPushButton("下一页 ▶")
        self.next_page_btn.setFixedWidth(80)
        self.next_page_btn.clicked.connect(self._on_next_page)
        pagination_layout.addWidget(self.next_page_btn)

        # 分页大小选择
        pagination_layout.addWidget(QLabel("每页:"))
        self.page_size_combo = QLabel(str(self._page_size))
        pagination_layout.addWidget(self.page_size_combo)

        parent_layout.addWidget(pagination_widget)

    def _create_mod_tree(self) -> QTreeWidget:
        """创建 Mod 列表树"""
        tree = QTreeWidget()
        tree.setHeaderLabels(["Mod 名称", "版本", "状态"])
        tree.setColumnWidth(0, 300)
        tree.setColumnWidth(1, 80)
        tree.setColumnWidth(2, 60)
        tree.setRootIsDecorated(True)
        tree.setIndentation(16)
        tree.setAlternatingRowColors(False)
        tree.itemClicked.connect(self._on_mod_selected)
        return tree

    # ═══════════════════════════════════════════════════════
    # 顶层 Tab 导航
    # ═══════════════════════════════════════════════════════

    def _on_top_tab_changed(self, index: int):
        """顶层 TabBar 切换（SPA 页面切换）"""
        self.page_stack.setCurrentIndex(index)
        if index == 0:
            # 切换到 Mod 管理页
            self._refresh_current()
        elif index == 1:
            # 切换到解析卡片页：按页面当前模式回填最近一次结果
            if self.card_page.is_log_mode:
                if self._log_view is not None:
                    result, entries = self._log_view
                    self.card_page.set_log_result(result, entries)
            elif self._card_result is not None:
                self.card_page.set_card_result(self._card_result)

    # ═══════════════════════════════════════════════════════
    # 操作日志对话框（F10-3）
    # ═══════════════════════════════════════════════════════

    def _create_log_dialog(self):
        """创建操作日志非模态对话框（默认隐藏，工具栏 📜 / Ctrl+L 开关；
        弹出位置/大小不持久化，默认居中于主窗口）"""
        self.log_dialog = LogDialog(self._mod_service, self)
        self.log_dialog.visibility_changed.connect(self._on_log_dialog_visibility)

    def _on_log_dialog_visibility(self, visible: bool):
        """对话框可见性变化（✕/Esc/主窗口最小化还原均覆盖）：同步按钮勾选态"""
        self.log_btn.setChecked(visible)

    def _toggle_log_dialog(self, checked: bool):
        """工具栏 📜 按钮 / Ctrl+L：开关日志对话框"""
        if checked:
            self.log_dialog.show()
            self.log_dialog.raise_()  # 非模态从属窗口，确保不被主窗口盖住
        else:
            self.log_dialog.hide()

    def switch_to_card_page(self):
        """切换到卡片解析页面"""
        self.top_tab_bar.setCurrentIndex(1)
        self.page_stack.setCurrentIndex(1)

    # ═══════════════════════════════════════════════════════
    # 拖放处理（卡片 PNG / zipmod 安装）
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _is_zipmod(path: Path) -> bool:
        return path.suffix.lower() in (".zipmod", ".zipmod_disabled")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            for url in urls:
                path = Path(url.toLocalFile())
                if path.suffix.lower() == ".png" or self._is_zipmod(path):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        if not event.mimeData().hasUrls():
            return
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        # 卡片 PNG → 解析依赖
        for path in paths:
            if path.suffix.lower() == ".png":
                self._parse_card(path)
                return
        # zipmod → 无卡片上下文安装（F19-4：询问目标分类）
        zipmods = [p for p in paths if self._is_zipmod(p)]
        if zipmods:
            if not get_config().game_mod_path:
                QMessageBox.warning(self, "提示", "请先在设置中配置游戏 Mod 路径")
                return
            category = self._ask_install_category()
            if category is None:
                return  # 用户取消
            for path in zipmods:
                self._do_install(str(path), ModLocation.GAME, category)

    # ═══════════════════════════════════════════════════════
    # 卡片解析
    # ═══════════════════════════════════════════════════════

    def _parse_card(self, card_path: Path):
        """解析角色卡/服装卡/场景卡，后台线程执行并在卡片解析页面显示结果（F17-11）"""
        if self._card_parse_thread is not None:
            # 上一次解析仍在进行，忽略本次拖入（防重入）
            self.status_bar.showMessage("正在解析上一张卡片，请稍候…", 3000)
            return

        logger.info("开始解析卡片: {}", card_path)
        self.status_bar.showMessage(f"正在解析卡片：{card_path.name}...")

        self._card_parse_thread = QThread(self)
        self._card_parse_worker = CardParseWorker(self._card_service, card_path)
        self._card_parse_worker.moveToThread(self._card_parse_thread)
        self._card_parse_thread.started.connect(self._card_parse_worker.run)
        self._card_parse_worker.progress.connect(self._on_card_parse_progress)
        self._card_parse_worker.finished.connect(self._on_card_parse_finished)
        self._card_parse_worker.error.connect(self._on_card_parse_error)
        self._card_parse_thread.start()

    def _on_card_parse_progress(self, done: int, total: int):
        """解析进度：状态栏显示已处理内嵌角色数（仅场景卡触发）"""
        self.status_bar.showMessage(f"解析场景中：已处理角色 {done}/{total}...")

    def _on_card_parse_finished(self, result: CardParseResult):
        """解析完成：展示结果或友好错误（F17-12）"""
        self._dispose_card_parse_thread()

        if not result.is_valid:
            logger.warning("卡片解析失败: {} - {}", result.card_path.name, result.error_message)
            QMessageBox.warning(
                self,
                "卡片解析失败",
                result.error_message,
            )
            self.status_bar.clearMessage()
            # 重置卡片页拖放区（避免一直显示"解析中..."）
            self.card_page.drop_zone.setText(
                "🎴 拖入角色卡/服装卡/场景卡 (PNG) 进行 Mod 依赖分析"
            )
            self.card_page.stats_label.setText("拖入 PNG 卡片开始解析")
            return

        self._card_result = result

        # 切换到解析卡片页面并显示结果
        self.card_page.set_card_result(result)
        self.switch_to_card_page()

        logger.info(
            "卡片解析完成: {} - 总计={} 已安装={} 仓库有={} 缺失={}",
            result.card_path.name,
            result.total_count, result.installed_count,
            result.in_repo_count, result.missing_count,
        )
        self.status_bar.showMessage(
            f"卡片解析完成：{result.total_count} 个依赖 Mod | "
            f"已安装 {result.installed_count} | "
            f"仓库有 {result.in_repo_count} | "
            f"缺失 {result.missing_count}"
        )

    def _on_card_parse_error(self, message: str):
        """后台线程未预期异常"""
        self._dispose_card_parse_thread()
        logger.error("卡片解析线程异常: {}", message)
        QMessageBox.critical(self, "解析出错", f"解析卡片时发生未预期的错误：\n{message}")
        self.status_bar.clearMessage()
        self.card_page.drop_zone.setText(
            "🎴 拖入角色卡/服装卡/场景卡 (PNG) 进行 Mod 依赖分析"
        )
        self.card_page.stats_label.setText("拖入 PNG 卡片开始解析")

    def _dispose_card_parse_thread(self):
        """回收解析线程与 worker（finished/error 到达时 run() 已近尾声）"""
        thread, self._card_parse_thread = self._card_parse_thread, None
        self._card_parse_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)

    def _parse_card_and_show_dialog(self, card_path: Path):
        """解析卡片并在对话框中显示（保留旧接口兼容）"""
        self.status_bar.showMessage(f"正在解析卡片：{card_path.name}...")

        result = self._card_service.parse_card(card_path)

        if not result.is_valid:
            QMessageBox.warning(
                self,
                "卡片解析失败",
                result.error_message,
            )
            self.status_bar.clearMessage()
            return

        self._card_service.check_mod_availability(
            result,
            self._mod_service.session,
        )

        self._card_result = result

        dialog = CardResultDialog(result, self)
        dialog.show()

        self.status_bar.showMessage(
            f"卡片解析完成：{result.total_count} 个依赖 Mod | "
            f"已安装 {result.installed_count} | "
            f"仓库有 {result.in_repo_count} | "
            f"缺失 {result.missing_count}"
        )

    def reparse_card(self) -> CardParseResult | None:
        """重新解析上次的卡片（用于安装后刷新结果）"""
        if self._card_result is None:
            return None
        result = self._card_service.parse_card(self._card_result.card_path)
        if result.is_valid:
            self._card_service.check_mod_availability(
                result,
                self._mod_service.session,
            )
            self._card_result = result
        return self._card_result

    # ═══════════════════════════════════════════════════════
    # 游戏日志解析（F23）
    # ═══════════════════════════════════════════════════════

    def _on_game_log_parse(self, log_path: Path):
        """卡片页「🎮 从游戏日志读取缺失」：解析 + 对照本地库并展示（F23-1）"""
        logger.info("开始解析游戏日志: {}", log_path)
        self.status_bar.showMessage(f"正在解析游戏日志：{log_path.name}...")

        try:
            result, entries = self._sideloader_log_service.analyze(
                log_path,
                self._mod_service.session,
                self._card_service,
            )
        except Exception as e:  # noqa: BLE001 - 兜底回传 UI
            logger.exception("游戏日志解析异常")
            QMessageBox.critical(self, "日志解析出错", f"解析游戏日志时发生错误：\n{e}")
            self.status_bar.clearMessage()
            self.card_page.drop_zone.setText(
                "🎴 拖入角色卡/服装卡/场景卡 (PNG) 进行 Mod 依赖分析"
            )
            return

        if not result.mods:
            self.status_bar.clearMessage()
            self.status_bar.showMessage("游戏日志中未发现缺失 / 过旧的 Mod 警告", 5000)
            QMessageBox.information(
                self,
                "没有发现缺失",
                "日志中未发现 Sideloader 的缺失 / 过旧警告。\n\n"
                "提示：警告只在游戏内实际加载含缺失依赖的卡 / 场景时产生，\n"
                "仅进入主菜单不会触发。",
            )
            self.card_page.drop_zone.setText(
                "🎴 拖入角色卡/服装卡/场景卡 (PNG) 进行 Mod 依赖分析"
            )
            return

        self._log_path = log_path
        self._log_view = (result, entries)
        self.card_page.set_log_result(result, entries)
        self.switch_to_card_page()

        outdated = sum(1 for e in entries.values() if e.kind is LogEntryKind.OUTDATED)
        logger.info(
            "游戏日志解析完成: {} - 总 GUID={} 已装={} 过旧={} 仓库有={} 缺失={}",
            log_path.name,
            result.total_count, result.installed_count, outdated,
            result.in_repo_count, result.missing_count,
        )
        self.status_bar.showMessage(
            f"日志解析完成：{result.total_count} 个 GUID | "
            f"已装 {result.installed_count}"
            + (f"（⚠过旧 {outdated}）" if outdated else "")
            + f" | 仓库有 {result.in_repo_count} | "
            f"缺失 {result.missing_count}"
        )

    def refresh_log_result(self) -> CardParseResult | None:
        """重读日志并重查本地库（安装 / 升级后刷新，F23）。

        日志文件本身只反映上次游戏会话，重新对照后新装的 Mod 会
        从 🟡/🔴 转为 🟢（并带「已装*」说明）。
        """
        if self._log_path is None:
            return None
        try:
            result, entries = self._sideloader_log_service.analyze(
                self._log_path,
                self._mod_service.session,
                self._card_service,
            )
        except Exception:  # noqa: BLE001 - 刷新失败不阻断安装流程
            logger.exception("刷新日志结果失败")
            return None
        self._log_view = (result, entries)
        self.card_page.set_log_result(result, entries)
        return result

    def _refresh_card_or_log(self) -> CardParseResult | None:
        """按卡片页当前模式刷新结果：日志模式重查库，卡片模式重解析卡片"""
        if self.card_page.is_log_mode:
            return self.refresh_log_result()
        new_result = self.reparse_card()
        if new_result is not None:
            self.card_page.set_card_result(new_result)
        return new_result

    def install_repo_mod(self, mod_id: int, category: str | None = None) -> tuple[bool, str]:
        """从仓库安装 Mod 到游戏（供卡片结果面板调用；category 为 F19 目标分类）"""
        result = self._mod_service.install_from_repo(mod_id, category=category)
        if result.success:
            self._refresh_current()
        return result.success, result.message

    # ═══════════════════════════════════════════════════════
    # 卡片解析页面信号回调
    # ═══════════════════════════════════════════════════════

    def _on_card_page_install(self, mod_id: int, category: str = ""):
        """卡片解析页：安装单个 Mod（category 为按依赖粗分路由的目标分类，F17-13）"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        name = mod.name if mod else f"ID={mod_id}"
        logger.info("卡片页安装请求: {} (ID={}, 分类={})", name, mod_id, category or "平铺")
        self.status_bar.showMessage(f"正在从仓库安装：{name}...")

        success, msg = self.install_repo_mod(mod_id, category=category or None)
        QMessageBox.information(self, "安装结果", msg)

        if success:
            # 按当前模式刷新：卡片重解析 / 日志重查库（F23）
            self._refresh_card_or_log()

    def _on_card_page_reparse(self):
        """卡片解析页：请求重新解析（全部安装后；日志模式重查库，F23）"""
        self._refresh_card_or_log()

    def _on_convert_to_kk(self, input_path: Path, output_path: Path):
        """卡片解析页：将二代卡（KKS）转换为一代卡（KK）"""
        logger.info("开始转换卡片: {} → {}", input_path, output_path)
        self.status_bar.showMessage("正在转换卡片为一代卡（KK）...")

        success, error_msg = self._card_service.kks_to_kk(output_path)

        if success:
            self.status_bar.showMessage(f"转换成功：{output_path.name}")
            QMessageBox.information(
                self,
                "转换成功",
                f"已成功转换为一代卡（KK）：\n{output_path}",
            )
        else:
            self.status_bar.showMessage("转换失败")
            QMessageBox.warning(
                self,
                "转换失败",
                error_msg,
            )

    # ═══════════════════════════════════════════════════════
    # Mod 安装/添加
    # ═══════════════════════════════════════════════════════

    def _ask_install_category(self) -> str | None:
        """询问 GAME 端安装目标分类（F19-4，无卡片上下文入口）。

        Returns:
            "" = 分类开关关闭（走旧公式）；None = 用户取消；其他 = 目标分类名
        """
        if not get_config().category.game_category_enabled:
            return ""
        dialog = CategoryChoiceDialog(self)
        if dialog.exec() != CategoryChoiceDialog.DialogCode.Accepted:
            return None
        return dialog.selected

    def _install_mod(self, location: ModLocation):
        """安装 Mod 到游戏 或 添加到仓库"""
        # 检查路径
        config = get_config()
        if location == ModLocation.GAME and not config.game_mod_path:
            QMessageBox.warning(self, "提示", "请先在设置中配置游戏 Mod 路径")
            return
        if location == ModLocation.REPO and not config.repo_path:
            QMessageBox.warning(self, "提示", "请先在设置中配置本地仓库路径")
            return

        # 选择文件
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 zipmod 文件",
            "",
            "zipmod 文件 (*.zipmod);;所有文件 (*.*)",
        )
        if not file_path:
            return

        # GAME 端询问目标分类（F19-4）
        category: str | None = None
        if location == ModLocation.GAME:
            category = self._ask_install_category()
            if category is None:
                return  # 用户取消

        self._do_install(file_path, location, category)

    def _install_and_collect(self):
        """安装并收藏"""
        config = get_config()
        if not config.game_mod_path:
            QMessageBox.warning(self, "提示", "请先在设置中配置游戏 Mod 路径")
            return
        if not config.repo_path:
            QMessageBox.warning(self, "提示", "请先在设置中配置本地仓库路径")
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 zipmod 文件",
            "",
            "zipmod 文件 (*.zipmod);;所有文件 (*.*)",
        )
        if not file_path:
            return

        category = self._ask_install_category()
        if category is None:
            return  # 用户取消

        self.status_bar.showMessage("正在安装并收藏...")
        self._pending_path = Path(file_path)
        self._pending_action = PendingAction.INSTALL_AND_COLLECT
        self._pending_category = category
        result = self._mod_service.install_and_collect(Path(file_path), category=category)
        self._handle_install_result(result)

    def _batch_install(self, location: ModLocation):
        """批量安装 Mod"""
        config = get_config()
        if location == ModLocation.GAME and not config.game_mod_path:
            QMessageBox.warning(self, "提示", "请先在设置中配置游戏 Mod 路径")
            return
        if location == ModLocation.REPO and not config.repo_path:
            QMessageBox.warning(self, "提示", "请先在设置中配置本地仓库路径")
            return

        # GAME 端询问一次目标分类，整个批次共用（F19-4）
        category: str | None = None
        if location == ModLocation.GAME:
            category = self._ask_install_category()
            if category is None:
                return  # 用户取消

        dialog = BatchInstallDialog(self._mod_service, location, parent=self, category=category)
        dialog.install_finished.connect(self._refresh_current)
        dialog.exec()

    def _do_install(self, file_path: str, location: ModLocation, category: str | None = None):
        """执行安装（category 为 F19 目标分类，仅 GAME 端使用）"""
        loc_text = "安装到游戏" if location == ModLocation.GAME else "添加到仓库"
        logger.info("{} - 开始: {}", loc_text, file_path)
        self.status_bar.showMessage(f"正在{loc_text}...")

        # 保存原始路径，用于重试
        self._pending_path = Path(file_path)
        self._pending_location = location
        self._pending_action = PendingAction.INSTALL
        self._pending_category = category

        result: InstallResult

        if location == ModLocation.GAME:
            result = self._mod_service.install_to_game(Path(file_path), category=category)
        else:
            result = self._mod_service.add_to_repo(Path(file_path))

        self._handle_install_result(result)

    def _handle_install_result(self, result: InstallResult):
        """处理安装结果"""
        if result.success:
            logger.info("操作成功: {}", result.message)
            QMessageBox.information(self, "操作成功", result.message)
            self._refresh_current()
            self.status_bar.showMessage(result.message)
        elif result.message in (
            "NEWER_VERSION_NEEDS_CONFIRM",
            "OLDER_VERSION_NEEDS_CONFIRM",
        ):
            # 需要用户确认升级/降级
            if result.duplicate_report is None or result.manifest is None:
                QMessageBox.warning(self, "错误", "内部错误：缺少重复检测报告")
                return

            action = "安装" if (
                result.duplicate_report.existing_mod is not None
                and result.duplicate_report.existing_mod.location == ModLocation.GAME
            ) else "添加"

            dialog = InstallConfirmDialog(
                result.manifest,
                result.duplicate_report,
                action_name=action,
                parent=self,
                target_category=self._pending_category or "",
            )
            if dialog.exec() == InstallConfirmDialog.DialogCode.Accepted:
                # 用户确认，重新执行（带确认标志）
                self.status_bar.showMessage(f"正在{action}...")

                if self._pending_path is None:
                    QMessageBox.warning(self, "错误", "内部错误：丢失文件路径")
                    return

                if self._pending_action == PendingAction.INSTALL_AND_COLLECT:
                    retry = self._mod_service.install_and_collect(
                        self._pending_path,
                        confirm_upgrade=dialog.confirm_upgrade,
                        confirm_downgrade=dialog.confirm_downgrade,
                        category=self._pending_category,
                    )
                elif self._pending_location == ModLocation.GAME:
                    retry = self._mod_service.install_to_game(
                        self._pending_path,
                        confirm_upgrade=dialog.confirm_upgrade,
                        confirm_downgrade=dialog.confirm_downgrade,
                        category=self._pending_category,
                    )
                else:
                    retry = self._mod_service.add_to_repo(
                        self._pending_path,
                        confirm_upgrade=dialog.confirm_upgrade,
                        confirm_downgrade=dialog.confirm_downgrade,
                    )

                if retry.success:
                    QMessageBox.information(self, "操作成功", retry.message)
                    self._refresh_current()
                    self.status_bar.showMessage(retry.message)
                else:
                    logger.warning("重试操作失败: {}", retry.message)
                    QMessageBox.warning(self, "操作失败", retry.message)
                    self.status_bar.clearMessage()
        else:
            logger.warning("操作失败: {}", result.message)
            QMessageBox.warning(self, "操作失败", result.message)
            self.status_bar.clearMessage()

    # ═══════════════════════════════════════════════════════
    # 卸载 / 启用禁用
    # ═══════════════════════════════════════════════════════

    def _do_uninstall(self, mod_id: int):
        """执行卸载/移除"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        if mod is None:
            return

        logger.info("卸载 Mod: {} (ID={})", mod.name, mod_id)
        self.status_bar.showMessage(f"正在卸载：{mod.name}...")
        result = self._mod_service.uninstall(mod_id)

        if result.success:
            QMessageBox.information(self, "操作成功", result.message)
            self._refresh_current()
            self.detail_panel.clear()
            self.status_bar.showMessage(result.message)
        else:
            QMessageBox.warning(self, "操作失败", result.message)
            self.status_bar.clearMessage()

    def _do_set_enabled(self, mod_id: int, enabled: bool):
        """执行启用/禁用"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        if mod is None:
            return

        action = "启用" if enabled else "禁用"
        logger.info("{} Mod: {} (ID={})", action, mod.name, mod_id)
        self.status_bar.showMessage(f"正在{action}：{mod.name}...")
        result = self._mod_service.set_enabled(mod_id, enabled)

        if result.success:
            QMessageBox.information(self, "操作成功", result.message)
            self._refresh_current()
            if result.mod:
                is_in_repo = self._mod_service.get_repo_mod_by_guid(result.mod.guid) is not None
                self.detail_panel.show_mod(result.mod, is_in_game=False, is_in_repo=is_in_repo)
            self.status_bar.showMessage(result.message)
        else:
            QMessageBox.warning(self, "操作失败", result.message)
            self.status_bar.clearMessage()

    def _do_install_from_repo(self, mod_id: int):
        """从仓库安装 Mod 到游戏（详情面板按钮触发，无卡片上下文 → 询问分类 F19-4）"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        if mod is None:
            return

        category = self._ask_install_category()
        if category is None:
            return  # 用户取消

        logger.info("从仓库安装到游戏: {} (ID={})", mod.name, mod_id)
        self.status_bar.showMessage(f"正在从仓库安装：{mod.name}...")
        result = self._mod_service.install_from_repo(mod_id, category=category)

        if result.success:
            QMessageBox.information(self, "操作成功", result.message)
            self._refresh_current()
            # 尝试选中新安装到游戏的 Mod
            self.detail_panel.clear()
            self.status_bar.showMessage(result.message)
        else:
            QMessageBox.warning(self, "操作失败", result.message)
            self.status_bar.clearMessage()

    def _do_add_to_repo(self, mod_id: int):
        """从游戏将 Mod 加入本地仓库（详情面板按钮触发）"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        if mod is None:
            return

        logger.info("从游戏添加到仓库: {} (ID={})", mod.name, mod_id)
        self.status_bar.showMessage(f"正在添加到仓库：{mod.name}...")
        result = self._mod_service.add_game_mod_to_repo(mod_id)

        if result.success:
            QMessageBox.information(self, "操作成功", result.message)
            self._refresh_current()
            self.detail_panel.clear()
            self.status_bar.showMessage(result.message)
        else:
            QMessageBox.warning(self, "操作失败", result.message)
            self.status_bar.clearMessage()

    # ═══════════════════════════════════════════════════════
    # 列表刷新
    # ═══════════════════════════════════════════════════════

    def _refresh_current(self):
        """刷新当前页面的列表（Mod 管理页按内层 Tab）"""
        idx = self.tab_widget.currentIndex()
        if idx == 0:
            self._refresh_game_list()
        else:
            self._refresh_repo_list()

    def _on_prev_page(self):
        """上一页"""
        idx = self.tab_widget.currentIndex()
        if idx == 0:
            if self._game_page > 1:
                self._game_page -= 1
                self._refresh_game_list()
        else:
            if self._repo_page > 1:
                self._repo_page -= 1
                self._refresh_repo_list()

    def _on_next_page(self):
        """下一页"""
        idx = self.tab_widget.currentIndex()
        if idx == 0:
            total_pages = max(1, (self._game_total + self._page_size - 1) // self._page_size)
            if self._game_page < total_pages:
                self._game_page += 1
                self._refresh_game_list()
        else:
            total_pages = max(1, (self._repo_total + self._page_size - 1) // self._page_size)
            if self._repo_page < total_pages:
                self._repo_page += 1
                self._refresh_repo_list()

    def _refresh_game_list(self):
        """刷新游戏 Mod 列表"""
        self.game_tree.clear()
        search = self.search_edit.text().strip()

        grouped, total = self._mod_service.group_by_author_paginated(
            ModLocation.GAME, search, page=self._game_page, page_size=self._page_size
        )
        self._game_total = total

        self._populate_tree(self.game_tree, grouped)
        self._update_pagination_label()
        self._update_status_bar()

    def _refresh_repo_list(self):
        """刷新仓库 Mod 列表"""
        self.repo_tree.clear()
        search = self.search_edit.text().strip()

        grouped, total = self._mod_service.group_by_author_paginated(
            ModLocation.REPO, search, page=self._repo_page, page_size=self._page_size
        )
        self._repo_total = total

        self._populate_tree(self.repo_tree, grouped)
        self._update_pagination_label()
        self._update_status_bar()

    def _update_pagination_label(self):
        """更新分页标签"""
        idx = self.tab_widget.currentIndex()
        if idx == 0:
            current = self._game_page
            total = self._game_total
        else:
            current = self._repo_page
            total = self._repo_total

        total_pages = max(1, (total + self._page_size - 1) // self._page_size)
        self.page_label.setText(f"第 {current} / {total_pages} 页 (共 {total} 个)")

        self.prev_page_btn.setEnabled(current > 1)
        self.next_page_btn.setEnabled(current < total_pages)

    def _populate_tree(
        self,
        tree: QTreeWidget,
        grouped: dict[str, list[Mod]],
    ):
        """按作者分组填充树形列表"""
        is_game = tree is self.game_tree

        for author, mods in sorted(grouped.items()):
            # 作者分组
            group = QTreeWidgetItem(tree)
            group.setText(0, f"📂 {author}")
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setFirstColumnSpanned(True)

            for mod in mods:
                item = QTreeWidgetItem(group)
                item.setText(0, mod.name)
                item.setText(1, display_version(mod.version))

                if mod.location == ModLocation.GAME:
                    status_text = "✅" if mod.status == ModStatus.ENABLED else "⛔"
                else:
                    # 仓库中的 Mod 标注是否已安装
                    if self._mod_service.get_game_mod_by_guid(mod.guid):
                        status_text = "📦 已装"
                    else:
                        status_text = "📁"
                item.setText(2, status_text)

                # 存储 mod_id 用于选择
                item.setData(0, Qt.ItemDataRole.UserRole, mod.id)

            group.setExpanded(True)

    # ═══════════════════════════════════════════════════════
    # 事件处理
    # ═══════════════════════════════════════════════════════

    def _on_mod_tab_changed(self, index: int):
        """Mod 管理页内部的 Tab 切换（游戏/仓库）"""
        # 重置到第一页
        if index == 0:
            self._game_page = 1
        else:
            self._repo_page = 1
        self._refresh_current()
        self.detail_panel.clear()

    def _on_search(self, text: str):
        # 搜索时重置到第一页
        self._game_page = 1
        self._repo_page = 1
        self._refresh_current()

    def _on_mod_selected(self, item: QTreeWidgetItem, column: int):
        mod_id = item.data(0, Qt.ItemDataRole.UserRole)
        if mod_id is None:
            return
        mod = self._mod_service.get_mod_by_id(mod_id)
        if mod:
            is_game = mod.location == ModLocation.GAME
            # 检查跨位置存在性
            is_in_game = not is_game and self._mod_service.get_game_mod_by_guid(mod.guid) is not None
            is_in_repo = is_game and self._mod_service.get_repo_mod_by_guid(mod.guid) is not None
            self.detail_panel.show_mod(mod, is_in_game=is_in_game, is_in_repo=is_in_repo)

    def _update_status_bar(self):
        """更新状态栏统计"""
        try:
            game_stats = self._mod_service.get_statistics(ModLocation.GAME)
            repo_stats = self._mod_service.get_statistics(ModLocation.REPO)

            self.status_bar.showMessage(
                f"🎮 游戏已装：{game_stats['total']} "
                f"（启用 {game_stats['enabled']} / 禁用 {game_stats['disabled']}）  |  "
                f"📁 仓库：{repo_stats['total']}"
            )
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════
    # 设置
    # ═══════════════════════════════════════════════════════

    def _open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            reload_config()
            self._config = get_config()
            self._load_theme()
            self._refresh_current()
            self.status_bar.showMessage("设置已保存")

    def _init_mod(self):
        """初始化 MOD 数据库"""
        db_path = get_db_path()
        is_first_init = not db_path.exists()

        if is_first_init:
            reply = QMessageBox.question(
                self,
                "初始化数据库",
                "确定要初始化数据库吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
        else:
            reply = QMessageBox.warning(
                self,
                "重置数据库",
                "此操作会重置mod数据库是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                progress = QProgressDialog("正在初始化数据库...", "取消", 0, 5, self)
                progress.setWindowTitle("请稍候")
                progress.setModal(True)
                progress.setMinimumDuration(0)
                progress.setValue(0)
                progress.show()

                progress.setLabelText("正在创建表结构...")
                progress.setValue(1)
                if progress.wasCanceled():
                    return
                init_db()

                progress.setLabelText("正在连接数据库...")
                progress.setValue(2)
                if progress.wasCanceled():
                    return
                session = get_session()
                mod_svc = ModService(session)

                progress.setLabelText("正在清理旧数据...")
                progress.setValue(3)
                if progress.wasCanceled():
                    session.close()
                    return
                session.query(OperationLog).delete()
                session.query(Mod).delete()
                session.commit()

                progress.setLabelText("正在扫描仓库 Mod...")
                progress.setValue(4)
                if progress.wasCanceled():
                    session.close()
                    return
                repo_count = mod_svc.refresh_from_disk(ModLocation.REPO)

                progress.setLabelText("正在扫描游戏 Mod...")
                progress.setValue(5)
                if progress.wasCanceled():
                    session.close()
                    return
                game_count = mod_svc.refresh_from_disk(ModLocation.GAME)

                session.close()
                progress.close()

                QMessageBox.information(
                    self,
                    "成功",
                    f"数据库初始化成功！\n"
                    f"游戏 Mod：{game_count} 个\n"
                    f"仓库 Mod：{repo_count} 个",
                    QMessageBox.StandardButton.Ok
                )
                self._refresh_current()
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "错误",
                    f"数据库初始化失败：{str(e)}",
                    QMessageBox.StandardButton.Ok
                )

    def _sync_mods(self):
        """磁盘 → 数据库 增量同步（后台线程）"""
        config = get_config()
        if not config.game_mod_path and not config.repo_path:
            QMessageBox.warning(
                self, "提示",
                "请先在设置中配置游戏 Mod 路径或本地仓库路径",
            )
            return

        reply = QMessageBox.question(
            self,
            "同步磁盘 Mod",
            "将以磁盘文件为准同步数据库：\n\n"
            "  • 磁盘新增的 zipmod → 导入数据库\n"
            "  • 磁盘已消失的文件 → 删除对应记录\n"
            "  • 文件改名/移动/换版本/启用禁用 → 更新记录（保留备注）\n\n"
            "继续同步？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        dialog = SyncDialog(self)
        dialog.sync_finished.connect(self._on_sync_finished)
        dialog.exec()

    def _on_sync_finished(self):
        """同步完成后刷新两个位置的列表"""
        # worker 用独立 session 提交，主窗口 session 的 identity map
        # 里可能还是旧对象，必须先过期再重查
        self._mod_service.session.expire_all()
        self._refresh_game_list()
        self._refresh_repo_list()

    def _version_scan(self):
        """仓库版本巡检 / 一键升级（F24，后台线程）"""
        config = get_config()
        if not config.game_mod_path or not config.repo_path:
            # 扫描读两侧、升级从仓库复制到游戏，两条路径缺一不可
            QMessageBox.warning(
                self,
                "提示",
                "仓库版本巡检需要同时配置游戏 Mod 路径和本地仓库路径，请先在设置中配置",
            )
            return
        dialog = VersionScanDialog(self)
        dialog.upgrade_finished.connect(self._on_version_upgrade_finished)
        dialog.exec()

    def _on_version_upgrade_finished(self):
        """F24 worker 用独立 session 提交，需过期后重查（同 _on_sync_finished）"""
        self._mod_service.session.expire_all()
        self._refresh_game_list()
        self._refresh_repo_list()

    def _duplicate_scan(self):
        """重复项检查 / 清理（F22，后台线程）"""
        config = get_config()
        if not config.game_mod_path and not config.repo_path:
            # 两侧独立扫描，至少需要一侧路径（同步磁盘的口径）
            QMessageBox.warning(
                self,
                "提示",
                "重复项检查需要配置游戏 Mod 路径或本地仓库路径，请先在设置中配置",
            )
            return
        dialog = DuplicateScanDialog(self)
        dialog.cleanup_finished.connect(self._on_duplicate_cleanup_finished)
        dialog.exec()

    def _on_duplicate_cleanup_finished(self):
        """F22 worker 用独立 session 提交，需过期后重查（同 _on_sync_finished）"""
        self._mod_service.session.expire_all()
        self._refresh_game_list()
        self._refresh_repo_list()

    def _load_theme(self):
        """加载主题样式"""
        theme = self._config.theme
        if theme == "dark":
            qss_path = Path(__file__).parent.parent.parent / "assets" / "styles" / "dark.qss"
            if qss_path.exists():
                self.setStyleSheet(qss_path.read_text(encoding="utf-8"))
        else:
            # 亮色主题：清除暗色 QSS，恢复系统默认样式
            self.setStyleSheet("")

        # 通知卡片解析页更新主题相关样式
        self.card_page.apply_theme(theme == "dark")

    def _check_first_run(self):
        """首次运行引导"""
        config = get_config()
        if not config.game_mod_path or not config.repo_path:
            QTimer.singleShot(500, lambda: QMessageBox.information(
                self,
                "欢迎使用 KK Mod Manager",
                "首次使用请先配置以下路径：\n\n"
                "1. 游戏 Mod 路径（Sideloader Mods 目录）\n"
                "2. 本地 Mod 仓库路径（Mod 收藏目录）\n"
                "3. 点击保存后再次打开设置即可初始化mod数据库信息\n\n"
                "点击「设置」按钮开始配置。",
            ))

    # ═══════════════════════════════════════════════════════
    # 窗口事件
    # ═══════════════════════════════════════════════════════

    def closeEvent(self, event):
        """关闭窗口时保存配置"""
        self._config.window_width = self.width()
        self._config.window_height = self.height()
        save_config(self._config)
        self._mod_service.session.close()
        event.accept()
