"""Mod 核心业务服务（F04 / F06 / F08 / F14 / F19）

需求文档 v1.5:
  - F04: Mod 安装到游戏（复制 zipmod，F19 分类布局 {游戏路径}/{分类}/{作者}/）
  - F14: Mod 添加到仓库（复制 zipmod 到 {仓库路径}/{作者}/，不分类）
  - F06: 卸载/移除（删除 zipmod 文件）
  - F08: 启用/禁用（修改文件扩展名）
  - F19: 分类目录安装（卡片视角路由，category 由调用方给出，不来自 zip 内容）

核心设计：
  - 安装 = 复制 .zipmod 到目标目录（不解压！）
  - GAME 端按分类+作者建目录并持久化 game_relative_path；REPO 端维持单层作者目录
  - 双重检测（游戏+仓库）防止重复
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session
from loguru import logger
from packaging.version import InvalidVersion, Version

from app.config import get_config
from app.models.mod import Mod, ModLocation, ModStatus, OperationLog
from app.services.manifest_service import (
    ManifestService,
    ModManifest,
    DuplicateResult,
    DuplicateReport,
    ManifestParseError,
)
from app.utils.file_utils import (
    safe_copy,
    delete_file,
    rename_extension,
    clean_empty_dirs,
    get_author_dir,
    compute_relative_path,
    compute_repo_relative_path,
    resolve_repo_mod_path,
    resolve_game_mod_path,
    infer_author_from_path,
    derive_category_from_relpath,
)
from app.utils.version_utils import display_version


@dataclass
class InstallResult:
    """安装/操作结果"""
    success: bool
    message: str
    mod: Mod | None = None
    duplicate_report: DuplicateReport | None = None
    manifest: ModManifest | None = None


# ═══════════════════════════════════════════════════════════
# 批量安装相关数据类
# ═══════════════════════════════════════════════════════════

class BatchItemStatus(Enum):
    """批量安装单项状态"""
    PENDING = "pending"         # 等待处理
    SUCCESS = "success"         # 成功
    SKIPPED = "skipped"         # 跳过（同版本已存在）
    CONFLICT = "conflict"       # 版本冲突（需用户处理）
    FAILED = "failed"           # 解析或复制失败


@dataclass
class BatchItemResult:
    """批量安装单条结果"""
    file_path: Path
    status: BatchItemStatus
    message: str
    manifest: ModManifest | None = None
    duplicate_report: DuplicateReport | None = None


@dataclass
class BatchInstallResult:
    """批量安装总结果"""
    total: int = 0
    success_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    failed_count: int = 0
    items: list[BatchItemResult] = field(default_factory=list)

    @property
    def conflicts(self) -> list[BatchItemResult]:
        """返回所有版本冲突项"""
        return [i for i in self.items if i.status == BatchItemStatus.CONFLICT]

    @property
    def failures(self) -> list[BatchItemResult]:
        """返回所有失败项"""
        return [i for i in self.items if i.status == BatchItemStatus.FAILED]


# ═══════════════════════════════════════════════════════════
# 磁盘同步相关数据类（Sync）
# ═══════════════════════════════════════════════════════════


@dataclass
class SyncChangeItem:
    """磁盘同步单项变更明细"""
    action: str            # "added" / "removed" / "updated" / "parse_failed"
    guid: str
    name: str
    file_name: str
    detail: str = ""       # 如 "v1.0.0 → v2.0.0"、"文件已消失"、"状态: 启用→禁用"


@dataclass
class SyncReport:
    """单位置磁盘同步报告"""
    location: ModLocation = ModLocation.GAME
    scanned: int = 0                       # 磁盘扫描到的 zipmod 文件数（含禁用）
    db_before: int = 0                     # 同步前该位置 DB 记录数
    added: list[SyncChangeItem] = field(default_factory=list)
    removed: list[SyncChangeItem] = field(default_factory=list)
    updated: list[SyncChangeItem] = field(default_factory=list)
    parse_failures: list[SyncChangeItem] = field(default_factory=list)
    skipped: bool = False                  # 路径未配置 / 目录不存在
    message: str = ""                      # skipped 原因

    @property
    def change_count(self) -> int:
        """新增 + 删除 + 更新总数"""
        return len(self.added) + len(self.removed) + len(self.updated)

    def summary(self) -> str:
        """一行中文摘要（供 UI / 操作日志使用）"""
        if self.skipped:
            return f"[{self.location.value}] 跳过：{self.message}"
        label = "游戏" if self.location == ModLocation.GAME else "仓库"
        return (
            f"[{label}] 扫描 {self.scanned} | 新增 {len(self.added)} | "
            f"删除 {len(self.removed)} | 更新 {len(self.updated)} | "
            f"解析失败 {len(self.parse_failures)}"
        )


# ═══════════════════════════════════════════════════════════
# 仓库版本巡检相关数据类（F24）
# ═══════════════════════════════════════════════════════════


@dataclass
class VersionDiffItem:
    """F24 巡检单项：GAME 与 REPO 同 GUID 的版本差异"""
    guid: str
    name: str                  # 展示名（GAME/REPO 同名）
    author: str
    game_version: str          # 原始字符串（展示层走 display_version）
    repo_version: str
    game_mod_id: int           # GAME 行 ID（参考）
    repo_mod_id: int           # REPO 行 ID（升级目标）
    category: str | None = None  # 由 game_relative_path 首段推导的 F19 分类；None=平铺布局


@dataclass
class VersionScanReport:
    """仓库版本巡检报告（F24-1）"""
    repo_newer: list[VersionDiffItem] = field(default_factory=list)   # 仓库更新（可升级）
    game_newer: list[VersionDiffItem] = field(default_factory=list)   # 游戏更新（仅参考，F24-3）
    scanned_game: int = 0
    scanned_repo: int = 0
    skipped_unparseable: int = 0    # BR-V03 排除（任一侧不可解析）的配对数

    def summary(self) -> str:
        """一行中文摘要（供 UI / 操作日志使用，对齐 SyncReport.summary）"""
        return (
            f"仓库版本巡检：对比 GAME {self.scanned_game} 行 / REPO {self.scanned_repo} 行 | "
            f"仓库更新 {len(self.repo_newer)} 项 | 游戏更新 {len(self.game_newer)} 项 | "
            f"版本不可比 {self.skipped_unparseable} 对（已忽略）"
        )


@dataclass
class UpgradeItemResult:
    """F24 批量升级单项结果"""
    guid: str
    name: str
    game_version: str          # 升级前的游戏版本（原始字符串）
    repo_version: str
    status: BatchItemStatus = BatchItemStatus.PENDING   # 复用批量安装状态枚举
    message: str = ""


@dataclass
class BatchUpgradeResult:
    """F24 批量升级总结果"""
    total: int = 0
    success_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    items: list[UpgradeItemResult] = field(default_factory=list)


def _normalize_author(author: str | None) -> str:
    """落库作者名：空值统一为 "Unknown"。

    与列表分组（group_by_author）、目录规则（get_author_dir）的展示一致，
    保证搜索 "Unknown" 能命中（空串不会 LIKE 匹配）。
    """
    return (author or "").strip() or "Unknown"


class ModService:
    """Mod 核心业务服务"""

    def __init__(self, db_session: Session):
        self.session = db_session
        self.manifest_svc = ManifestService()

    # ═══════════════════════════════════════════════════════════
    # 安装到游戏（F04 / F19）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _game_target_dir(game_root: Path, category: str | None, author: str) -> Path:
        """计算 GAME 端安装目标目录（F19，开发文档 _game_target_dir）。

        分类开关开启且调用方给出 category → {分类}/{作者}/ 三段式
        （两级目录都复用大小写不敏感匹配，BR-CAT01）；
        否则（category 为空或开关关闭）→ {作者}/ 旧公式（F19-5）。
        category 只能来自调用方（卡片上下文路由或用户选择），不来自 zip 内容（BR-CAT02）。
        """
        if category and get_config().category.game_category_enabled:
            category_dir = get_author_dir(game_root, category)
            return get_author_dir(category_dir, author)
        return get_author_dir(game_root, author)

    def install_to_game(
        self,
        zipmod_path: Path,
        confirm_upgrade: bool = False,
        confirm_downgrade: bool = False,
        category: str | None = None,
    ) -> InstallResult:
        """
        将 .zipmod 安装到游戏 Mod 路径。

        流程（F04-2）：
          1. 解析 manifest.xml
          2. 重复检测（游戏+仓库）
          3. 目标目录 = {游戏Mod路径}/{分类}/{作者名}/（F19，开关关/无分类回退 {作者名}/）
          4. 复制 zipmod 文件
          5. 写入数据库（含 game_relative_path）

        重复处理：
          - SAME_VERSION: 拒绝
          - NEWER_VERSION: 需要 confirm_upgrade=True
          - OLDER_VERSION: 需要 confirm_downgrade=True
        """
        config = get_config()

        # 1. 解析元数据
        try:
            manifest = self.manifest_svc.parse_from_zipmod(zipmod_path)
            logger.debug("解析 zipmod 成功: name={} version={} guid={}", manifest.name, manifest.version, manifest.guid)
        except ManifestParseError as e:
            logger.warning("zipmod 解析失败: {} - {}", zipmod_path.name, e)
            return InstallResult(success=False, message=str(e))

        # 2. 检查游戏路径是否配置
        if not config.game_mod_path:
            return InstallResult(
                success=False,
                message="请先在设置中配置游戏 Mod 路径",
                manifest=manifest,
            )

        game_root = Path(config.game_mod_path)
        if not game_root.exists():
            return InstallResult(
                success=False,
                message=f"游戏 Mod 路径不存在：{config.game_mod_path}",
                manifest=manifest,
            )

        # 3. 重复检测（在游戏路径中）
        dup = self.manifest_svc.check_duplicate(
            manifest, ModLocation.GAME, self.session
        )

        if dup.is_blocked:
            logger.info("安装被阻止（同版本已存在）: {} {}", manifest.name, display_version(manifest.version))
            return InstallResult(
                success=False,
                message=(
                    f"已安装相同版本的 Mod：{manifest.name} {display_version(manifest.version)}\n"
                    f"GUID: {manifest.guid}"
                ),
                duplicate_report=dup,
                manifest=manifest,
            )

        if dup.needs_confirmation:
            if dup.result == DuplicateResult.NEWER_VERSION and not confirm_upgrade:
                logger.info("需要确认升级: {} {} → {}", manifest.name, display_version(dup.existing_version), display_version(manifest.version))
                return InstallResult(
                    success=False,
                    message="NEWER_VERSION_NEEDS_CONFIRM",
                    duplicate_report=dup,
                    manifest=manifest,
                )
            if dup.result == DuplicateResult.OLDER_VERSION and not confirm_downgrade:
                logger.info("需要确认降级: {} {} → {}", manifest.name, display_version(dup.existing_version), display_version(manifest.version))
                return InstallResult(
                    success=False,
                    message="OLDER_VERSION_NEEDS_CONFIRM",
                    duplicate_report=dup,
                    manifest=manifest,
                )

        # 4. 复制文件到目标目录（F19 分类布局）
        #    F04-8: 先复制成功、再删旧版本 —— 复制失败时旧 Mod 完整保留
        target_dir = self._game_target_dir(game_root, category, manifest.author)
        logger.debug("安装目标目录: {}", target_dir)

        try:
            dest_file = safe_copy(
                src=zipmod_path,
                dst_dir=target_dir,
                file_name=zipmod_path.name,
                version=manifest.version,
            )
            logger.info("文件复制成功: {} → {}", zipmod_path.name, dest_file)
        except OSError as e:
            logger.error("文件复制失败: {} → {} - {}", zipmod_path.name, target_dir, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
                manifest=manifest,
            )

        # 5. 复制成功后移除旧版本（升级/降级）
        if dup.existing_mod is not None:
            logger.info("移除旧版本: {} {} (GUID={})", manifest.name, display_version(dup.existing_version), manifest.guid)
            self._remove_mod_files(dup.existing_mod, config.game_mod_path)
            self.session.delete(dup.existing_mod)
            self.session.flush()

        # 6. 数据库记录（持久化 game_relative_path，F19-6）
        mod = Mod(
            guid=manifest.guid,
            name=manifest.name,
            version=manifest.version,
            author=_normalize_author(manifest.author),
            author_normalized=manifest.author_normalized,
            description=manifest.description,
            website=manifest.website,
            file_name=dest_file.name,
            location=ModLocation.GAME,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
            game_relative_path=compute_relative_path(game_root, dest_file),
        )
        self.session.add(mod)

        # 7. 操作日志
        self._log_operation(
            operation="install",
            mod_name=manifest.name,
            mod_guid=manifest.guid,
            location="game",
            detail=f"安装 {display_version(manifest.version)} → {dest_file}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"安装成功：{manifest.name} {display_version(manifest.version)}",
            mod=mod,
            manifest=manifest,
        )

    # ═══════════════════════════════════════════════════════════
    # 添加到仓库（F14）
    # ═══════════════════════════════════════════════════════════

    def add_to_repo(
        self,
        zipmod_path: Path,
        confirm_upgrade: bool = False,
        confirm_downgrade: bool = False,
    ) -> InstallResult:
        """
        将 .zipmod 添加到本地仓库。

        与 install_to_game 逻辑相同，只是目标路径不同。
        """
        config = get_config()

        # 1. 解析
        try:
            manifest = self.manifest_svc.parse_from_zipmod(zipmod_path)
            logger.debug("解析 zipmod 成功(add_to_repo): name={} version={}", manifest.name, manifest.version)
        except ManifestParseError as e:
            logger.warning("zipmod 解析失败(add_to_repo): {} - {}", zipmod_path.name, e)
            return InstallResult(success=False, message=str(e))

        # 2. 校验仓库路径
        if not config.repo_path:
            return InstallResult(
                success=False,
                message="请先在设置中配置本地 Mod 仓库路径",
                manifest=manifest,
            )

        repo_root = Path(config.repo_path)
        if not repo_root.exists():
            return InstallResult(
                success=False,
                message=f"仓库路径不存在：{config.repo_path}",
                manifest=manifest,
            )

        # 3. 重复检测
        dup = self.manifest_svc.check_duplicate(
            manifest, ModLocation.REPO, self.session
        )

        if dup.is_blocked:
            logger.info("添加到仓库被阻止（同版本已存在）: {} {}", manifest.name, display_version(manifest.version))
            return InstallResult(
                success=False,
                message=f"仓库中已存在相同版本的 Mod：{manifest.name} {display_version(manifest.version)}",
                duplicate_report=dup,
                manifest=manifest,
            )

        if dup.needs_confirmation:
            if dup.result == DuplicateResult.NEWER_VERSION and not confirm_upgrade:
                return InstallResult(
                    success=False,
                    message="NEWER_VERSION_NEEDS_CONFIRM",
                    duplicate_report=dup,
                    manifest=manifest,
                )
            if dup.result == DuplicateResult.OLDER_VERSION and not confirm_downgrade:
                return InstallResult(
                    success=False,
                    message="OLDER_VERSION_NEEDS_CONFIRM",
                    duplicate_report=dup,
                    manifest=manifest,
                )

        # 4. 复制（F04-8: 先复制成功、再删旧版本，失败时旧 Mod 完整保留）
        author_dir = get_author_dir(repo_root, manifest.author)
        logger.debug("仓库目标目录: {}", author_dir)

        try:
            dest_file = safe_copy(
                src=zipmod_path,
                dst_dir=author_dir,
                file_name=zipmod_path.name,
                version=manifest.version,
            )
            logger.info("添加到仓库成功: {} → {}", zipmod_path.name, dest_file)
        except OSError as e:
            logger.error("添加到仓库文件复制失败: {} - {}", zipmod_path.name, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
                manifest=manifest,
            )

        # 5. 复制成功后移除旧版本（升级/降级）
        if dup.existing_mod is not None:
            self._remove_mod_files(dup.existing_mod, config.repo_path)
            self.session.delete(dup.existing_mod)
            self.session.flush()

        # 6. 数据库
        mod = Mod(
            guid=manifest.guid,
            name=manifest.name,
            version=manifest.version,
            author=_normalize_author(manifest.author),
            author_normalized=manifest.author_normalized,
            description=manifest.description,
            website=manifest.website,
            file_name=dest_file.name,
            location=ModLocation.REPO,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
            repo_relative_path=compute_repo_relative_path(repo_root, dest_file),
        )
        self.session.add(mod)

        self._log_operation(
            operation="repo_add",
            mod_name=manifest.name,
            mod_guid=manifest.guid,
            location="repo",
            detail=f"添加 {display_version(manifest.version)} → {dest_file}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"已添加到仓库：{manifest.name} {display_version(manifest.version)}",
            mod=mod,
            manifest=manifest,
        )

    # ═══════════════════════════════════════════════════════════
    # 安装并收藏（F14-2）
    # ═══════════════════════════════════════════════════════════

    def install_and_collect(
        self,
        zipmod_path: Path,
        confirm_upgrade: bool = False,
        confirm_downgrade: bool = False,
        category: str | None = None,
    ) -> InstallResult:
        """
        安装并收藏：先添加到仓库，再安装到游戏（Q-02 确认流程）。
        category 仅作用于游戏端安装（F19，仓库端不分类）。
        """
        logger.info("执行安装并收藏: {}", zipmod_path.name)
        # 先添加到仓库
        repo_result = self.add_to_repo(
            zipmod_path,
            confirm_upgrade=confirm_upgrade,
            confirm_downgrade=confirm_downgrade,
        )
        if not repo_result.success:
            logger.warning("安装并收藏 - 添加到仓库失败: {}", repo_result.message)
            return repo_result

        # 再安装到游戏
        game_result = self.install_to_game(
            zipmod_path,
            confirm_upgrade=confirm_upgrade,
            confirm_downgrade=confirm_downgrade,
            category=category,
        )

        if game_result.success:
            game_result.message = (
                f"安装并收藏成功：{game_result.manifest.name} "
                f"{display_version(game_result.manifest.version)}"
            )
        return game_result

    # ═══════════════════════════════════════════════════════════
    # 卸载 / 移除（F06）
    # ═══════════════════════════════════════════════════════════

    def uninstall(self, mod_id: int) -> InstallResult:
        """卸载游戏中的 Mod 或从仓库移除"""
        mod: Mod | None = self.session.get(Mod, mod_id)
        if mod is None:
            logger.warning("卸载失败 - Mod 不存在: ID={}", mod_id)
            return InstallResult(success=False, message=f"Mod 不存在：ID={mod_id}")

        config = get_config()

        # 删除文件
        if mod.location == ModLocation.GAME:
            # GAME 端优先用 game_relative_path（F19-6），为空回退作者目录旧公式
            base_path = config.game_mod_path
            file_path = resolve_game_mod_path(mod, Path(base_path))
        else:
            # REPO 端优先用 repo_relative_path
            base_path = config.repo_path
            file_path = resolve_repo_mod_path(mod, Path(base_path))

        if file_path.exists():
            try:
                delete_file(file_path)
                logger.info("删除文件: {}", file_path)
            except OSError as e:
                logger.error("删除文件失败: {} - {}", file_path, e)
                return InstallResult(
                    success=False,
                    message=f"删除文件失败：{e}",
                )

        # 清理空目录（GAME 端排除分类目录：分类目录即使被清空也不删除，BR-CAT06）
        if config.clean_empty_dirs:
            clean_empty_dirs(file_path.parent)
            if mod.location == ModLocation.GAME:
                clean_empty_dirs(
                    Path(base_path),
                    exclude_names=set(config.category.category_names),
                )
            else:
                clean_empty_dirs(Path(base_path))

        # 日志
        op_type = "uninstall" if mod.location == ModLocation.GAME else "repo_remove"
        self._log_operation(
            operation=op_type,
            mod_name=mod.name,
            mod_guid=mod.guid,
            location=mod.location.value,
            detail=f"删除 {mod.file_name}",
        )

        self.session.delete(mod)
        self.session.commit()

        loc_text = "卸载" if mod.location == ModLocation.GAME else "移除"
        return InstallResult(
            success=True,
            message=f"{loc_text}成功：{mod.name}",
        )

    # ═══════════════════════════════════════════════════════════
    # 启用 / 禁用（F08）
    # ═══════════════════════════════════════════════════════════

    def set_enabled(self, mod_id: int, enabled: bool) -> InstallResult:
        """
        启用/禁用游戏中的 Mod。

        禁用: .zipmod → .zipmod_disabled（F08-3）
        启用: .zipmod_disabled → .zipmod（F08-4）
        """
        mod: Mod | None = self.session.get(Mod, mod_id)
        if mod is None:
            return InstallResult(success=False, message=f"Mod 不存在：ID={mod_id}")

        if mod.location != ModLocation.GAME:
            return InstallResult(
                success=False,
                message="仅游戏中的 Mod 支持启用/禁用操作",
            )

        if enabled and mod.status == ModStatus.ENABLED:
            return InstallResult(success=True, message="Mod 已经是启用状态")

        if not enabled and mod.status == ModStatus.DISABLED:
            return InstallResult(success=True, message="Mod 已经是禁用状态")

        config = get_config()
        # 定位统一走 game_relative_path 优先（F19-6，basename 按当前状态替换）
        current_path = resolve_game_mod_path(mod, Path(config.game_mod_path))

        if enabled:
            # 启用：.zipmod_disabled → .zipmod
            old_path = current_path
            new_path = rename_extension(old_path, ".zipmod_disabled", ".zipmod")
            if new_path is None:
                logger.warning("启用失败 - 文件不存在: {}", old_path)
                return InstallResult(
                    success=False,
                    message=f"启用失败：文件不存在 {old_path}",
                )
            mod.file_name = new_path.name
            mod.status = ModStatus.ENABLED
            logger.info("启用 Mod: {} → {}", old_path, new_path)
        else:
            # 禁用：.zipmod → .zipmod_disabled
            old_path = current_path
            new_path = rename_extension(old_path, ".zipmod", ".zipmod_disabled")
            if new_path is None:
                logger.warning("禁用失败 - 文件不存在: {}", old_path)
                return InstallResult(
                    success=False,
                    message=f"禁用失败：文件不存在 {old_path}",
                )
            mod.file_name = new_path.name
            mod.status = ModStatus.DISABLED
            logger.info("禁用 Mod: {} → {}", old_path, new_path)

        op_type = "enable" if enabled else "disable"
        self._log_operation(
            operation=op_type,
            mod_name=mod.name,
            mod_guid=mod.guid,
            location="game",
            detail=f"{'启用' if enabled else '禁用'} {mod.file_name}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"{'启用' if enabled else '禁用'}成功：{mod.name}",
            mod=mod,
        )

    # ═══════════════════════════════════════════════════════════
    # 从仓库安装到游戏（卡片解析结果用，F15-5）
    # ═══════════════════════════════════════════════════════════

    def install_from_repo(
        self,
        mod_id: int,
        category: str | None = None,
    ) -> InstallResult:
        """
        将仓库中的 Mod 安装到游戏。

        用于卡片解析结果中的"一键安装"功能。
        category 为 F19 目标分类（由卡片依赖粗分路由或用户选择给出）。
        """
        repo_mod: Mod | None = self.session.get(Mod, mod_id)
        if repo_mod is None:
            logger.warning("从仓库安装失败 - Mod 不存在: ID={}", mod_id)
            return InstallResult(success=False, message=f"Mod 不存在：ID={mod_id}")

        if repo_mod.location != ModLocation.REPO:
            return InstallResult(
                success=False,
                message="只能从仓库安装 Mod 到游戏",
            )

        config = get_config()
        if not config.game_mod_path:
            return InstallResult(
                success=False,
                message="请先配置游戏 Mod 路径",
            )

        # 检查游戏中是否已存在
        game_dup = (
            self.session.query(Mod)
            .filter(
                Mod.guid == repo_mod.guid,
                Mod.location == ModLocation.GAME,
            )
            .first()
        )
        if game_dup is not None and game_dup.version == repo_mod.version:
            logger.info("从仓库安装被跳过（游戏已安装同版本）: {} {}", repo_mod.name, display_version(repo_mod.version))
            return InstallResult(
                success=False,
                message=f"游戏已安装相同版本：{repo_mod.name} {display_version(repo_mod.version)}",
            )

        # 从仓库复制到游戏（F04-8: 先复制成功、再删游戏旧版本，失败时旧 Mod 完整保留）
        repo_path = Path(config.repo_path)
        game_path = Path(config.game_mod_path)
        src_file = resolve_repo_mod_path(repo_mod, repo_path)

        if not src_file.exists():
            logger.error("从仓库安装失败 - 仓库文件不存在: {}", src_file)
            return InstallResult(
                success=False,
                message=f"仓库中文件不存在：{src_file}",
            )

        game_target_dir = self._game_target_dir(game_path, category, repo_mod.author)
        logger.debug("从仓库安装 - 复制: {} → {}", src_file, game_target_dir)
        try:
            dest_file = safe_copy(
                src=src_file,
                dst_dir=game_target_dir,
                file_name=src_file.name,
                version=repo_mod.version,
            )
            logger.info("从仓库安装成功: {} {} → {}", repo_mod.name, display_version(repo_mod.version), dest_file)
        except OSError as e:
            logger.error("从仓库安装文件复制失败: {} - {}", src_file.name, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
            )

        # F24-6: 升级保留游戏行禁用状态——状态必须与磁盘扩展名同步修改，
        # 否则下次磁盘同步会按文件后缀把状态改回启用、set_enabled 也会
        # 解析到不存在的 _disabled 路径；改名失败降级为启用态，不阻断升级
        preserve_disabled = game_dup is not None and game_dup.status == ModStatus.DISABLED
        final_file = dest_file
        if preserve_disabled and dest_file.suffix.lower() == ".zipmod":
            renamed = rename_extension(dest_file, ".zipmod", ".zipmod_disabled")
            if renamed is not None:
                final_file = renamed
            else:
                preserve_disabled = False

        # 复制成功后移除游戏旧版本（升级）
        if game_dup is not None:
            logger.info("从仓库安装 - 移除游戏旧版本: {} {}", game_dup.name, display_version(game_dup.version))
            self._remove_mod_files(game_dup, config.game_mod_path)
            self.session.delete(game_dup)
            self.session.flush()

        game_mod = Mod(
            guid=repo_mod.guid,
            name=repo_mod.name,
            version=repo_mod.version,
            author=_normalize_author(repo_mod.author),
            author_normalized=repo_mod.author_normalized,
            description=repo_mod.description,
            website=repo_mod.website,
            file_name=final_file.name,
            location=ModLocation.GAME,
            status=ModStatus.DISABLED if preserve_disabled else ModStatus.ENABLED,
            installed_at=datetime.now(),
            game_relative_path=compute_relative_path(game_path, final_file),
        )
        self.session.add(game_mod)

        self._log_operation(
            operation="install",
            mod_name=repo_mod.name,
            mod_guid=repo_mod.guid,
            location="game",
            detail=(
                f"从仓库安装 {display_version(repo_mod.version)}"
                + ("（保持禁用）" if preserve_disabled else "")
            ),
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"已安装到游戏：{repo_mod.name} {display_version(repo_mod.version)}",
            mod=game_mod,
        )

    # ═══════════════════════════════════════════════════════════
    # 从游戏添加到仓库
    # ═══════════════════════════════════════════════════════════

    def add_game_mod_to_repo(self, mod_id: int) -> InstallResult:
        """
        将游戏中的 Mod 添加到本地仓库。

        与 install_from_repo 对称的「反向」操作。
        """
        game_mod: Mod | None = self.session.get(Mod, mod_id)
        if game_mod is None:
            logger.warning("添加仓库失败 - Mod 不存在: ID={}", mod_id)
            return InstallResult(success=False, message=f"Mod 不存在：ID={mod_id}")

        if game_mod.location != ModLocation.GAME:
            return InstallResult(
                success=False,
                message="只能将游戏中的 Mod 添加到仓库",
            )

        config = get_config()
        if not config.repo_path:
            return InstallResult(
                success=False,
                message="请先配置本地 Mod 仓库路径",
            )

        # 检查仓库中是否已存在
        repo_dup = (
            self.session.query(Mod)
            .filter(
                Mod.guid == game_mod.guid,
                Mod.location == ModLocation.REPO,
            )
            .first()
        )
        if repo_dup is not None and repo_dup.version == game_mod.version:
            logger.info("添加到仓库被跳过（仓库已存在同版本）: {} {}", game_mod.name, display_version(game_mod.version))
            return InstallResult(
                success=False,
                message=f"仓库中已存在相同版本：{game_mod.name} {display_version(game_mod.version)}",
            )

        # 从游戏复制到仓库（源定位走 game_relative_path 优先，F19-6）
        # F04-8: 先复制成功、再删仓库旧版本，失败时旧 Mod 完整保留
        game_path = Path(config.game_mod_path)
        repo_path = Path(config.repo_path)
        src_file = resolve_game_mod_path(game_mod, game_path)

        if not src_file.exists():
            logger.error("添加到仓库失败 - 游戏文件不存在: {}", src_file)
            return InstallResult(
                success=False,
                message=f"游戏文件不存在：{src_file}",
            )

        repo_author_dir = get_author_dir(repo_path, game_mod.author)
        logger.debug("添加到仓库 - 复制: {} → {}", src_file, repo_author_dir)
        try:
            dest_file = safe_copy(
                src=src_file,
                dst_dir=repo_author_dir,
                file_name=src_file.name,
                version=game_mod.version,
            )
            logger.info("添加到仓库成功: {} {} → {}", game_mod.name, display_version(game_mod.version), dest_file)
        except OSError as e:
            logger.error("添加到仓库文件复制失败: {} - {}", src_file.name, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
            )

        # 复制成功后移除仓库旧版本（升级）
        if repo_dup is not None:
            logger.info("添加到仓库 - 移除仓库旧版本: {} {}", repo_dup.name, display_version(repo_dup.version))
            self._remove_mod_files(repo_dup, config.repo_path)
            self.session.delete(repo_dup)
            self.session.flush()

        repo_mod = Mod(
            guid=game_mod.guid,
            name=game_mod.name,
            version=game_mod.version,
            author=_normalize_author(game_mod.author),
            author_normalized=game_mod.author_normalized,
            description=game_mod.description,
            website=game_mod.website,
            file_name=dest_file.name,
            location=ModLocation.REPO,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
            repo_relative_path=compute_repo_relative_path(repo_path, dest_file),
        )
        self.session.add(repo_mod)

        self._log_operation(
            operation="repo_add",
            mod_name=game_mod.name,
            mod_guid=game_mod.guid,
            location="repo",
            detail=f"从游戏添加 {display_version(game_mod.version)}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"已加入仓库：{game_mod.name} {display_version(game_mod.version)}",
            mod=repo_mod,
        )

    # ═══════════════════════════════════════════════════════════
    # 仓库版本巡检 / 一键升级（F24）
    # ═══════════════════════════════════════════════════════════

    def scan_repo_versions(self) -> VersionScanReport:
        """全表对比 GAME 与 REPO 同 GUID 版本（F24-1）。

        - packaging.Version 严格比较；BR-V03 保守口径：任一侧 InvalidVersion
          → 该配对整体不参与（既不算仓库更新也不算游戏更新）
        - 版本相等（含 "1.0" vs "v1.0" 这类解析后相等的原始串）→ 不列出
        - GUID 仅单侧存在 → 不参与（不是版本差异）
        - 纯 DB 查询 + 内存比较，不触碰磁盘；有差异时记一条聚合操作日志
        """
        report = VersionScanReport()
        game_mods = self.session.query(Mod).filter(Mod.location == ModLocation.GAME).all()
        repo_by_guid = {
            m.guid: m
            for m in self.session.query(Mod).filter(Mod.location == ModLocation.REPO).all()
        }
        report.scanned_game = len(game_mods)
        report.scanned_repo = len(repo_by_guid)
        category_names = get_config().category.category_names

        for game_mod in game_mods:
            repo_mod = repo_by_guid.get(game_mod.guid)
            if repo_mod is None:
                continue  # GUID 单侧存在 → 不是版本差异
            try:
                game_v = Version(game_mod.version)
                repo_v = Version(repo_mod.version)
            except InvalidVersion:
                # BR-V03：版本无法解析时不断言新旧，配对整体排除
                report.skipped_unparseable += 1
                continue
            if repo_v > game_v:
                report.repo_newer.append(
                    VersionDiffItem(
                        guid=game_mod.guid,
                        name=game_mod.name or repo_mod.name or game_mod.guid,
                        author=game_mod.author or repo_mod.author or "",
                        game_version=game_mod.version,
                        repo_version=repo_mod.version,
                        game_mod_id=game_mod.id,
                        repo_mod_id=repo_mod.id,
                        category=derive_category_from_relpath(
                            game_mod.game_relative_path, category_names
                        ),
                    )
                )
            elif game_v > repo_v:
                # 游戏更新（降级方向）：仅参考展示，不推导分类（F24-3 不可操作）
                report.game_newer.append(
                    VersionDiffItem(
                        guid=game_mod.guid,
                        name=game_mod.name or repo_mod.name or game_mod.guid,
                        author=game_mod.author or repo_mod.author or "",
                        game_version=game_mod.version,
                        repo_version=repo_mod.version,
                        game_mod_id=game_mod.id,
                        repo_mod_id=repo_mod.id,
                    )
                )
            # 相等 → 不列出

        if report.repo_newer or report.game_newer:
            self._log_operation(
                operation="upgrade",
                mod_name="(仓库版本巡检)",
                mod_guid="",
                location="game",
                detail=report.summary()[:450],
            )
            self.session.commit()

        logger.info("仓库版本巡检完成: {}", report.summary())
        return report

    def batch_upgrade_from_repo(
        self,
        repo_mod_ids: list[int],
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> BatchUpgradeResult:
        """按巡检结果批量从仓库升级游戏侧（F24-2/4/5）。

        逐项复用 install_from_repo（F04-8 防丢序 + 逐项 OperationLog + 单项独立 commit）。
        分类在执行时从 GAME 行 game_relative_path 现场推导（保证与磁盘当前布局
        一致，避免分类布局的 Mod 被隐式搬到平铺目录）；游戏行已不在（被卸载）
        → 按 None 平铺安装走全新安装分支。单项失败/意外异常不中断批量。
        """
        result = BatchUpgradeResult(total=len(repo_mod_ids))
        config = get_config()

        # 前置路径校验（镜像 batch_install 口径）：两侧任一未配置 → 全部 FAILED
        if not config.game_mod_path or not config.repo_path:
            reason = (
                "未配置游戏 Mod 路径，请先在设置中配置"
                if not config.game_mod_path
                else "未配置仓库路径，请先在设置中配置"
            )
            for repo_mod_id in repo_mod_ids:
                item = UpgradeItemResult(guid="", name=f"ID={repo_mod_id}", game_version="", repo_version="",
                                         status=BatchItemStatus.FAILED, message=reason)
                result.items.append(item)
                result.failed_count += 1
            return result

        category_names = config.category.category_names
        total = len(repo_mod_ids)
        for idx, repo_mod_id in enumerate(repo_mod_ids):
            repo_mod = self.session.get(Mod, repo_mod_id)
            name = repo_mod.name if repo_mod is not None else f"ID={repo_mod_id}"
            if progress_callback:
                progress_callback(idx, total, name)

            game_row = (
                self.get_game_mod_by_guid(repo_mod.guid) if repo_mod is not None else None
            )
            item = UpgradeItemResult(
                guid=repo_mod.guid if repo_mod is not None else "",
                name=name,
                game_version=game_row.version if game_row is not None else "",
                repo_version=repo_mod.version if repo_mod is not None else "",
            )
            category = (
                derive_category_from_relpath(game_row.game_relative_path, category_names)
                if game_row is not None
                else None
            )
            try:
                res = self.install_from_repo(repo_mod_id, category)
            except Exception as e:  # noqa: BLE001 - 单项意外异常回滚后继续批量
                self.session.rollback()
                item.status = BatchItemStatus.FAILED
                item.message = f"升级异常：{e}"
                result.items.append(item)
                result.failed_count += 1
                continue

            if res.success:
                item.status = BatchItemStatus.SUCCESS
                result.success_count += 1
            else:
                # 失败原因甄别：执行后游戏行版本已与仓库一致 → 同版本跳过
                # （扫描后游戏侧已被更新等场景），比匹配 message 子串稳健
                game_now = (
                    self.get_game_mod_by_guid(item.guid)
                    if repo_mod is not None and item.guid
                    else None
                )
                if game_now is not None and game_now.version == repo_mod.version:
                    item.status = BatchItemStatus.SKIPPED
                    result.skipped_count += 1
                else:
                    item.status = BatchItemStatus.FAILED
                    result.failed_count += 1
            item.message = res.message
            result.items.append(item)

        if progress_callback:
            progress_callback(total, total, "完成")
        logger.info(
            "批量升级完成: 总={} 成功={} 跳过={} 失败={}",
            result.total, result.success_count, result.skipped_count, result.failed_count,
        )
        return result

    # ═══════════════════════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════════════════════

    def get_mods_by_location(
        self,
        location: ModLocation,
        search: str = "",
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Mod]:
        """
        查询指定位置的 Mod 列表（支持分页）。

        Args:
            location: Mod 位置
            search: 搜索关键字
            limit: 每页数量，None 表示不限制（全部返回）
            offset: 跳过数量

        Returns:
            Mod 列表
        """
        query = self.session.query(Mod).filter(Mod.location == location)

        if search:
            pattern = f"%{search}%"
            query = query.filter(
                (Mod.name.like(pattern))
                | (Mod.author.like(pattern))
                | (Mod.guid.like(pattern))
            )

        query = query.order_by(Mod.author_normalized, Mod.name)

        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)

        return query.all()

    def get_mods_count(
        self,
        location: ModLocation,
        search: str = "",
    ) -> int:
        """获取指定位置 Mod 总数（用于分页）"""
        query = self.session.query(Mod).filter(Mod.location == location)

        if search:
            pattern = f"%{search}%"
            query = query.filter(
                (Mod.name.like(pattern))
                | (Mod.author.like(pattern))
                | (Mod.guid.like(pattern))
            )

        return query.count()

    def get_mod_by_id(self, mod_id: int) -> Mod | None:
        """根据 ID 获取 Mod"""
        return self.session.get(Mod, mod_id)

    def get_repo_mod_by_guid(self, guid: str) -> Mod | None:
        """根据 GUID 查询仓库中的 Mod"""
        return (
            self.session.query(Mod)
            .filter(Mod.guid == guid, Mod.location == ModLocation.REPO)
            .first()
        )

    def get_game_mod_by_guid(self, guid: str) -> Mod | None:
        """根据 GUID 查询游戏中的 Mod"""
        return (
            self.session.query(Mod)
            .filter(Mod.guid == guid, Mod.location == ModLocation.GAME)
            .first()
        )

    def get_statistics(self, location: ModLocation) -> dict:
        """获取统计信息"""
        total = (
            self.session.query(Mod)
            .filter(Mod.location == location)
            .count()
        )
        enabled = (
            self.session.query(Mod)
            .filter(Mod.location == location, Mod.status == ModStatus.ENABLED)
            .count()
        )
        return {
            "total": total,
            "enabled": enabled,
            "disabled": total - enabled,
        }

    def group_by_author(
        self,
        location: ModLocation,
        search: str = "",
    ) -> dict[str, list[Mod]]:
        """
        按作者分组 Mod 列表（用于 UI 展示）。

        Returns:
            {author_display_name: [Mod, ...]}，保持插入顺序
        """
        mods = self.get_mods_by_location(location, search)
        result: dict[str, list[Mod]] = {}
        for mod in mods:
            key = mod.author.strip() if mod.author else "Unknown"
            if key not in result:
                result[key] = []
            result[key].append(mod)
        return result

    def group_by_author_paginated(
        self,
        location: ModLocation,
        search: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> tuple[dict[str, list[Mod]], int]:
        """
        按作者分组 Mod 列表（支持分页）。

        先按 author 分组，再对每个组的 Mod 数量求和得到总数。
        分页作用于最外层的 Mod 列表。

        Args:
            location: Mod 位置
            search: 搜索关键字
            page: 页码（从 1 开始）
            page_size: 每页 Mod 数量

        Returns:
            (grouped_mods, total_count) 元组
        """
        total = self.get_mods_count(location, search)
        offset = (page - 1) * page_size
        mods = self.get_mods_by_location(location, search, limit=page_size, offset=offset)

        result: dict[str, list[Mod]] = {}
        for mod in mods:
            key = mod.author.strip() if mod.author else "Unknown"
            if key not in result:
                result[key] = []
            result[key].append(mod)
        return result, total

    def refresh_from_disk(
        self,
        location: ModLocation,
    ) -> int:
        """
        从磁盘重新扫描 Mod 并同步数据库。

        用途：用户手动在文件系统中增删 Mod 后刷新。
        注意：此操作会清空该位置的所有记录并重新扫描。

        Returns:
            扫描到的 Mod 数量
        """
        config = get_config()
        base_path = (
            Path(config.game_mod_path) if location == ModLocation.GAME
            else Path(config.repo_path)
        )

        if not base_path.exists():
            return 0

        # 清空旧记录
        self.session.query(Mod).filter(Mod.location == location).delete()
        self.session.flush()

        count = 0
        seen_guids: set[str] = set()
        for zipmod_file in base_path.rglob("*.zipmod"):
            if not zipmod_file.is_file():
                continue
            try:
                manifest = self.manifest_svc.parse_from_zipmod(zipmod_file)
            except ManifestParseError as e:
                logger.warning("刷新扫描 - 跳过无效 Mod: {} - {}", zipmod_file, e)
                continue

            # 同一位置下按 GUID 去重（避免硬链接导致重复扫描）
            if manifest.guid in seen_guids:
                continue
            seen_guids.add(manifest.guid)

            # 作者推断：
            #   GAME 端用 BR-CAT03 护栏（分类段自底向上排除，不受分类开关控制）；
            #   REPO 端维持单层父目录推断（仓库不分类）。
            if not manifest.author:
                if location == ModLocation.GAME:
                    manifest.author = infer_author_from_path(
                        zipmod_file, base_path, config.category.category_names
                    ) or ""
                elif zipmod_file.parent != base_path:
                    manifest.author = zipmod_file.parent.name

            mod = Mod(
                guid=manifest.guid,
                name=manifest.name,
                version=manifest.version,
                author=_normalize_author(manifest.author),
                author_normalized=manifest.author_normalized,
                description=manifest.description,
                website=manifest.website,
                file_name=zipmod_file.name,
                location=location,
                status=ModStatus.ENABLED,
                installed_at=datetime.fromtimestamp(
                    zipmod_file.stat().st_mtime
                ),
                repo_relative_path=(
                    zipmod_file.relative_to(base_path).as_posix()
                    if location == ModLocation.REPO else ""
                ),
                game_relative_path=(
                    zipmod_file.relative_to(base_path).as_posix()
                    if location == ModLocation.GAME else ""
                ),
            )
            self.session.add(mod)
            count += 1

        self.session.commit()
        return count

    def sync_from_disk(
        self,
        location: ModLocation,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> SyncReport:
        """以磁盘为准增量同步数据库（不改动磁盘文件）。

        与 refresh_from_disk 的区别：
          - 增量 diff 而非清空重建，保留 notes / installed_at
          - 扫描 .zipmod 与 .zipmod_disabled，按后缀同步禁用状态
          - 记录 OperationLog 摘要

        Args:
            location: 同步位置（GAME / REPO）
            progress_callback: 进度回调 (当前序号, 总数, 文件名)

        Returns:
            SyncReport 同步报告
        """
        report = SyncReport(location=location)

        config = get_config()
        if location == ModLocation.GAME:
            base_str, label = config.game_mod_path, "游戏 Mod 路径"
        else:
            base_str, label = config.repo_path, "仓库路径"
        if not base_str:
            report.skipped = True
            report.message = f"未配置{label}"
            return report
        base_path = Path(base_str)
        if not base_path.exists():
            report.skipped = True
            report.message = f"{label}不存在：{base_str}"
            return report

        # ── 阶段 1：扫描磁盘，解析 manifest ──────────────────
        disk_files = sorted(
            f for f in base_path.rglob("*")
            if f.is_file() and f.suffix.lower() in (".zipmod", ".zipmod_disabled")
        )
        report.scanned = len(disk_files)

        disk_by_guid: dict[str, list[dict]] = {}
        fail_paths: set[Path] = set()
        for i, file_path in enumerate(disk_files):
            if progress_callback:
                progress_callback(i, len(disk_files), file_path.name)
            try:
                manifest = self.manifest_svc.parse_from_zipmod(file_path)
            except ManifestParseError as e:
                fail_paths.add(file_path)
                report.parse_failures.append(SyncChangeItem(
                    action="parse_failed", guid="", name=file_path.name,
                    file_name=file_path.name, detail=str(e)[:200],
                ))
                continue
            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = datetime.now().timestamp()
            disk_by_guid.setdefault(manifest.guid, []).append({
                "path": file_path,
                "manifest": manifest,
                "is_disabled": file_path.name.lower().endswith(".zipmod_disabled"),
                "mtime": mtime,
            })

        # ── 阶段 2：加载 DB，清理同位置重复 GUID 的脏数据 ────
        db_mods = self.session.query(Mod).filter(Mod.location == location).all()
        report.db_before = len(db_mods)
        db_by_guid: dict[str, Mod] = {}
        for mod in db_mods:
            if mod.guid in db_by_guid:
                # 唯一索引外的历史脏数据：保留先出现的一条
                self.session.delete(mod)
                report.removed.append(SyncChangeItem(
                    action="removed", guid=mod.guid, name=mod.name,
                    file_name=mod.file_name, detail="清理重复记录",
                ))
            else:
                db_by_guid[mod.guid] = mod

        # ── 阶段 3：磁盘有 GUID → 新增或原地更新 ────────────
        for guid, entries in disk_by_guid.items():
            mod = db_by_guid.get(guid)

            # 同 GUID 多文件：优先取 DB 推导路径所在文件，
            # 否则启用态优先 + 路径字典序（保证确定性）；多余文件不动
            chosen = None
            if mod is not None:
                expect = self._derive_mod_path(mod, base_path)
                for entry in entries:
                    if os.path.normcase(str(entry["path"])) == os.path.normcase(str(expect)):
                        chosen = entry
                        break
            if chosen is None:
                entries.sort(key=lambda e: (e["is_disabled"], str(e["path"])))
                chosen = entries[0]

            if mod is None:
                manifest = chosen["manifest"]
                author = _normalize_author(manifest.author)
                if not manifest.author:
                    # 作者推断：GAME 端用 BR-CAT03 护栏（分类段排除，
                    # 不受分类开关控制）；REPO 端单层父目录（与 refresh_from_disk 一致）
                    if location == ModLocation.GAME:
                        author = _normalize_author(infer_author_from_path(
                            chosen["path"], base_path, config.category.category_names
                        ))
                    elif chosen["path"].parent != base_path:
                        author = chosen["path"].parent.name
                new_mod = Mod(
                    guid=guid,
                    name=manifest.name,
                    version=manifest.version,
                    author=author,
                    author_normalized=author.strip().lower(),
                    description=manifest.description,
                    website=manifest.website,
                    file_name=chosen["path"].name,
                    location=location,
                    status=(
                        ModStatus.DISABLED
                        if chosen["is_disabled"] and location == ModLocation.GAME
                        else ModStatus.ENABLED
                    ),
                    installed_at=datetime.fromtimestamp(chosen["mtime"]),
                    repo_relative_path=(
                        chosen["path"].relative_to(base_path).as_posix()
                        if location == ModLocation.REPO else ""
                    ),
                    game_relative_path=(
                        chosen["path"].relative_to(base_path).as_posix()
                        if location == ModLocation.GAME else ""
                    ),
                )
                self.session.add(new_mod)
                report.added.append(SyncChangeItem(
                    action="added", guid=guid, name=manifest.name,
                    file_name=chosen["path"].name,
                    detail=f"{display_version(manifest.version)} 作者 {author}",
                ))
            else:
                item = self._apply_mod_update(
                    mod, chosen["manifest"], chosen["path"], base_path, location,
                )
                if item is not None:
                    report.updated.append(item)

        # ── 阶段 4：DB 有、磁盘无该 GUID → 删除或保留 ────────
        for guid, mod in db_by_guid.items():
            if guid in disk_by_guid:
                continue
            expect = self._derive_mod_path(mod, base_path)
            if expect.exists():
                if expect in fail_paths:
                    # 文件还在但解析失败（损坏 / 下载中），保留记录
                    logger.warning("同步 - 文件解析失败，保留记录: {} ({})", expect.name, guid)
                    continue
                # 路径被其他 GUID 的文件占用 → 原地被替换，删旧记录（新 GUID 已在阶段 3 新增）
                self.session.delete(mod)
                report.removed.append(SyncChangeItem(
                    action="removed", guid=guid, name=mod.name,
                    file_name=mod.file_name, detail="文件被替换为其他 Mod",
                ))
            elif (
                location == ModLocation.GAME
                and any(
                    fp.name.lower() == mod.current_file_name.lower()
                    for fp in fail_paths
                )
            ):
                # GAME 端路径是推导值（作者目录），文件可能实际在别处：
                # 按文件名兜底匹配解析失败文件，避免误删
                logger.warning(
                    "同步 - 疑似解析失败，保留记录: {} ({})", mod.current_file_name, guid,
                )
            else:
                # 文件已消失
                self.session.delete(mod)
                report.removed.append(SyncChangeItem(
                    action="removed", guid=guid, name=mod.name,
                    file_name=mod.file_name, detail="文件已消失",
                ))

        # ── 阶段 5：单事务提交（先 flush 删除，防御 INSERT 先于
        #    DELETE 的 flush 顺序撞 (location, guid) 唯一索引）──
        try:
            self.session.flush()
            self.session.commit()
        except Exception as e:
            self.session.rollback()
            logger.error("磁盘同步失败({}): {}", location.value, e)
            raise

        if report.change_count or report.parse_failures:
            self._log_operation(
                operation="sync",
                mod_name=f"(磁盘同步 {location.value})",
                mod_guid="",
                location=location.value,
                detail=report.summary()[:450],
            )
            self.session.commit()

        if progress_callback:
            progress_callback(len(disk_files), len(disk_files), "完成")
        logger.info("磁盘同步完成: {}", report.summary())
        return report

    @staticmethod
    def _derive_mod_path(mod: Mod, base_path: Path) -> Path:
        """推导 DB 记录对应的磁盘路径（与 _remove_mod_files 同一公式）"""
        if mod.location == ModLocation.REPO:
            return resolve_repo_mod_path(mod, base_path)
        return resolve_game_mod_path(mod, base_path)

    def _apply_mod_update(
        self,
        mod: Mod,
        manifest: ModManifest,
        file_path: Path,
        base_path: Path,
        location: ModLocation,
    ) -> SyncChangeItem | None:
        """按磁盘实际情况原地更新已有记录（保留 notes / installed_at）。

        file_name 沿用代码库约定：存磁盘实际文件名（禁用时含 _disabled 后缀，
        与 set_enabled 行为一致）。

        Returns:
            有变化时返回变更明细，无变化返回 None
        """
        changes: list[str] = []
        file_name = file_path.name
        is_disabled = file_path.name.lower().endswith(".zipmod_disabled")

        if mod.file_name != file_name:
            changes.append(f"文件名 {mod.file_name} → {file_name}")
            mod.file_name = file_name

        if location == ModLocation.REPO:
            rel = file_path.relative_to(base_path).as_posix()
            if mod.repo_relative_path != rel:
                changes.append(f"路径 {mod.repo_relative_path or '(空)'} → {rel}")
                mod.repo_relative_path = rel  # 顺带回填老数据
        else:
            rel = file_path.relative_to(base_path).as_posix()
            if mod.game_relative_path != rel:
                changes.append(f"路径 {mod.game_relative_path or '(空)'} → {rel}")
                mod.game_relative_path = rel  # 顺带回填老数据（F19-6）
            new_status = ModStatus.DISABLED if is_disabled else ModStatus.ENABLED
            if mod.status != new_status:
                old = "禁用" if mod.status == ModStatus.DISABLED else "启用"
                new = "禁用" if new_status == ModStatus.DISABLED else "启用"
                changes.append(f"状态 {old} → {new}")
                mod.status = new_status

        if mod.name != manifest.name:
            changes.append(f"名称 {mod.name} → {manifest.name}")
            mod.name = manifest.name
        if mod.version != manifest.version:
            changes.append(f"版本 {display_version(mod.version)} → {display_version(manifest.version)}")
            mod.version = manifest.version
        if (mod.description or "") != manifest.description:
            changes.append("描述已更新")
            mod.description = manifest.description
        if (mod.website or "") != manifest.website:
            changes.append("网站已更新")
            mod.website = manifest.website

        # author：仅当 manifest 有非空作者且忽略大小写不同才更新（避免大小写抖动）
        if manifest.author and (mod.author or "").strip().lower() != manifest.author.strip().lower():
            changes.append(f"作者 {mod.author or '(空)'} → {manifest.author}")
            mod.author = manifest.author
            mod.author_normalized = manifest.author_normalized

        # manifest 无作者 → DB 的 author 是目录推断值（或空）。
        # 文件被移动到其他子目录后按实际目录重新推断：
        #   GAME 端用 BR-CAT03 护栏（分类段自底向上排除，避免把分类名
        #   写成作者）；REPO 端用单层父目录名。定位本身已由 rel path
        #   优先保障，此处更正主要保证作者分组展示正确。
        # manifest 有作者时不做此更正（manifest 元数据优先于目录名）。
        if not manifest.author and file_path.parent != base_path:
            if location == ModLocation.GAME:
                config = get_config()
                dir_name = infer_author_from_path(
                    file_path, base_path, config.category.category_names
                ) or "Unknown"
            else:
                dir_name = file_path.parent.name
            if (mod.author or "").strip().lower() != dir_name.strip().lower():
                changes.append(
                    f"作者目录 {mod.author or '(空)'} → {dir_name}（按实际位置更正）"
                )
                mod.author = dir_name
                mod.author_normalized = dir_name.strip().lower()

        if not changes:
            return None
        return SyncChangeItem(
            action="updated", guid=mod.guid, name=mod.name,
            file_name=file_name, detail="；".join(changes),
        )

    # ═══════════════════════════════════════════════════════════
    # 批量安装（Batch Install）
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def collect_zipmods(paths: list[Path]) -> list[Path]:
        """
        从给定路径列表中收集所有 .zipmod 文件。

        - 如果 path 是文件且后缀为 .zipmod，直接加入
        - 如果 path 是目录，递归查找所有 .zipmod（含 .zipmod_disabled）

        Returns:
            去重后的 .zipmod 文件路径列表
        """
        result: list[Path] = []
        seen: set[Path] = set()

        for p in paths:
            if p.is_file():
                if p.suffix.lower() in (".zipmod", ".zipmod_disabled"):
                    resolved = p.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        result.append(p)
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f.suffix.lower() in (".zipmod", ".zipmod_disabled"):
                        resolved = f.resolve()
                        if resolved not in seen:
                            seen.add(resolved)
                            result.append(f)

        return result

    def batch_install(
        self,
        zipmod_paths: list[Path],
        location: ModLocation,
        progress_callback: Callable[[int, int, str], None] | None = None,
        category: str | None = None,
    ) -> BatchInstallResult:
        """
        批量安装 / 添加 Mod。

        遇到版本冲突（NEWER / OLDER / UNKNOWN）时不中断，
        记录为 CONFLICT，让用户事后处理；
        遇到同版本重复时记录为 SKIPPED；
        其他错误记录为 FAILED。

        Args:
            zipmod_paths: 要安装的 .zipmod 文件列表
            location: 目标位置（GAME / REPO）
            progress_callback: 可选进度回调 (current, total, file_name)
            category: GAME 端目标分类（F19，REPO 端忽略）

        Returns:
            BatchInstallResult 包含每个文件的处理结果
        """
        batch = BatchInstallResult(total=len(zipmod_paths))
        config = get_config()

        # 验证目标路径
        if location == ModLocation.GAME:
            base_path_str = config.game_mod_path
            path_label = "游戏 Mod 路径"
        else:
            base_path_str = config.repo_path
            path_label = "仓库路径"

        if not base_path_str:
            for fp in zipmod_paths:
                batch.items.append(BatchItemResult(
                    file_path=fp,
                    status=BatchItemStatus.FAILED,
                    message=f"未配置{path_label}，请先在设置中配置",
                ))
                batch.failed_count += 1
            return batch

        base_path = Path(base_path_str)
        if not base_path.exists():
            for fp in zipmod_paths:
                batch.items.append(BatchItemResult(
                    file_path=fp,
                    status=BatchItemStatus.FAILED,
                    message=f"{path_label}不存在：{base_path_str}",
                ))
                batch.failed_count += 1
            return batch

        for idx, fp in enumerate(zipmod_paths):
            if progress_callback:
                progress_callback(idx, len(zipmod_paths), fp.name)

            item = self._batch_install_single(fp, location, base_path, category)
            batch.items.append(item)

            if item.status == BatchItemStatus.SUCCESS:
                batch.success_count += 1
            elif item.status == BatchItemStatus.SKIPPED:
                batch.skipped_count += 1
            elif item.status == BatchItemStatus.CONFLICT:
                batch.conflict_count += 1
            else:
                batch.failed_count += 1

        if progress_callback:
            progress_callback(len(zipmod_paths), len(zipmod_paths), "完成")

        logger.info(
            "批量安装完成: 总={} 成功={} 跳过={} 冲突={} 失败={}",
            batch.total, batch.success_count,
            batch.skipped_count, batch.conflict_count, batch.failed_count,
        )
        return batch

    def _batch_install_single(
        self,
        fp: Path,
        location: ModLocation,
        base_path: Path,
        category: str | None = None,
    ) -> BatchItemResult:
        """批量安装中处理单个文件，不抛异常，结果封装为 BatchItemResult"""
        # 1. 解析 manifest
        try:
            manifest = self.manifest_svc.parse_from_zipmod(fp)
        except ManifestParseError as e:
            logger.warning("批量安装 - 解析失败: {} - {}", fp.name, e)
            return BatchItemResult(
                file_path=fp,
                status=BatchItemStatus.FAILED,
                message=str(e),
            )

        # 2. 重复检测
        dup = self.manifest_svc.check_duplicate(manifest, location, self.session)

        if dup.is_blocked:
            # 同版本已存在，跳过
            logger.debug("批量安装 - 跳过（同版本）: {} {}", manifest.name, display_version(manifest.version))
            return BatchItemResult(
                file_path=fp,
                status=BatchItemStatus.SKIPPED,
                message=f"已存在相同版本：{manifest.name} {display_version(manifest.version)}",
                manifest=manifest,
                duplicate_report=dup,
            )

        if dup.needs_confirmation:
            # 版本不同，记录为冲突，不自动处理
            logger.debug(
                "批量安装 - 版本冲突: {} 已有={} 待装={}",
                manifest.name, display_version(dup.existing_version),
                display_version(manifest.version),
            )
            return BatchItemResult(
                file_path=fp,
                status=BatchItemStatus.CONFLICT,
                message=(
                    f"版本冲突：已有 {display_version(dup.existing_version)}，"
                    f"待添加 {display_version(manifest.version)}"
                ),
                manifest=manifest,
                duplicate_report=dup,
            )

        # 3. 复制文件（GAME 端按 F19 分类布局）
        #    F04-8: 先复制成功、再删旧版本（正常情况不会有旧版本，防御性处理）
        if location == ModLocation.GAME:
            author_dir = self._game_target_dir(base_path, category, manifest.author)
        else:
            author_dir = get_author_dir(base_path, manifest.author)
        try:
            dest_file = safe_copy(
                src=fp,
                dst_dir=author_dir,
                file_name=fp.name,
                version=manifest.version,
            )
        except OSError as e:
            logger.error("批量安装 - 复制失败: {} - {}", fp.name, e)
            return BatchItemResult(
                file_path=fp,
                status=BatchItemStatus.FAILED,
                message=f"文件复制失败：{e}",
                manifest=manifest,
            )

        # 4. 复制成功后移除旧版本（防御性）
        if dup.existing_mod is not None:
            self._remove_mod_files(dup.existing_mod, base_path)
            self.session.delete(dup.existing_mod)
            self.session.flush()

        # 5. 写入数据库
        mod = Mod(
            guid=manifest.guid,
            name=manifest.name,
            version=manifest.version,
            author=_normalize_author(manifest.author),
            author_normalized=manifest.author_normalized,
            description=manifest.description,
            website=manifest.website,
            file_name=dest_file.name,
            location=location,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
            repo_relative_path=(
                compute_repo_relative_path(base_path, dest_file)
                if location == ModLocation.REPO else ""
            ),
            game_relative_path=(
                compute_relative_path(base_path, dest_file)
                if location == ModLocation.GAME else ""
            ),
        )
        self.session.add(mod)

        op_type = "install" if location == ModLocation.GAME else "repo_add"
        loc_label = "game" if location == ModLocation.GAME else "repo"
        self._log_operation(
            operation=op_type,
            mod_name=manifest.name,
            mod_guid=manifest.guid,
            location=loc_label,
            detail=f"批量添加 {display_version(manifest.version)} → {dest_file}",
        )
        self.session.commit()

        logger.debug("批量安装 - 成功: {} {}", manifest.name, display_version(manifest.version))
        return BatchItemResult(
            file_path=fp,
            status=BatchItemStatus.SUCCESS,
            message=f"成功：{manifest.name} {display_version(manifest.version)}",
            manifest=manifest,
        )

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _remove_mod_files(mod: Mod, base_path: str) -> None:
        """删除 Mod 对应的磁盘文件（rel path 优先定位，F19-6）"""
        base = Path(base_path)
        if mod.location == ModLocation.REPO:
            file_path = resolve_repo_mod_path(mod, base)
        else:
            file_path = resolve_game_mod_path(mod, base)
        if file_path.exists():
            delete_file(file_path)

    def _log_operation(
        self,
        operation: str,
        mod_name: str,
        mod_guid: str,
        location: str,
        detail: str = "",
        result: str = "success",
    ) -> None:
        """记录操作日志"""
        log = OperationLog(
            operation=operation,
            mod_name=mod_name,
            mod_guid=mod_guid,
            location=location,
            detail=detail,
            result=result,
        )
        self.session.add(log)
