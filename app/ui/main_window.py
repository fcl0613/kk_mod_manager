"""主窗口

需求文档 v1.3 - 7.1 主窗口布局：
  - 顶部工具栏（安装/添加/设置按钮，搜索框）
  - 顶层 TabBar（Mod管理 / 解析卡片）
  - Mod管理页：左侧 Mod 列表 + 右侧详情面板
  - 解析卡片页：图片预览 + Mod 依赖列表（GUID|Mod名称|状态|操作）
  - 底部状态栏
"""

from datetime import datetime
from enum import Enum
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QToolBar, QPushButton, QLineEdit, QLabel,
    QTreeWidget, QTreeWidgetItem, QSplitter,
    QTabWidget, QTabBar, QStackedWidget,
    QStatusBar, QFileDialog, QMessageBox, QProgressDialog,
)
from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from loguru import logger

from app.config import get_config, save_config, reload_config
from app.database import init_db, get_session, get_db_path
from app.models.mod import Mod, ModLocation, ModStatus, OperationLog
from app.services.mod_service import ModService, InstallResult
from app.services.card_service import CardService, CardParseResult
from app.services.update_service import (
    UpdateService, UpdateResult, UpdateStatus, RELEASES_PAGE_URL,
)
from app.ui.settings_dialog import SettingsDialog
from app.ui.install_dialog import InstallConfirmDialog
from app.ui.mod_detail_panel import ModDetailPanel
from app.ui.card_result_dialog import CardResultDialog
from app.ui.card_parse_page import CardParsePage
from app.ui.batch_install_dialog import BatchInstallDialog
from app.ui.update_dialog import UpdateDialog


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

        # 更新检查服务
        self._update_service = UpdateService(self)
        self._update_service.checked.connect(self._on_update_checked)
        # 记录上次已知"有更新"的版本，启动时用于恢复徽章
        self._known_available_version = self._config.update_notified_version
        # 当前打开的设置对话框（检查完成后即时刷新版本显示）
        self._settings_dialog: SettingsDialog | None = None

        self._setup_ui()
        self._load_theme()
        self._check_first_run()

        # 首次启动加载已有数据
        self._refresh_game_list()
        self._update_status_bar()

        # 如果上次已通知过某版本，恢复 NEW 徽章
        if self._known_available_version:
            self._set_update_badge_visible(True)

        # 延迟 3 秒后自动检查更新（避免阻塞首屏渲染）
        QTimer.singleShot(3000, self._auto_check_update)

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
        self.page_stack.addWidget(self.card_page)

        main_layout.addWidget(self.page_stack, 1)

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
        """创建顶部工具栏"""
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)

        # 安装到游戏
        install_btn = QPushButton("📦 安装到游戏")
        install_btn.clicked.connect(lambda: self._install_mod(ModLocation.GAME))
        toolbar.addWidget(install_btn)

        # 添加到仓库
        repo_btn = QPushButton("📁 添加到仓库")
        repo_btn.clicked.connect(lambda: self._install_mod(ModLocation.REPO))
        toolbar.addWidget(repo_btn)

        # 安装并收藏
        both_btn = QPushButton("📦+📁 安装并收藏")
        both_btn.clicked.connect(self._install_and_collect)
        toolbar.addWidget(both_btn)

        toolbar.addSeparator()

        # 批量添加到游戏
        batch_game_btn = QPushButton("🗂 批量→游戏")
        batch_game_btn.setToolTip("批量添加多个 .zipmod 文件到游戏 Mod 目录")
        batch_game_btn.clicked.connect(lambda: self._batch_install(ModLocation.GAME))
        toolbar.addWidget(batch_game_btn)

        # 批量添加到仓库
        batch_repo_btn = QPushButton("🗂 批量→仓库")
        batch_repo_btn.setToolTip("批量添加多个 .zipmod 文件到本地仓库")
        batch_repo_btn.clicked.connect(lambda: self._batch_install(ModLocation.REPO))
        toolbar.addWidget(batch_repo_btn)

        toolbar.addSeparator()

        # 刷新按钮
        refresh_btn = QPushButton("🔄 刷新")
        refresh_btn.clicked.connect(self._refresh_current)
        toolbar.addWidget(refresh_btn)

        toolbar.addSeparator()

        # 搜索框
        search_label = QLabel("搜索：")
        toolbar.addWidget(search_label)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("名称/作者/GUID...")
        self.search_edit.setFixedWidth(200)
        self.search_edit.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search_edit)

        toolbar.addSeparator()

        # 设置按钮
        self.settings_btn = QPushButton("⚙ 设置")
        self.settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(self.settings_btn)

        # NEW 徽章（叠加在设置按钮右上角，默认隐藏）
        self.update_badge = QLabel("NEW", self.settings_btn)
        self.update_badge.setObjectName("updateBadge")
        self.update_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_badge.setFixedSize(32, 16)
        self.update_badge.hide()
        # 初始化位置（按钮创建后第一次调整）
        QTimer.singleShot(0, self._reposition_update_badge)

        # 初始化MOD按钮
        init_mod_btn = QPushButton("🔄 初始化MOD")
        init_mod_btn.clicked.connect(self._init_mod)
        toolbar.addWidget(init_mod_btn)

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
            # 切换到解析卡片页
            if self._card_result is not None:
                self.card_page.set_card_result(self._card_result)

    def switch_to_card_page(self):
        """切换到卡片解析页面"""
        self.top_tab_bar.setCurrentIndex(1)
        self.page_stack.setCurrentIndex(1)

    # ═══════════════════════════════════════════════════════
    # 拖放处理（用于卡片拖放）
    # ═══════════════════════════════════════════════════════

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            for url in urls:
                path = Path(url.toLocalFile())
                if path.suffix.lower() == ".png":
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = Path(url.toLocalFile())
                if path.suffix.lower() == ".png":
                    self._parse_card(path)
                    break

    # ═══════════════════════════════════════════════════════
    # 卡片解析
    # ═══════════════════════════════════════════════════════

    def _parse_card(self, card_path: Path):
        """解析角色卡/服装卡，并在卡片解析页面显示结果"""
        logger.info("开始解析卡片: {}", card_path)
        self.status_bar.showMessage(f"正在解析卡片：{card_path.name}...")

        # 解析卡片
        result = self._card_service.parse_card(card_path)

        if not result.is_valid:
            logger.warning("卡片解析失败: {} - {}", card_path.name, result.error_message)
            QMessageBox.warning(
                self,
                "卡片解析失败",
                result.error_message,
            )
            self.status_bar.clearMessage()
            # 重置卡片页拖放区（避免一直显示"解析中..."）
            self.card_page.drop_zone.setText(
                "🎴 拖入角色卡/服装卡 (PNG) 进行 Mod 依赖分析"
            )
            self.card_page.stats_label.setText("拖入 PNG 卡片开始解析")
            return

        # 检查 Mod 可用性
        self._card_service.check_mod_availability(
            result,
            self._mod_service.session,
        )

        self._card_result = result

        # 切换到解析卡片页面并显示结果
        self.card_page.set_card_result(result)
        self.switch_to_card_page()

        logger.info(
            "卡片解析完成: {} - 总计={} 已安装={} 仓库有={} 缺失={}",
            card_path.name,
            result.total_count, result.installed_count,
            result.in_repo_count, result.missing_count,
        )
        self.status_bar.showMessage(
            f"卡片解析完成：{result.total_count} 个依赖 Mod | "
            f"已安装 {result.installed_count} | "
            f"仓库有 {result.in_repo_count} | "
            f"缺失 {result.missing_count}"
        )

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

    def install_repo_mod(self, mod_id: int) -> tuple[bool, str]:
        """从仓库安装 Mod 到游戏（供卡片结果面板调用）"""
        result = self._mod_service.install_from_repo(mod_id)
        if result.success:
            self._refresh_current()
        return result.success, result.message

    # ═══════════════════════════════════════════════════════
    # 卡片解析页面信号回调
    # ═══════════════════════════════════════════════════════

    def _on_card_page_install(self, mod_id: int):
        """卡片解析页：安装单个 Mod"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        name = mod.name if mod else f"ID={mod_id}"
        logger.info("卡片页安装请求: {} (ID={})", name, mod_id)
        self.status_bar.showMessage(f"正在从仓库安装：{name}...")

        success, msg = self.install_repo_mod(mod_id)
        QMessageBox.information(self, "安装结果", msg)

        if success:
            # 重新解析卡片并刷新页面
            new_result = self.reparse_card()
            if new_result:
                self.card_page.set_card_result(new_result)

    def _on_card_page_reparse(self):
        """卡片解析页：请求重新解析（全部安装后）"""
        new_result = self.reparse_card()
        if new_result:
            self.card_page.set_card_result(new_result)

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

        self._do_install(file_path, location)

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

        self.status_bar.showMessage("正在安装并收藏...")
        self._pending_path = Path(file_path)
        self._pending_action = PendingAction.INSTALL_AND_COLLECT
        result = self._mod_service.install_and_collect(Path(file_path))
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

        dialog = BatchInstallDialog(self._mod_service, location, parent=self)
        dialog.install_finished.connect(self._refresh_current)
        dialog.exec()

    def _do_install(self, file_path: str, location: ModLocation):
        """执行安装"""
        loc_text = "安装到游戏" if location == ModLocation.GAME else "添加到仓库"
        logger.info("{} - 开始: {}", loc_text, file_path)
        self.status_bar.showMessage(f"正在{loc_text}...")

        # 保存原始路径，用于重试
        self._pending_path = Path(file_path)
        self._pending_location = location
        self._pending_action = PendingAction.INSTALL

        result: InstallResult

        if location == ModLocation.GAME:
            result = self._mod_service.install_to_game(Path(file_path))
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
            if result.dup_report is None or result.manifest is None:
                QMessageBox.warning(self, "错误", "内部错误：缺少重复检测报告")
                return

            action = "安装" if (
                result.dup_report.existing_mod is not None
                and result.dup_report.existing_mod.location == ModLocation.GAME
            ) else "添加"

            dialog = InstallConfirmDialog(
                result.manifest,
                result.dup_report,
                action_name=action,
                parent=self,
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
                    )
                elif self._pending_location == ModLocation.GAME:
                    retry = self._mod_service.install_to_game(
                        self._pending_path,
                        confirm_upgrade=dialog.confirm_upgrade,
                        confirm_downgrade=dialog.confirm_downgrade,
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
        """从仓库安装 Mod 到游戏（详情面板按钮触发）"""
        mod = self._mod_service.get_mod_by_id(mod_id)
        if mod is None:
            return

        logger.info("从仓库安装到游戏: {} (ID={})", mod.name, mod_id)
        self.status_bar.showMessage(f"正在从仓库安装：{mod.name}...")
        result = self._mod_service.install_from_repo(mod_id)

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
        """刷新当前 Mod Tab 的列表"""
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
                item.setText(1, f"v{mod.version}")

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
        # 主窗口接收"立即检查更新"信号，触发实际检查
        dialog.update_check_requested.connect(self._on_manual_update_check)
        # 暴露给 _on_update_checked，使检查结果能即时刷新版本显示
        self._settings_dialog = dialog
        try:
            accepted = dialog.exec() == SettingsDialog.DialogCode.Accepted
        finally:
            self._settings_dialog = None

        if accepted:
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
        # 取消可能在飞行中的更新检查请求
        if hasattr(self, "_update_service"):
            self._update_service.cancel()
        event.accept()

    # ═══════════════════════════════════════════════════════
    # 更新检查
    # ═══════════════════════════════════════════════════════

    def _auto_check_update(self):
        """启动后自动检查更新（受配置和节流控制）"""
        logger.info("[update] 启动后自动检查更新入口")
        cfg = self._config
        if not cfg.update_check_enabled:
            logger.info("[update] 自动检查已禁用（update_check_enabled=False），跳过")
            return

        # 节流：距上次检查 < N 天则跳过
        last = cfg.update_last_check_at
        interval_days = cfg.update_check_interval_days
        logger.info(
            "[update] 节流检查：上次检查时间={} 间隔天数={} 当前版本={}",
            last or "（从未）", interval_days, self._update_service.current_version,
        )
        if last and interval_days > 0:
            try:
                last_dt = datetime.fromisoformat(last)
                elapsed = datetime.now() - last_dt
                if elapsed.days < interval_days:
                    logger.info(
                        "[update] 节流命中：距上次仅 {} 天（< {} 天），跳过自动检查",
                        elapsed.days, interval_days,
                    )
                    return
                else:
                    logger.info(
                        "[update] 节流未命中：距上次 {} 天（>= {} 天），继续检查",
                        elapsed.days, interval_days,
                    )
            except ValueError as e:
                logger.warning(
                    "[update] 上次检查时间戳格式损坏：{}={}，跳过节流检查",
                    last, e,
                )

        # 检测版本变化：用户已升级，主动重置 skip 字段
        if (
            cfg.update_notified_version
            and cfg.update_notified_version != self._update_service.current_version
            and self._update_service.current_version > "0.0.0"
        ):
            logger.info(
                "[update] 检测到版本变化：notified={} → current={}，重置 skipped_version",
                cfg.update_notified_version, self._update_service.current_version,
            )
            cfg.update_skipped_version = ""

        logger.info("[update] 触发自动检查请求 → UpdateService.check()")
        self._update_service.check()

    def _on_manual_update_check(self):
        """用户手动触发的检查（设置页按钮），绕过节流"""
        logger.info("[update] 手动检查更新入口（设置页按钮）")
        # 同时刷新已知版本（可能用户已升级）
        if self._config.update_notified_version != self._update_service.current_version:
            logger.info(
                "[update] 检测到版本变化：notified={} → current={}，重置 skip/notified",
                self._config.update_notified_version,
                self._update_service.current_version,
            )
            self._config.update_skipped_version = ""
            self._config.update_notified_version = ""
        # 若已有请求在飞行中，避免静默吞掉点击导致状态栏假象
        if self._update_service.is_busy():
            logger.info("[update] 上一次请求尚未完成，跳过本次手动触发")
            self.status_bar.showMessage("正在检查更新...（上一次请求尚未完成）", 5000)
            return
        logger.info("[update] 触发手动检查请求 → UpdateService.check()")
        self._update_service.check()
        # 使用 15 秒超时：若 check 真挂死（理论上 QTimer 应能 abort）也保证状态栏能恢复
        self.status_bar.showMessage("正在检查更新...", 15000)

    def _on_update_checked(self, result: UpdateResult):
        """UpdateService 结果回调"""
        logger.info(
            "[update] 收到结果：status={} current={} latest={} error={}",
            result.status.value, result.current, result.latest or "（空）",
            result.error or "（无）",
        )
        cfg = self._config

        # 统一写入"最近检查时间"
        cfg.update_last_check_at = datetime.now().isoformat()

        # 记录最新版本信息（供设置页展示 + "前往下载页面"按钮使用）
        if result.latest:
            cfg.update_latest_version = result.latest
            cfg.update_latest_url = result.release_url or RELEASES_PAGE_URL
            cfg.update_latest_is_prerelease = (
                result.status == UpdateStatus.PRERELEASE_AVAILABLE
            )
            logger.info(
                "[update] 记录最新版本：{}（prerelease={} url={}）",
                cfg.update_latest_version, cfg.update_latest_is_prerelease,
                cfg.update_latest_url,
            )

        if result.status == UpdateStatus.UPDATE_AVAILABLE:
            logger.info("[update] 分支：UPDATE_AVAILABLE → 调用 _handle_update_available")
            self._handle_update_available(result)
        elif result.status == UpdateStatus.PRERELEASE_AVAILABLE:
            logger.info("[update] 分支：PRERELEASE_AVAILABLE → 显示状态栏提示")
            logger.info("发现 prerelease 版本 {}，已忽略", result.latest)
            self.status_bar.showMessage(f"暂无稳定版更新（prerelease {result.latest}）", 3000)
        elif result.status == UpdateStatus.UP_TO_DATE:
            logger.info("[update] 分支：UP_TO_DATE → 清除徽章 + 显示提示")
            logger.debug("已是最新版本 {}", result.latest or cfg.update_last_check_at)
            # 已是最新时清除徽章
            self._set_update_badge_visible(False)
            self._known_available_version = ""
            cfg.update_notified_version = ""
            # 反馈给用户（覆盖"正在检查更新..."），3 秒后自动恢复统计信息
            if result.latest:
                self.status_bar.showMessage(f"已是最新版本 v{result.latest}", 3000)
            else:
                self.status_bar.showMessage("已是最新版本", 3000)
        elif result.status == UpdateStatus.NETWORK_ERROR:
            logger.info("[update] 分支：NETWORK_ERROR → 显示状态栏提示")
            logger.warning("更新检查网络错误: {}", result.error)
            self.status_bar.showMessage("暂时无法检查更新", 3000)
        elif result.status == UpdateStatus.RATE_LIMITED:
            logger.info("[update] 分支：RATE_LIMITED → 显示限速提示")
            logger.warning("更新检查被 GitHub API 限速")
            self.status_bar.showMessage("暂时无法检查更新，请稍后再试", 3000)
        elif result.status == UpdateStatus.PARSE_ERROR:
            logger.info("[update] 分支：PARSE_ERROR → 显示解析失败提示")
            logger.warning("更新检查解析失败: {}", result.error)
            self.status_bar.showMessage("暂时无法检查更新", 3000)
        else:
            logger.warning("[update] 未知 status：{}", result.status)

        try:
            save_config(cfg)
            logger.debug("[update] 配置已保存：update_last_check_at={}", cfg.update_last_check_at)
        except Exception as e:
            logger.warning("[update] 保存配置失败：{}", e)

        # 设置页正开着时即时刷新版本记录显示
        if self._settings_dialog is not None:
            self._settings_dialog.refresh_update_info()

    def _handle_update_available(self, result: UpdateResult):
        """处理发现新版本的情况：显示徽章 + 弹通知"""
        cfg = self._config

        # 显示徽章
        self._set_update_badge_visible(True)
        self._known_available_version = result.latest

        # 已通知过该版本且未被跳过 → 不再弹对话框
        already_notified = cfg.update_notified_version == result.latest
        is_skipped = cfg.update_skipped_version == result.latest

        if already_notified or is_skipped:
            logger.debug(
                "新版本 {} 已通知过={} 已跳过={}，不弹对话框",
                result.latest, already_notified, is_skipped,
            )
            self.status_bar.showMessage(
                f"新版本 v{result.latest} 可用（设置页查看详情）", 3000
            )
            return

        # 首次发现 → 弹对话框 + 记录已通知
        logger.info("首次发现新版本 v{}，弹出通知对话框", result.latest)
        dialog = UpdateDialog(result, parent=self)
        dialog.exec()

        cfg.update_notified_version = result.latest
        self.status_bar.showMessage(f"🎉 发现新版本 v{result.latest}", 5000)

    def _set_update_badge_visible(self, visible: bool):
        """显示/隐藏 NEW 徽章"""
        if visible:
            self._reposition_update_badge()
            self.update_badge.show()
            self.update_badge.raise_()
        else:
            self.update_badge.hide()

    def _reposition_update_badge(self):
        """将 NEW 徽章定位到设置按钮右上角"""
        if not hasattr(self, "update_badge") or not hasattr(self, "settings_btn"):
            return
        btn_w = self.settings_btn.width()
        badge_w = self.update_badge.width()
        # 右上角偏移
        self.update_badge.move(btn_w - badge_w + 4, -4)
