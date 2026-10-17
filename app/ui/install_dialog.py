"""安装确认对话框（F03-3/4 / F07 / F19）

需求文档 v1.5:
  - SAME_VERSION: 拒绝安装弹窗
  - NEWER_VERSION: 提示升级确认
  - OLDER_VERSION: 提示降级警告
  - F19-4: 无卡片上下文安装时询问目标分类（角色类 / 工作室类）
  - F19-9: 安装确认对话框显示目标分类
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QRadioButton,
)
from PyQt6.QtCore import Qt

from app.config import get_config
from app.services.manifest_service import (
    ModManifest,
    DuplicateReport,
    DuplicateResult,
)


class CategoryChoiceDialog(QDialog):
    """安装目标分类选择对话框（F19-4）

    无卡片上下文的安装入口（拖拽 zipmod / 手动安装）弹出，
    询问安装到角色类还是工作室类分类；两类落点取自设置。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        cfg = get_config().category
        self._chara = cfg.category_chara
        self._studio = cfg.category_studio

        self.setWindowTitle("选择安装分类")
        self.setMinimumWidth(400)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("该 Mod 安装到哪个分类？")
        title.setObjectName("title")
        layout.addWidget(title)

        hint = QLabel(
            "无卡片上下文，无法自动判断使用场景（F19-4）。\n"
            "角色类：人物卡 / 服装卡依赖；工作室类：场景卡【工作室】依赖。"
        )
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.chara_radio = QRadioButton(f"角色类（{self._chara}）")
        self.chara_radio.setChecked(True)
        layout.addWidget(self.chara_radio)

        self.studio_radio = QRadioButton(f"工作室类（{self._studio}）")
        layout.addWidget(self.studio_radio)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    @property
    def selected(self) -> str:
        """所选目标分类目录名"""
        return self._studio if self.studio_radio.isChecked() else self._chara


class InstallConfirmDialog(QDialog):
    """安装确认对话框"""

    def __init__(
        self,
        manifest: ModManifest,
        dup_report: DuplicateReport,
        action_name: str = "安装",
        parent=None,
        target_category: str = "",
    ):
        super().__init__(parent)
        self.manifest = manifest
        self.dup_report = dup_report
        self.action_name = action_name
        self.target_category = target_category  # F19 目标分类（空 = 平铺布局）
        self.confirm_upgrade = False
        self.confirm_downgrade = False
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle(f"{self.action_name}确认")
        self.setMinimumWidth(450)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── 标题 ──
        if self.dup_report.result == DuplicateResult.SAME_VERSION:
            title = QLabel(f"⛔ 重复{self.action_name}检测")
            title.setObjectName("title")
        elif self.dup_report.result == DuplicateResult.NEWER_VERSION:
            title = QLabel(f"🔄 检测到新版本")
            title.setObjectName("title")
        elif self.dup_report.result == DuplicateResult.OLDER_VERSION:
            title = QLabel(f"⚠️ 检测到更新版本")
            title.setObjectName("title")
        else:
            title = QLabel(f"🔄 检测到已存在的 Mod")
            title.setObjectName("title")
        layout.addWidget(title)

        # ── 分隔线 ──
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: #45475a;")
        layout.addWidget(line)

        # ── Mod 信息 ──
        info_layout = QVBoxLayout()
        info_layout.setSpacing(8)

        self._add_field(info_layout, "名称", self.manifest.name)
        self._add_field(info_layout, "GUID", self.manifest.guid)
        self._add_field(info_layout, "作者", self.manifest.display_author)
        if self.target_category:
            # F19-9：显示目标分类（三段式布局）
            self._add_field(
                info_layout, "目标分类",
                f"{self.target_category}/{self.manifest.display_author}",
            )

        if self.dup_report.result == DuplicateResult.SAME_VERSION:
            self._add_field(
                info_layout, "版本",
                f"{self.dup_report.existing_version}（已安装）= "
                f"{self.dup_report.incoming_version}（当前）"
            )
            hint = QLabel("无需重复安装。")
            hint.setObjectName("subtitle")
            info_layout.addWidget(hint)
        else:
            self._add_field(
                info_layout, "已安装版本", self.dup_report.existing_version
            )
            self._add_field(
                info_layout, "待安装版本", self.dup_report.incoming_version
            )

            if self.dup_report.result == DuplicateResult.NEWER_VERSION:
                hint = QLabel("新版本更高，升级将替换旧版本。是否继续？")
                hint.setStyleSheet("color: #a6e3a1;")
                info_layout.addWidget(hint)
            elif self.dup_report.result == DuplicateResult.OLDER_VERSION:
                hint = QLabel("⚠ 待安装版本比已安装版本更低，降级可能导致兼容性问题。")
                hint.setStyleSheet("color: #f9e2af;")
                info_layout.addWidget(hint)
            else:
                hint = QLabel("检测到已存在的 Mod，版本号格式无法比较，是否替换？")
                hint.setStyleSheet("color: #f9e2af;")
                info_layout.addWidget(hint)

        layout.addLayout(info_layout)

        # ── 分隔线 ──
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("background-color: #45475a;")
        layout.addWidget(line2)

        # ── 按钮 ──
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if self.dup_report.result == DuplicateResult.SAME_VERSION:
            ok_btn = QPushButton("确定")
            ok_btn.setObjectName("primary")
            ok_btn.clicked.connect(self.reject)
            btn_layout.addWidget(ok_btn)
        else:
            cancel_btn = QPushButton("取消")
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(cancel_btn)

            confirm_btn = QPushButton("确认替换")
            if self.dup_report.result == DuplicateResult.NEWER_VERSION:
                confirm_btn.setObjectName("success")
                confirm_btn.setText("升级安装")
            elif self.dup_report.result == DuplicateResult.OLDER_VERSION:
                confirm_btn.setObjectName("danger")
                confirm_btn.setText("降级安装")
            else:
                confirm_btn.setObjectName("primary")
                confirm_btn.setText("替换安装")
            confirm_btn.clicked.connect(self._on_confirm)
            btn_layout.addWidget(confirm_btn)

        layout.addLayout(btn_layout)

    def _add_field(self, layout: QVBoxLayout, label: str, value: str):
        row = QHBoxLayout()
        lbl = QLabel(f"{label}：")
        lbl.setStyleSheet("color: #a6adc8; font-weight: bold;")
        lbl.setFixedWidth(100)
        row.addWidget(lbl)

        val = QLabel(value)
        val.setWordWrap(True)
        val.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(val, 1)
        layout.addLayout(row)

    def _on_confirm(self):
        if self.dup_report.result == DuplicateResult.NEWER_VERSION:
            self.confirm_upgrade = True
        elif self.dup_report.result == DuplicateResult.OLDER_VERSION:
            self.confirm_downgrade = True
        else:
            self.confirm_upgrade = True  # 保守处理
        self.accept()
