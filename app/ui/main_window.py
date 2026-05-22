"""主窗口

需求文档 v1.3 - 7.1 主窗口布局：
  - 顶部工具栏（安装/添加/设置按钮，搜索框）
  - 顶层 TabBar（Mod管理 / 解析卡片）
  - Mod管理页：左侧 Mod 列表 + 右侧详情面板
  - 解析卡片页：图片预览 + Mod 依赖列表（GUID|Mod名称|状态|操作）
  - 底部状态栏
"""

from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout,
    QToolBar, QPushButton, QLineEdit, QLabel,
    QTreeWidget, QTreeWidgetItem, QSplitter,
    QTabWidget, QTabBar, QStackedWidget,
    QStatusBar, QFileDialog, QMessageBox,
)
from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from loguru import logger

from app.config import get_config, save_config, reload_config
from app.database import init_db, get_session
from app.models.mod import Mod, ModLocation, ModStatus
from app.services.mod_service import ModService, InstallResult
from app.services.card_service import CardService, CardParseResult
from app.ui.settings_dialog import SettingsDialog
from app.ui.install_dialog import InstallConfirmDialog
from app.ui.mod_detail_panel import ModDetailPanel
from app.ui.card_result_dialog import CardResultDialog
from app.ui.card_parse_page import CardParsePage


class MainWindow(QMainWindow):
    """主窗口"""

    def __init__(self):
        super().__init__()
        self._config = get_config()
        self._card_result: CardParseResult | None = None  # 最近一次卡片解析结果
        self._pending_path: Path | None = None  # 等待重试的原始文件路径
        self._pending_location: ModLocation | None = None  # 等待重试的目标位置

        # 初始化数据库
        init_db()
        self._mod_service = ModService(get_session())
        self._card_service = CardService()

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
        settings_btn = QPushButton("⚙ 设置")
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)

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

        return panel

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
        self._pending_location = ModLocation.REPO  # 先装仓库
        result = self._mod_service.install_and_collect(file_path)
        self._handle_install_result(result)

    def _do_install(self, file_path: str, location: ModLocation):
        """执行安装"""
        loc_text = "安装到游戏" if location == ModLocation.GAME else "添加到仓库"
        logger.info("{} - 开始: {}", loc_text, file_path)
        self.status_bar.showMessage(f"正在{loc_text}...")

        # 保存原始路径，用于重试
        self._pending_path = Path(file_path)
        self._pending_location = location

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

                if self._pending_location == ModLocation.GAME:
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

    def _refresh_game_list(self):
        """刷新游戏 Mod 列表"""
        self.game_tree.clear()
        search = self.search_edit.text().strip()

        grouped = self._mod_service.group_by_author(ModLocation.GAME, search)
        stats = self._mod_service.get_statistics(ModLocation.GAME)

        self._populate_tree(self.game_tree, grouped)
        self._update_status_bar()

    def _refresh_repo_list(self):
        """刷新仓库 Mod 列表"""
        self.repo_tree.clear()
        search = self.search_edit.text().strip()

        grouped = self._mod_service.group_by_author(ModLocation.REPO, search)
        stats = self._mod_service.get_statistics(ModLocation.REPO)

        self._populate_tree(self.repo_tree, grouped)
        self._update_status_bar()

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
        self._refresh_current()
        self.detail_panel.clear()

    def _on_search(self, text: str):
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
                "2. 本地 Mod 仓库路径（Mod 收藏目录）\n\n"
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
