"""Mod 核心业务服务（F04 / F06 / F08 / F14）

需求文档 v1.3:
  - F04: Mod 安装到游戏（复制 zipmod 到 {游戏路径}/{作者}/）
  - F14: Mod 添加到仓库（复制 zipmod 到 {仓库路径}/{作者}/）
  - F06: 卸载/移除（删除 zipmod 文件）
  - F08: 启用/禁用（修改文件扩展名）

核心设计：
  - 安装 = 复制 .zipmod 到目标目录（不解压！）
  - 按作者名分类创建目录
  - 双重检测（游戏+仓库）防止重复
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session
from loguru import logger

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
)


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


class ModService:
    """Mod 核心业务服务"""

    def __init__(self, db_session: Session):
        self.session = db_session
        self.manifest_svc = ManifestService()

    # ═══════════════════════════════════════════════════════════
    # 安装到游戏（F04）
    # ═══════════════════════════════════════════════════════════

    def install_to_game(
        self,
        zipmod_path: Path,
        confirm_upgrade: bool = False,
        confirm_downgrade: bool = False,
    ) -> InstallResult:
        """
        将 .zipmod 安装到游戏 Mod 路径。

        流程（F04-2）：
          1. 解析 manifest.xml
          2. 重复检测（游戏+仓库）
          3. 目标目录 = {游戏Mod路径}/{作者名}/
          4. 复制 zipmod 文件
          5. 写入数据库

        重复处理：
          - SAME_VERSION: 拒绝
          - NEWER_VERSION: 需要 confirm_upgrade=True
          - OLDER_VERSION: 需要 confirm_downgrade=True
        """
        config = get_config()
        path = Path(zipmod_path)

        # 1. 解析元数据
        try:
            manifest = self.manifest_svc.parse_from_zipmod(path)
            logger.debug("解析 zipmod 成功: name={} version={} guid={}", manifest.name, manifest.version, manifest.guid)
        except ManifestParseError as e:
            logger.warning("zipmod 解析失败: {} - {}", path.name, e)
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
            logger.info("安装被阻止（同版本已存在）: {} v{}", manifest.name, manifest.version)
            return InstallResult(
                success=False,
                message=(
                    f"已安装相同版本的 Mod：{manifest.name} v{manifest.version}\n"
                    f"GUID: {manifest.guid}"
                ),
                duplicate_report=dup,
                manifest=manifest,
            )

        if dup.needs_confirmation:
            if dup.result == DuplicateResult.NEWER_VERSION and not confirm_upgrade:
                logger.info("需要确认升级: {} v{} → v{}", manifest.name, dup.existing_version, manifest.version)
                return InstallResult(
                    success=False,
                    message="NEWER_VERSION_NEEDS_CONFIRM",
                    duplicate_report=dup,
                    manifest=manifest,
                )
            if dup.result == DuplicateResult.OLDER_VERSION and not confirm_downgrade:
                logger.info("需要确认降级: {} v{} → v{}", manifest.name, dup.existing_version, manifest.version)
                return InstallResult(
                    success=False,
                    message="OLDER_VERSION_NEEDS_CONFIRM",
                    duplicate_report=dup,
                    manifest=manifest,
                )

        # 4. 如果有旧版本，先删除
        if dup.existing_mod is not None:
            logger.info("移除旧版本: {} v{} (GUID={})", manifest.name, dup.existing_version, manifest.guid)
            self._remove_mod_files(dup.existing_mod, config.game_mod_path)
            self.session.delete(dup.existing_mod)
            self.session.flush()

        # 5. 复制文件到目标目录
        author_dir = get_author_dir(game_root, manifest.author)
        logger.debug("安装目标目录: {}", author_dir)

        try:
            dest_file = safe_copy(
                src=path,
                dst_dir=author_dir,
                author=manifest.display_author,
                file_name=path.name,
                version=manifest.version,
            )
            logger.info("文件复制成功: {} → {}", path.name, dest_file)
        except OSError as e:
            logger.error("文件复制失败: {} → {} - {}", path.name, author_dir, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
                manifest=manifest,
            )

        # 6. 数据库记录
        mod = Mod(
            guid=manifest.guid,
            name=manifest.name,
            version=manifest.version,
            author=manifest.author,
            author_normalized=manifest.author_normalized,
            description=manifest.description,
            website=manifest.website,
            file_name=dest_file.name,
            location=ModLocation.GAME,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
        )
        self.session.add(mod)

        # 7. 操作日志
        self._log_operation(
            operation="install",
            mod_name=manifest.name,
            mod_guid=manifest.guid,
            location="game",
            detail=f"安装 v{manifest.version} → {dest_file}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"安装成功：{manifest.name} v{manifest.version}",
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
        path = Path(zipmod_path)

        # 1. 解析
        try:
            manifest = self.manifest_svc.parse_from_zipmod(path)
            logger.debug("解析 zipmod 成功(add_to_repo): name={} version={}", manifest.name, manifest.version)
        except ManifestParseError as e:
            logger.warning("zipmod 解析失败(add_to_repo): {} - {}", path.name, e)
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
            logger.info("添加到仓库被阻止（同版本已存在）: {} v{}", manifest.name, manifest.version)
            return InstallResult(
                success=False,
                message=f"仓库中已存在相同版本的 Mod：{manifest.name} v{manifest.version}",
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

        # 4. 移除旧版本
        if dup.existing_mod is not None:
            self._remove_mod_files(dup.existing_mod, config.repo_path)
            self.session.delete(dup.existing_mod)
            self.session.flush()

        # 5. 复制
        author_dir = get_author_dir(repo_root, manifest.author)
        logger.debug("仓库目标目录: {}", author_dir)

        try:
            dest_file = safe_copy(
                src=path,
                dst_dir=author_dir,
                author=manifest.display_author,
                file_name=path.name,
                version=manifest.version,
            )
            logger.info("添加到仓库成功: {} → {}", path.name, dest_file)
        except OSError as e:
            logger.error("添加到仓库文件复制失败: {} - {}", path.name, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
                manifest=manifest,
            )

        # 6. 数据库
        mod = Mod(
            guid=manifest.guid,
            name=manifest.name,
            version=manifest.version,
            author=manifest.author,
            author_normalized=manifest.author_normalized,
            description=manifest.description,
            website=manifest.website,
            file_name=dest_file.name,
            location=ModLocation.REPO,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
        )
        self.session.add(mod)

        self._log_operation(
            operation="repo_add",
            mod_name=manifest.name,
            mod_guid=manifest.guid,
            location="repo",
            detail=f"添加 v{manifest.version} → {dest_file}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"已添加到仓库：{manifest.name} v{manifest.version}",
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
    ) -> InstallResult:
        """
        安装并收藏：先添加到仓库，再安装到游戏（Q-02 确认流程）。
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

        # 再从仓库安装到游戏
        game_result = self.install_to_game(
            zipmod_path,
            confirm_upgrade=confirm_upgrade,
            confirm_downgrade=confirm_downgrade,
        )

        if game_result.success:
            game_result.message = (
                f"安装并收藏成功：{game_result.manifest.name} "
                f"v{game_result.manifest.version}"
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
        base_path = (
            config.game_mod_path if mod.location == ModLocation.GAME
            else config.repo_path
        )

        # 删除文件
        author_dir = get_author_dir(Path(base_path), mod.author)
        file_path = author_dir / mod.current_file_name

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

        # 清理空目录
        if config.clean_empty_dirs:
            clean_empty_dirs(author_dir)
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
        author_dir = get_author_dir(Path(config.game_mod_path), mod.author)

        if enabled:
            # 启用：.zipmod_disabled → .zipmod
            old_path = author_dir / mod.is_disabled_file
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
            old_path = author_dir / mod.file_name
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

    def install_from_repo(self, mod_id: int) -> InstallResult:
        """
        将仓库中的 Mod 安装到游戏。

        用于卡片解析结果中的"一键安装"功能。
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
        if game_dup is not None:
            if game_dup.version == repo_mod.version:
                logger.info("从仓库安装被跳过（游戏已安装同版本）: {} v{}", repo_mod.name, repo_mod.version)
                return InstallResult(
                    success=False,
                    message=f"游戏已安装相同版本：{repo_mod.name} v{repo_mod.version}",
                )
            # 版本不同 → 当作升级处理
            logger.info("从仓库安装 - 移除游戏旧版本: {} v{}", game_dup.name, game_dup.version)
            self._remove_mod_files(game_dup, config.game_mod_path)
            self.session.delete(game_dup)
            self.session.flush()

        # 从仓库复制到游戏
        repo_path = Path(config.repo_path)
        game_path = Path(config.game_mod_path)
        author_dir = get_author_dir(repo_path, repo_mod.author)
        src_file = author_dir / repo_mod.current_file_name

        if not src_file.exists():
            logger.error("从仓库安装失败 - 仓库文件不存在: {}", src_file)
            return InstallResult(
                success=False,
                message=f"仓库中文件不存在：{src_file}",
            )

        game_author_dir = get_author_dir(game_path, repo_mod.author)
        logger.debug("从仓库安装 - 复制: {} → {}", src_file, game_author_dir)
        try:
            dest_file = safe_copy(
                src=src_file,
                dst_dir=game_author_dir,
                author=repo_mod.author or "Unknown",
                file_name=src_file.name,
                version=repo_mod.version,
            )
            logger.info("从仓库安装成功: {} v{} → {}", repo_mod.name, repo_mod.version, dest_file)
        except OSError as e:
            logger.error("从仓库安装文件复制失败: {} - {}", src_file.name, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
            )

        game_mod = Mod(
            guid=repo_mod.guid,
            name=repo_mod.name,
            version=repo_mod.version,
            author=repo_mod.author,
            author_normalized=repo_mod.author_normalized,
            description=repo_mod.description,
            website=repo_mod.website,
            file_name=dest_file.name,
            location=ModLocation.GAME,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
        )
        self.session.add(game_mod)

        self._log_operation(
            operation="install",
            mod_name=repo_mod.name,
            mod_guid=repo_mod.guid,
            location="game",
            detail=f"从仓库安装 v{repo_mod.version}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"已安装到游戏：{repo_mod.name} v{repo_mod.version}",
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
        if repo_dup is not None:
            if repo_dup.version == game_mod.version:
                logger.info("添加到仓库被跳过（仓库已存在同版本）: {} v{}", game_mod.name, game_mod.version)
                return InstallResult(
                    success=False,
                    message=f"仓库中已存在相同版本：{game_mod.name} v{game_mod.version}",
                )
            # 版本不同 → 移除仓库旧版本
            logger.info("添加到仓库 - 移除仓库旧版本: {} v{}", repo_dup.name, repo_dup.version)
            self._remove_mod_files(repo_dup, config.repo_path)
            self.session.delete(repo_dup)
            self.session.flush()

        # 从游戏复制到仓库
        game_path = Path(config.game_mod_path)
        repo_path = Path(config.repo_path)
        author_dir = get_author_dir(game_path, game_mod.author)
        src_file = author_dir / game_mod.current_file_name

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
                author=game_mod.author or "Unknown",
                file_name=src_file.name,
                version=game_mod.version,
            )
            logger.info("添加到仓库成功: {} v{} → {}", game_mod.name, game_mod.version, dest_file)
        except OSError as e:
            logger.error("添加到仓库文件复制失败: {} - {}", src_file.name, e)
            return InstallResult(
                success=False,
                message=f"文件复制失败：{e}",
            )

        repo_mod = Mod(
            guid=game_mod.guid,
            name=game_mod.name,
            version=game_mod.version,
            author=game_mod.author,
            author_normalized=game_mod.author_normalized,
            description=game_mod.description,
            website=game_mod.website,
            file_name=dest_file.name,
            location=ModLocation.REPO,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
        )
        self.session.add(repo_mod)

        self._log_operation(
            operation="repo_add",
            mod_name=game_mod.name,
            mod_guid=game_mod.guid,
            location="repo",
            detail=f"从游戏添加 v{game_mod.version}",
        )

        self.session.commit()

        return InstallResult(
            success=True,
            message=f"已加入仓库：{game_mod.name} v{game_mod.version}",
            mod=repo_mod,
        )

    # ═══════════════════════════════════════════════════════════
    # 查询
    # ═══════════════════════════════════════════════════════════

    def get_mods_by_location(
        self,
        location: ModLocation,
        search: str = "",
    ) -> list[Mod]:
        """查询指定位置的 Mod 列表"""
        query = self.session.query(Mod).filter(Mod.location == location)

        if search:
            pattern = f"%{search}%"
            query = query.filter(
                (Mod.name.like(pattern))
                | (Mod.author.like(pattern))
                | (Mod.guid.like(pattern))
            )

        query = query.order_by(Mod.author_normalized, Mod.name)
        return query.all()

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
        for zipmod_file in base_path.rglob("*.zipmod"):
            if not zipmod_file.is_file():
                continue
            try:
                manifest = self.manifest_svc.parse_from_zipmod(zipmod_file)
            except ManifestParseError:
                continue

            # 检查 author 目录
            parent_dir = zipmod_file.parent
            if parent_dir != base_path:
                # 文件在子目录中，用目录名推断作者
                if not manifest.author:
                    manifest.author = parent_dir.name

            mod = Mod(
                guid=manifest.guid,
                name=manifest.name,
                version=manifest.version,
                author=manifest.author,
                author_normalized=manifest.author_normalized,
                description=manifest.description,
                website=manifest.website,
                file_name=zipmod_file.name,
                location=location,
                status=ModStatus.ENABLED,
                installed_at=datetime.fromtimestamp(
                    zipmod_file.stat().st_mtime
                ),
            )
            self.session.add(mod)
            count += 1

        self.session.commit()
        return count

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

            item = self._batch_install_single(fp, location, base_path)
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
            logger.debug("批量安装 - 跳过（同版本）: {} v{}", manifest.name, manifest.version)
            return BatchItemResult(
                file_path=fp,
                status=BatchItemStatus.SKIPPED,
                message=f"已存在相同版本：{manifest.name} v{manifest.version}",
                manifest=manifest,
                duplicate_report=dup,
            )

        if dup.needs_confirmation:
            # 版本不同，记录为冲突，不自动处理
            logger.debug(
                "批量安装 - 版本冲突: {} 已有=v{} 待装=v{}",
                manifest.name, dup.existing_version, manifest.version,
            )
            return BatchItemResult(
                file_path=fp,
                status=BatchItemStatus.CONFLICT,
                message=(
                    f"版本冲突：已有 v{dup.existing_version}，"
                    f"待添加 v{manifest.version}"
                ),
                manifest=manifest,
                duplicate_report=dup,
            )

        # 3. 移除旧版本（正常情况不会到这里，但防御性处理）
        if dup.existing_mod is not None:
            config = get_config()
            base_str = config.game_mod_path if location == ModLocation.GAME else config.repo_path
            self._remove_mod_files(dup.existing_mod, base_str)
            self.session.delete(dup.existing_mod)
            self.session.flush()

        # 4. 复制文件
        author_dir = get_author_dir(base_path, manifest.author)
        try:
            dest_file = safe_copy(
                src=fp,
                dst_dir=author_dir,
                author=manifest.display_author,
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

        # 5. 写入数据库
        mod = Mod(
            guid=manifest.guid,
            name=manifest.name,
            version=manifest.version,
            author=manifest.author,
            author_normalized=manifest.author_normalized,
            description=manifest.description,
            website=manifest.website,
            file_name=dest_file.name,
            location=location,
            status=ModStatus.ENABLED,
            installed_at=datetime.now(),
        )
        self.session.add(mod)

        op_type = "install" if location == ModLocation.GAME else "repo_add"
        loc_label = "game" if location == ModLocation.GAME else "repo"
        self._log_operation(
            operation=op_type,
            mod_name=manifest.name,
            mod_guid=manifest.guid,
            location=loc_label,
            detail=f"批量添加 v{manifest.version} → {dest_file}",
        )
        self.session.commit()

        logger.debug("批量安装 - 成功: {} v{}", manifest.name, manifest.version)
        return BatchItemResult(
            file_path=fp,
            status=BatchItemStatus.SUCCESS,
            message=f"成功：{manifest.name} v{manifest.version}",
            manifest=manifest,
        )

    # ═══════════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _remove_mod_files(mod: Mod, base_path: str) -> None:
        """删除 Mod 对应的磁盘文件"""
        author_dir = get_author_dir(Path(base_path), mod.author)
        file_path = author_dir / mod.current_file_name
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
