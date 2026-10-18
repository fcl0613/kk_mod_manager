"""ModService 业务方法测试（install / uninstall / set_enabled / 查询 / 批量）。

sync_from_disk / refresh_from_disk 已由 tests/sync_service_test.py 手动脚本覆盖，
此处不重复；仅测 SyncReport 数据类。
"""

import os
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app.config as cfg
from app.config import AppConfig
from app.models.mod import Mod, ModLocation, ModStatus, OperationLog
from app.services.mod_service import (
    BatchItemStatus,
    ModService,
    SyncChangeItem,
    SyncReport,
    _normalize_author,
)
from tests.conftest import make_bad_file, make_zipmod


def set_config(**kwargs) -> None:
    """测试中途改写配置单例（未给的字段沿用当前值；teardown 由 autouse fixture 兜底恢复）"""
    cur = cfg.get_config()
    overrides = dict(
        game_mod_path=cur.game_mod_path,
        repo_path=cur.repo_path,
    )
    overrides.update(kwargs)
    cfg._config_instance = AppConfig(**overrides)


def seed_mod(session, guid, name, author, version="1.0.0",
             location=ModLocation.GAME, status=ModStatus.ENABLED,
             file_name=None, repo_relative_path="") -> Mod:
    """直接插一行 Mod（查询类测试用）"""
    mod = Mod(
        guid=guid, name=name, version=version, author=author,
        author_normalized=(author or "").strip().lower() or "unknown",
        file_name=file_name or f"{name}.zipmod", location=location,
        status=status, repo_relative_path=repo_relative_path,
    )
    session.add(mod)
    session.commit()
    return mod


def files_under(root: Path) -> set[str]:
    return {f.relative_to(root).as_posix() for f in root.rglob("*") if f.is_file()}


def _copy_boom(src, dst_dir, file_name, version):
    """模拟复制失败（磁盘满/权限等），F04-8 防丢测试用"""
    raise OSError("simulated copy failure")


# ═══════════════════════════════════════════════════════════
# install_to_game
# ═══════════════════════════════════════════════════════════

class TestInstallToGame:
    def test_install_success(self, svc, dirs, db_session):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", version="2.1.0",
                          author="Alice")
        res = svc.install_to_game(src)

        assert res.success
        assert res.message == "安装成功：ModA v2.1.0"
        assert (dirs.game / "Alice" / "ModA.zipmod").is_file()
        assert res.mod is not None and res.mod.guid == "guid.a"

        mod = svc.get_mod_by_id(res.mod.id)
        assert mod.version == "2.1.0"
        assert mod.author == "Alice"
        assert mod.author_normalized == "alice"
        assert mod.file_name == "ModA.zipmod"
        assert mod.location is ModLocation.GAME
        assert mod.status is ModStatus.ENABLED
        assert db_session.query(OperationLog).filter_by(operation="install").count() == 1

    def test_install_message_with_v_prefixed_version(self, svc, dirs):
        """manifest 版本自带 v 前缀 → 消息展示不出现 "vv"（display_version 归一）"""
        src = make_zipmod(dirs.root / "src", "ModA", "guid.v", version="v2.1.0",
                          author="Alice")
        res = svc.install_to_game(src)

        assert res.success
        assert res.message == "安装成功：ModA v2.1.0"
        assert res.mod.version == "v2.1.0"  # 落库保持 manifest 原值

    def test_reuses_author_dir_case_insensitive(self, svc, dirs):
        (dirs.game / "ALICE").mkdir()
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src)

        assert res.success
        assert (dirs.game / "ALICE" / "ModA.zipmod").is_file()
        # Windows 文件系统大小写不敏感，exists() 无法区分 → 枚举实际目录名
        assert [d.name for d in dirs.game.iterdir() if d.is_dir()] == ["ALICE"]

    def test_no_author_lands_in_unknown(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author=None)
        res = svc.install_to_game(src)

        assert res.success
        assert (dirs.game / "Unknown" / "ModA.zipmod").is_file()
        assert res.mod.author == "Unknown"
        assert res.mod.author_normalized == "unknown"

    def test_parse_failure_rejected(self, svc, dirs):
        bad = make_bad_file(dirs.root / "src", "bad.zipmod")
        res = svc.install_to_game(bad)

        assert not res.success
        assert "不是有效的 ZIP" in res.message
        assert files_under(dirs.game) == set()
        assert svc.get_mods_count(ModLocation.GAME) == 0

    def test_path_not_configured(self, svc):
        set_config(game_mod_path="")
        src = make_zipmod(Path(cfg.get_config().repo_path) / "src",
                          "ModA", "guid.a")
        res = svc.install_to_game(src)
        assert not res.success
        assert res.message == "请先在设置中配置游戏 Mod 路径"

    def test_path_missing(self, svc, dirs):
        set_config(game_mod_path=str(dirs.root / "not_exist"))
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a")
        res = svc.install_to_game(src)
        assert not res.success
        assert "游戏 Mod 路径不存在" in res.message

    def test_same_version_blocked(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        assert svc.install_to_game(src).success

        res = svc.install_to_game(src)
        assert not res.success
        assert "已安装相同版本的 Mod" in res.message
        assert res.duplicate_report is not None and res.duplicate_report.is_blocked
        # 磁盘与库都只有一份
        assert files_under(dirs.game) == {"Alice/ModA.zipmod"}
        assert svc.get_mods_count(ModLocation.GAME) == 1

    def test_newer_version_needs_confirm(self, svc, dirs):
        make_zipmod(dirs.root / "v1", "ModA", "guid.a", version="1.0.0", author="Alice")
        v1 = make_zipmod(dirs.root / "v1", "ModA", "guid.a", version="1.0.0", author="Alice")
        assert svc.install_to_game(v1).success
        v2 = make_zipmod(dirs.root / "v2", "ModA", "guid.a", version="2.0.0", author="Alice")

        res = svc.install_to_game(v2)
        assert not res.success
        assert res.message == "NEWER_VERSION_NEEDS_CONFIRM"
        # 未确认时旧版本原样保留
        assert svc.get_game_mod_by_guid("guid.a").version == "1.0.0"
        assert files_under(dirs.game) == {"Alice/ModA.zipmod"}

    def test_newer_version_with_confirm_replaces(self, svc, dirs):
        v1 = make_zipmod(dirs.root / "v1", "ModA", "guid.a", version="1.0.0", author="Alice")
        assert svc.install_to_game(v1).success
        v2 = make_zipmod(dirs.root / "v2", "ModA", "guid.a", version="2.0.0", author="Alice")

        res = svc.install_to_game(v2, confirm_upgrade=True)
        assert res.success
        assert svc.get_game_mod_by_guid("guid.a").version == "2.0.0"
        # F04-8 先复制后删旧 → 新文件按 BR-F05 带版本号命名，旧文件已删仍只一份
        assert files_under(dirs.game) == {"Alice/ModAv2.0.0.zipmod"}
        assert svc.get_mods_count(ModLocation.GAME) == 1

    def test_upgrade_copy_failure_keeps_old_mod(self, svc, dirs, monkeypatch):
        """F04-8: 升级时复制失败 → 旧 Mod 的文件与数据库记录必须完整保留"""
        v1 = make_zipmod(dirs.root / "v1", "ModA", "guid.a", version="1.0.0", author="Alice")
        assert svc.install_to_game(v1).success
        v2 = make_zipmod(dirs.root / "v2", "ModA", "guid.a", version="2.0.0", author="Alice")

        monkeypatch.setattr("app.services.mod_service.safe_copy", _copy_boom)
        res = svc.install_to_game(v2, confirm_upgrade=True)

        assert not res.success
        assert "文件复制失败" in res.message
        # 旧文件未删、旧记录未动
        assert files_under(dirs.game) == {"Alice/ModA.zipmod"}
        assert svc.get_game_mod_by_guid("guid.a").version == "1.0.0"
        assert svc.get_mods_count(ModLocation.GAME) == 1

    def test_older_version_needs_confirm(self, svc, dirs):
        v2 = make_zipmod(dirs.root / "v2", "ModA", "guid.a", version="2.0.0", author="Alice")
        assert svc.install_to_game(v2).success
        v1 = make_zipmod(dirs.root / "v1", "ModA", "guid.a", version="1.0.0", author="Alice")

        assert svc.install_to_game(v1).message == "OLDER_VERSION_NEEDS_CONFIRM"
        assert svc.get_game_mod_by_guid("guid.a").version == "2.0.0"

        res = svc.install_to_game(v1, confirm_downgrade=True)
        assert res.success
        assert svc.get_game_mod_by_guid("guid.a").version == "1.0.0"

    def test_same_filename_other_guid_renames(self, svc, dirs):
        first = make_zipmod(dirs.root / "s1", "SameName", "guid.a", version="1.0.0",
                            author="Alice")
        second = make_zipmod(dirs.root / "s2", "SameName", "guid.b", version="2.0.0",
                             author="Alice")
        assert svc.install_to_game(first).success
        res = svc.install_to_game(second)

        assert res.success
        assert files_under(dirs.game) == {"Alice/SameName.zipmod",
                                          "Alice/SameNamev2.0.0.zipmod"}
        assert svc.get_mods_count(ModLocation.GAME) == 2


# ═══════════════════════════════════════════════════════════
# 分类目录安装（F19）
# ═══════════════════════════════════════════════════════════

class TestCategoryInstall:
    def test_install_with_category_lands_three_segment(self, svc, dirs):
        """F19-1：传分类 → {分类}/{作者}/ 三段式 + game_relative_path 落库"""
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src, category="ForStudioScenesLoading")

        assert res.success
        assert (dirs.game / "ForStudioScenesLoading" / "Alice" / "ModA.zipmod").is_file()
        assert res.mod.game_relative_path == "ForStudioScenesLoading/Alice/ModA.zipmod"

    def test_install_without_category_stays_flat(self, svc, dirs):
        """category=None → 旧公式平铺（兼容）；rel path 仍持久化（F19-6）"""
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src)

        assert res.success
        assert files_under(dirs.game) == {"Alice/ModA.zipmod"}
        assert res.mod.game_relative_path == "Alice/ModA.zipmod"

    def test_category_ignored_when_toggle_off(self, svc, dirs):
        """F19-5：开关关闭 → 传了 category 也回退旧公式"""
        set_config()
        cfg.get_config().category.game_category_enabled = False
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src, category="ForStudioScenesLoading")

        assert res.success
        assert files_under(dirs.game) == {"Alice/ModA.zipmod"}
        assert res.mod.game_relative_path == "Alice/ModA.zipmod"

    def test_category_dir_reuse_case_insensitive(self, svc, dirs):
        """BR-CAT01：分类目录复用磁盘已有大小写拼写"""
        (dirs.game / "FORSTUDIOSCENESLOADING").mkdir()
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src, category="ForStudioScenesLoading")

        assert res.success
        assert (dirs.game / "FORSTUDIOSCENESLOADING" / "Alice" / "ModA.zipmod").is_file()
        assert res.mod.game_relative_path == (
            "FORSTUDIOSCENESLOADING/Alice/ModA.zipmod"
        )

    def test_uninstall_keeps_category_dir(self, svc, dirs):
        """BR-CAT06：分类目录清空也不删；其下作者目录正常清理"""
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src, category="MyMods")
        assert res.success

        r = svc.uninstall(res.mod.id)
        assert r.success
        assert not (dirs.game / "MyMods" / "Alice" / "ModA.zipmod").exists()
        assert not (dirs.game / "MyMods" / "Alice").exists()
        assert (dirs.game / "MyMods").is_dir()

    def test_set_enabled_renames_via_rel_path(self, svc, dirs):
        """F19-6：启停改名经 rel path 定位，双向可用"""
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        res = svc.install_to_game(src, category="ForCharactersLoading")
        assert res.success

        r1 = svc.set_enabled(res.mod.id, False)
        assert r1.success
        assert (dirs.game / "ForCharactersLoading" / "Alice" / "ModA.zipmod_disabled").is_file()

        r2 = svc.set_enabled(res.mod.id, True)
        assert r2.success
        assert (dirs.game / "ForCharactersLoading" / "Alice" / "ModA.zipmod").is_file()

    def test_install_from_repo_with_category(self, svc, dirs):
        """F19-10：从仓库安装（卡片页入口）走分类路由"""
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        assert svc.add_to_repo(src).success
        repo_mod = svc.get_repo_mod_by_guid("guid.a")

        res = svc.install_from_repo(repo_mod.id, category="ForStudioScenesLoading")
        assert res.success
        assert (dirs.game / "ForStudioScenesLoading" / "Alice" / "ModA.zipmod").is_file()
        assert res.mod.game_relative_path == "ForStudioScenesLoading/Alice/ModA.zipmod"

    def test_batch_install_game_with_category(self, svc, dirs):
        a = make_zipmod(dirs.root / "s", "ModA", "guid.a", author="Alice")
        b = make_zipmod(dirs.root / "s", "ModB", "guid.b", author="Bob")

        result = svc.batch_install([a, b], ModLocation.GAME, category="MyMods")
        assert result.success_count == 2
        assert (dirs.game / "MyMods" / "Alice" / "ModA.zipmod").is_file()
        assert (dirs.game / "MyMods" / "Bob" / "ModB.zipmod").is_file()
        assert svc.get_game_mod_by_guid("guid.a").game_relative_path == (
            "MyMods/Alice/ModA.zipmod"
        )

    def test_legacy_row_without_rel_path_falls_back(self, svc, dirs, db_session):
        """F19-6 老数据：game_relative_path 为空 → 回退作者目录公式定位"""
        mod = seed_mod(db_session, "guid.old", "OldMod", "Alice")
        make_zipmod(dirs.game / "Alice", "OldMod", "guid.old", author="Alice")

        r = svc.set_enabled(mod.id, False)
        assert r.success
        assert (dirs.game / "Alice" / "OldMod.zipmod_disabled").is_file()

        r2 = svc.uninstall(mod.id)
        assert r2.success
        assert files_under(dirs.game) == set()


# ═══════════════════════════════════════════════════════════
# add_to_repo / install_and_collect
# ═══════════════════════════════════════════════════════════

class TestAddToRepo:
    def test_add_to_repo_success_sets_relative_path(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "RepoMod", "guid.r", author="Xavier")
        res = svc.add_to_repo(src)

        assert res.success
        assert res.message == "已添加到仓库：RepoMod v1.0.0"
        assert (dirs.repo / "Xavier" / "RepoMod.zipmod").is_file()
        mod = svc.get_repo_mod_by_guid("guid.r")
        assert mod.repo_relative_path == "Xavier/RepoMod.zipmod"
        assert mod.status is ModStatus.ENABLED
        assert db_op_count(svc, "repo_add") == 1

    def test_same_version_blocked(self, svc):
        src = make_zipmod(Path(cfg.get_config().repo_path) / "src",
                          "RepoMod", "guid.r")
        assert svc.add_to_repo(src).success
        res = svc.add_to_repo(src)
        assert not res.success
        assert "仓库中已存在相同版本的 Mod" in res.message

    def test_path_not_configured(self, svc):
        set_config(repo_path="")
        src = make_zipmod(Path(cfg.get_config().game_mod_path) / "src",
                          "RepoMod", "guid.r")
        res = svc.add_to_repo(src)
        assert not res.success
        assert res.message == "请先在设置中配置本地 Mod 仓库路径"

    def test_path_missing(self, svc, dirs):
        set_config(repo_path=str(dirs.root / "not_exist"))
        src = make_zipmod(dirs.root / "src", "RepoMod", "guid.r")
        res = svc.add_to_repo(src)
        assert not res.success
        assert "仓库路径不存在" in res.message


class TestInstallAndCollect:
    def test_success_installs_both_locations(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModC", "guid.c", author="Cara")
        res = svc.install_and_collect(src)

        assert res.success
        assert res.message.startswith("安装并收藏成功")
        assert (dirs.game / "Cara" / "ModC.zipmod").is_file()
        assert (dirs.repo / "Cara" / "ModC.zipmod").is_file()
        assert svc.get_game_mod_by_guid("guid.c") is not None
        assert svc.get_repo_mod_by_guid("guid.c") is not None

    def test_repo_failure_short_circuits(self, svc, dirs):
        set_config(repo_path="")
        src = make_zipmod(dirs.root / "src", "ModC", "guid.c")
        res = svc.install_and_collect(src)

        assert not res.success
        assert "仓库" in res.message
        assert files_under(dirs.game) == set()  # 游戏端不受影响
        assert svc.get_mods_count(ModLocation.GAME) == 0


# ═══════════════════════════════════════════════════════════
# uninstall
# ═══════════════════════════════════════════════════════════

class TestUninstall:
    def test_game_uninstall_removes_file_row_and_dir(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        res = svc.uninstall(mod.id)
        assert res.success
        assert "卸载成功" in res.message
        assert files_under(dirs.game) == set()
        assert not (dirs.game / "Alice").exists()  # 空作者目录被清理
        assert svc.get_mod_by_id(mod.id) is None
        assert db_op_count(svc, "uninstall") == 1

    def test_keeps_dir_when_clean_disabled(self, svc, dirs):
        set_config(clean_empty_dirs=False)
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        assert svc.uninstall(mod.id).success
        assert (dirs.game / "Alice").is_dir()  # 目录保留

    def test_nonexistent_id(self, svc):
        res = svc.uninstall(9999)
        assert not res.success
        assert res.message == "Mod 不存在：ID=9999"

    def test_repo_uninstall_uses_relative_path(self, svc, dirs):
        target = dirs.repo / "Xavier" / "Sub" / "ModR.zipmod"
        target.parent.mkdir(parents=True)
        make_zipmod(target.parent, "ModR", "guid.r", author="Xavier")
        mod = seed_mod(svc.session, "guid.r", "ModR", "Xavier",
                       location=ModLocation.REPO, file_name="ModR.zipmod",
                       repo_relative_path="Xavier/Sub/ModR.zipmod")

        res = svc.uninstall(mod.id)
        assert res.success
        assert "移除成功" in res.message
        assert not target.exists()
        assert svc.get_repo_mod_by_guid("guid.r") is None

    def test_uninstall_disabled_mod_file(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod
        assert svc.set_enabled(mod.id, False).success

        assert svc.uninstall(mod.id).success
        assert not (dirs.game / "Alice" / "ModA.zipmod_disabled").exists()


def db_op_count(svc, operation: str) -> int:
    return svc.session.query(OperationLog).filter_by(operation=operation).count()


# ═══════════════════════════════════════════════════════════
# set_enabled
# ═══════════════════════════════════════════════════════════

class TestSetEnabled:
    def test_disable_then_enable_round_trip(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        off = svc.set_enabled(mod.id, False)
        assert off.success
        assert "禁用成功" in off.message
        assert (dirs.game / "Alice" / "ModA.zipmod_disabled").is_file()
        assert not (dirs.game / "Alice" / "ModA.zipmod").exists()
        mod = svc.get_mod_by_id(mod.id)
        assert mod.status is ModStatus.DISABLED
        assert mod.file_name == "ModA.zipmod_disabled"
        assert db_op_count(svc, "disable") == 1

        on = svc.set_enabled(mod.id, True)
        assert on.success
        assert "启用成功" in on.message
        assert (dirs.game / "Alice" / "ModA.zipmod").is_file()
        mod = svc.get_mod_by_id(mod.id)
        assert mod.status is ModStatus.ENABLED
        assert mod.file_name == "ModA.zipmod"

    def test_repo_mod_rejected(self, svc):
        mod = seed_mod(svc.session, "guid.r", "RepoMod", "Xavier",
                       location=ModLocation.REPO)
        res = svc.set_enabled(mod.id, False)
        assert not res.success
        assert res.message == "仅游戏中的 Mod 支持启用/禁用操作"

    def test_enable_already_enabled_noop(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        res = svc.set_enabled(mod.id, True)
        assert res.success
        assert res.message == "Mod 已经是启用状态"
        assert (dirs.game / "Alice" / "ModA.zipmod").is_file()

    def test_disable_fails_when_file_missing(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod
        (dirs.game / "Alice" / "ModA.zipmod").unlink()

        res = svc.set_enabled(mod.id, False)
        assert not res.success
        assert "禁用失败：文件不存在" in res.message


# ═══════════════════════════════════════════════════════════
# install_from_repo / add_game_mod_to_repo
# ═══════════════════════════════════════════════════════════

class TestInstallFromRepo:
    def test_success_copies_to_game(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "RepoMod", "guid.r", author="Xavier")
        repo_mod = svc.add_to_repo(src).mod

        res = svc.install_from_repo(repo_mod.id)
        assert res.success
        assert "已安装到游戏" in res.message
        assert (dirs.game / "Xavier" / "RepoMod.zipmod").is_file()
        game_mod = svc.get_game_mod_by_guid("guid.r")
        assert game_mod is not None and game_mod.version == "1.0.0"

    def test_same_version_in_game_skips(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "RepoMod", "guid.r", author="Xavier")
        repo_mod = svc.add_to_repo(src).mod
        assert svc.install_from_repo(repo_mod.id).success

        res = svc.install_from_repo(repo_mod.id)
        assert not res.success
        assert "游戏已安装相同版本" in res.message
        assert files_under(dirs.game) == {"Xavier/RepoMod.zipmod"}  # 不重复复制

    def test_different_version_replaces(self, svc, dirs):
        v1 = make_zipmod(dirs.root / "v1", "RepoMod", "guid.r", version="1.0.0",
                         author="Xavier")
        repo_mod = svc.add_to_repo(v1).mod
        assert svc.install_from_repo(repo_mod.id).success

        # 仓库升级到 v2 后再安装 → 游戏旧版本被替换
        v2 = make_zipmod(dirs.root / "v2", "RepoMod", "guid.r", version="2.0.0",
                         author="Xavier")
        new_repo_mod = svc.add_to_repo(v2, confirm_upgrade=True).mod
        res = svc.install_from_repo(new_repo_mod.id)

        assert res.success
        assert svc.get_game_mod_by_guid("guid.r").version == "2.0.0"
        # F04-8 先复制后删旧 → 升级文件按 BR-F05 带版本号命名
        assert files_under(dirs.game) == {"Xavier/RepoModv2.0.0.zipmod"}

    def test_upgrade_copy_failure_keeps_game_old(self, svc, dirs, monkeypatch):
        """F04-8: 从仓库升级时复制失败 → 游戏旧 Mod 完整保留"""
        v1 = make_zipmod(dirs.root / "v1", "RepoMod", "guid.r", version="1.0.0",
                         author="Xavier")
        repo_mod = svc.add_to_repo(v1).mod
        assert svc.install_from_repo(repo_mod.id).success
        v2 = make_zipmod(dirs.root / "v2", "RepoMod", "guid.r", version="2.0.0",
                         author="Xavier")
        new_repo_mod = svc.add_to_repo(v2, confirm_upgrade=True).mod

        monkeypatch.setattr("app.services.mod_service.safe_copy", _copy_boom)
        res = svc.install_from_repo(new_repo_mod.id)

        assert not res.success
        assert "文件复制失败" in res.message
        assert files_under(dirs.game) == {"Xavier/RepoMod.zipmod"}  # 游戏旧版还在
        assert svc.get_game_mod_by_guid("guid.r").version == "1.0.0"

    def test_game_mod_id_rejected(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        game_mod = svc.install_to_game(src).mod

        res = svc.install_from_repo(game_mod.id)
        assert not res.success
        assert res.message == "只能从仓库安装 Mod 到游戏"

    def test_repo_file_missing(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "RepoMod", "guid.r", author="Xavier")
        repo_mod = svc.add_to_repo(src).mod
        (dirs.repo / "Xavier" / "RepoMod.zipmod").unlink()

        res = svc.install_from_repo(repo_mod.id)
        assert not res.success
        assert "仓库中文件不存在" in res.message

    def test_upgrade_preserves_disabled_status(self, svc, dirs, db_session):
        """F24-6: 升级保留游戏行禁用状态（状态与磁盘扩展名同步修改）"""
        v1 = make_zipmod(dirs.root / "v1", "RepoMod", "guid.r", version="1.0.0",
                         author="Xavier")
        repo_mod = svc.add_to_repo(v1).mod
        game_mod = svc.install_from_repo(repo_mod.id).mod
        assert svc.set_enabled(game_mod.id, enabled=False).success  # 禁用旧版

        v2 = make_zipmod(dirs.root / "v2", "RepoMod", "guid.r", version="2.0.0",
                         author="Xavier")
        new_repo_mod = svc.add_to_repo(v2, confirm_upgrade=True).mod
        res = svc.install_from_repo(new_repo_mod.id)

        assert res.success
        game_now = svc.get_game_mod_by_guid("guid.r")
        assert game_now.status == ModStatus.DISABLED
        # 磁盘扩展名同步为 _disabled（否则下次同步会按后缀把状态改回启用）
        assert files_under(dirs.game) == {"Xavier/RepoModv2.0.0.zipmod_disabled"}
        log = (db_session.query(OperationLog)
               .filter(OperationLog.operation == "install")
               .order_by(OperationLog.id.desc()).first())
        assert "保持禁用" in log.detail

    def test_disabled_rename_failure_falls_back_enabled(self, svc, dirs, monkeypatch):
        """F24-6: _disabled 改名失败 → 降级为启用态，不阻断升级本身"""
        v1 = make_zipmod(dirs.root / "v1", "RepoMod", "guid.r", version="1.0.0",
                         author="Xavier")
        repo_mod = svc.add_to_repo(v1).mod
        game_mod = svc.install_from_repo(repo_mod.id).mod
        assert svc.set_enabled(game_mod.id, enabled=False).success
        v2 = make_zipmod(dirs.root / "v2", "RepoMod", "guid.r", version="2.0.0",
                         author="Xavier")
        new_repo_mod = svc.add_to_repo(v2, confirm_upgrade=True).mod

        # set_enabled 已完成（用了真 rename），此刻起让 rename_extension 失败
        monkeypatch.setattr(
            "app.services.mod_service.rename_extension",
            lambda path, old_suffix, new_suffix: None,
        )
        res = svc.install_from_repo(new_repo_mod.id)

        assert res.success
        game_now = svc.get_game_mod_by_guid("guid.r")
        assert game_now.status == ModStatus.ENABLED
        assert files_under(dirs.game) == {"Xavier/RepoModv2.0.0.zipmod"}


# ═══════════════════════════════════════════════════════════
# 仓库版本巡检 scan_repo_versions（F24-1）
# ═══════════════════════════════════════════════════════════

class TestScanRepoVersions:
    def test_repo_newer_listed_as_upgradable(self, svc, db_session):
        g = seed_mod(db_session, "guid.g", "ModG", "Makise", version="1.0.0")
        r = seed_mod(db_session, "guid.g", "ModG", "Makise", version="2.0.0",
                     location=ModLocation.REPO)

        report = svc.scan_repo_versions()

        assert len(report.repo_newer) == 1 and not report.game_newer
        item = report.repo_newer[0]
        assert item.guid == "guid.g"
        assert item.game_version == "1.0.0" and item.repo_version == "2.0.0"
        assert item.game_mod_id == g.id and item.repo_mod_id == r.id
        assert item.name == "ModG" and item.author == "Makise"
        assert item.category is None  # 未持久化 game_relative_path → 平铺
        assert report.scanned_game == 1 and report.scanned_repo == 1

    def test_game_newer_listed_readonly(self, svc, db_session):
        seed_mod(db_session, "guid.g", "ModG", "Makise", version="3.0.0")
        seed_mod(db_session, "guid.g", "ModG", "Makise", version="2.0.0",
                 location=ModLocation.REPO)

        report = svc.scan_repo_versions()

        assert len(report.game_newer) == 1 and not report.repo_newer

    def test_equal_versions_not_listed(self, svc, db_session):
        seed_mod(db_session, "guid.a", "ModA", "Makise", version="1.0.0")
        seed_mod(db_session, "guid.a", "ModA", "Makise", version="1.0.0",
                 location=ModLocation.REPO)
        # "1.0" vs "v1.0"：原始串不等但解析后相等 → 同样不列
        seed_mod(db_session, "guid.b", "ModB", "Makise", version="1.0")
        seed_mod(db_session, "guid.b", "ModB", "Makise", version="v1.0",
                 location=ModLocation.REPO)

        report = svc.scan_repo_versions()

        assert not report.repo_newer and not report.game_newer
        assert report.skipped_unparseable == 0

    def test_invalid_version_excluded_br_v03(self, svc, db_session):
        """BR-V03：任一侧版本不可解析 → 配对整体出局，不断言新旧"""
        seed_mod(db_session, "guid.x", "ModX", "Makise", version="1.0.0")
        seed_mod(db_session, "guid.x", "ModX", "Makise", version="not a version!",
                 location=ModLocation.REPO)
        seed_mod(db_session, "guid.y", "ModY", "Makise", version="still bad!")
        seed_mod(db_session, "guid.y", "ModY", "Makise", version="1.0.0",
                 location=ModLocation.REPO)

        report = svc.scan_repo_versions()

        assert not report.repo_newer and not report.game_newer
        assert report.skipped_unparseable == 2

    def test_guid_single_sided_excluded(self, svc, db_session):
        """GUID 仅单侧存在 → 不是版本差异，不参与"""
        seed_mod(db_session, "guid.only_game", "A", "Makise", version="1.0.0")
        seed_mod(db_session, "guid.only_repo", "B", "Makise", version="2.0.0",
                 location=ModLocation.REPO)

        report = svc.scan_repo_versions()

        assert not report.repo_newer and not report.game_newer
        assert report.scanned_game == 1 and report.scanned_repo == 1

    def test_derives_category_from_game_relpath(self, svc, db_session):
        seed_mod(db_session, "guid.hit", "Hit", "Makise", version="1.0.0",
                 file_name="Hit.zipmod")
        seed_mod(db_session, "guid.hit", "Hit", "Makise", version="2.0.0",
                 location=ModLocation.REPO)
        g_hit = db_session.query(Mod).filter_by(guid="guid.hit", location=ModLocation.GAME).one()
        g_hit.game_relative_path = "ForCharactersLoading/Makise/Hit.zipmod"

        seed_mod(db_session, "guid.case", "Case", "Makise", version="1.0.0",
                 file_name="Case.zipmod")
        seed_mod(db_session, "guid.case", "Case", "Makise", version="2.0.0",
                 location=ModLocation.REPO)
        g_case = db_session.query(Mod).filter_by(guid="guid.case", location=ModLocation.GAME).one()
        g_case.game_relative_path = "forcharactersloading/Makise/Case.zipmod"  # 大小写不一致

        seed_mod(db_session, "guid.flat", "Flat", "Makise", version="1.0.0")
        seed_mod(db_session, "guid.flat", "Flat", "Makise", version="2.0.0",
                 location=ModLocation.REPO)
        db_session.commit()

        report = svc.scan_repo_versions()
        by_guid = {i.guid: i for i in report.repo_newer}

        assert by_guid["guid.hit"].category == "ForCharactersLoading"
        assert by_guid["guid.case"].category == "ForCharactersLoading"  # 规范名
        assert by_guid["guid.flat"].category is None                    # 平铺老布局

    def test_scan_writes_aggregate_log_only_when_diff(self, svc, db_session):
        seed_mod(db_session, "guid.g", "ModG", "Makise", version="1.0.0")
        r = seed_mod(db_session, "guid.g", "ModG", "Makise", version="2.0.0",
                     location=ModLocation.REPO)

        svc.scan_repo_versions()
        assert db_op_count(svc, "upgrade") == 1
        log = svc.session.query(OperationLog).filter_by(operation="upgrade").one()
        assert log.mod_name == "(仓库版本巡检)"
        assert "仓库更新 1 项" in log.detail

        # 消除差异后再巡检 → 不再新增聚合日志
        r.version = "1.0.0"
        db_session.commit()
        svc.scan_repo_versions()
        assert db_op_count(svc, "upgrade") == 1


# ═══════════════════════════════════════════════════════════
# 一键升级 batch_upgrade_from_repo（F24-2/4/5）
# ═══════════════════════════════════════════════════════════

def _seed_upgradable_pair(svc, dirs, tag, guid, name, author,
                          v1="1.0.0", v2="2.0.0", category=None):
    """造一对可升级数据：游戏装 v1 → 仓库升 v2，返回新 REPO 行"""
    f1 = make_zipmod(dirs.root / f"{tag}-v1", name, guid, version=v1, author=author)
    repo1 = svc.add_to_repo(f1).mod
    assert svc.install_from_repo(repo1.id, category=category).success
    f2 = make_zipmod(dirs.root / f"{tag}-v2", name, guid, version=v2, author=author)
    repo2 = svc.add_to_repo(f2, confirm_upgrade=True).mod
    return repo2


class TestBatchUpgradeFromRepo:
    def test_success_path_replaces_game_file(self, svc, dirs, db_session):
        repo2 = _seed_upgradable_pair(svc, dirs, "a", "guid.a", "ModA", "Xavier")

        report = svc.scan_repo_versions()
        assert len(report.repo_newer) == 1
        result = svc.batch_upgrade_from_repo([report.repo_newer[0].repo_mod_id])

        assert result.success_count == 1 and result.failed_count == 0
        assert svc.get_game_mod_by_guid("guid.a").version == "2.0.0"
        assert files_under(dirs.game) == {"Xavier/ModAv2.0.0.zipmod"}  # 旧文件已删
        # v1 装入 + 批量升级各写一条 install 日志（add_to_repo 走 repo_add 不计）
        assert db_op_count(svc, "install") == 2

    def test_progress_callback_shape(self, svc, dirs):
        """进度回调与 batch_install 同形：每项前 (idx,total,name) + 末次 ("完成") 哨兵"""
        repo_a = _seed_upgradable_pair(svc, dirs, "a", "guid.a", "ModA", "Xavier")
        repo_b = _seed_upgradable_pair(svc, dirs, "b", "guid.b", "ModB", "Yuri")

        progress = MagicMock()
        result = svc.batch_upgrade_from_repo([repo_a.id, repo_b.id],
                                             progress_callback=progress)

        assert result.success_count == 2
        assert progress.call_count == 3  # 2 项 + 完成哨兵
        assert progress.call_args == ((2, 2, "完成"),)
        assert progress.call_args_list[0].args[:2] == (0, 2)

    def test_same_version_mid_batch_skipped(self, svc, dirs):
        """扫描后游戏侧已被单发更新到仓库版本 → 批量该项按 SKIPPED 消化"""
        repo2 = _seed_upgradable_pair(svc, dirs, "a", "guid.a", "ModA", "Xavier")
        assert svc.install_from_repo(repo2.id).success  # 先手动升级到 v2

        result = svc.batch_upgrade_from_repo([repo2.id])

        assert result.skipped_count == 1 and result.success_count == 0
        assert result.items[0].status is BatchItemStatus.SKIPPED
        assert "游戏已安装相同版本" in result.items[0].message

    def test_preserves_category_dir_on_disk(self, svc, dirs):
        """分类布局的 Mod 升级不搬家：新文件仍落在 {分类}/{作者}/ 下"""
        repo2 = _seed_upgradable_pair(svc, dirs, "a", "guid.a", "ModA", "Xavier",
                                      category="ForCharactersLoading")
        assert files_under(dirs.game) == {"ForCharactersLoading/Xavier/ModA.zipmod"}

        result = svc.batch_upgrade_from_repo([repo2.id])  # 不传分类，服务端自行推导

        assert result.success_count == 1
        assert files_under(dirs.game) == {"ForCharactersLoading/Xavier/ModAv2.0.0.zipmod"}
        game_row = svc.get_game_mod_by_guid("guid.a")
        assert game_row.game_relative_path.startswith("ForCharactersLoading/")

    def test_copy_failure_mid_batch_continues(self, svc, dirs, monkeypatch):
        """单项复制失败不中断批量：前项 SUCCESS、失败项游戏侧原样"""
        from app.services import mod_service as ms

        repo_a = _seed_upgradable_pair(svc, dirs, "a", "guid.a", "ModA", "Xavier")
        repo_b = _seed_upgradable_pair(svc, dirs, "b", "guid.b", "ModB", "Yuri")

        real_safe_copy = ms.safe_copy
        calls = {"n": 0}

        def _flaky_copy(src, dst_dir, file_name, version):
            calls["n"] += 1
            if calls["n"] >= 2:  # 批量第二项（setup 已完成，monkeypatch 后从零计数）
                raise OSError("boom on second copy")
            return real_safe_copy(src, dst_dir, file_name, version)

        monkeypatch.setattr("app.services.mod_service.safe_copy", _flaky_copy)
        result = svc.batch_upgrade_from_repo([repo_a.id, repo_b.id])

        assert result.success_count == 1 and result.failed_count == 1
        assert result.items[0].status is BatchItemStatus.SUCCESS
        assert result.items[1].status is BatchItemStatus.FAILED
        assert "文件复制失败" in result.items[1].message
        # 失败项游戏行/旧文件原样（F04-8）
        assert svc.get_game_mod_by_guid("guid.b").version == "1.0.0"
        assert files_under(dirs.game) == {
            "Xavier/ModAv2.0.0.zipmod",
            "Yuri/ModB.zipmod",
        }

    def test_paths_not_configured_all_failed(self, svc, dirs):
        repo2 = _seed_upgradable_pair(svc, dirs, "a", "guid.a", "ModA", "Xavier")

        set_config(game_mod_path="")
        result = svc.batch_upgrade_from_repo([repo2.id])

        assert result.total == 1 and result.failed_count == 1
        assert result.items[0].status is BatchItemStatus.FAILED
        assert "未配置" in result.items[0].message


# ═══════════════════════════════════════════════════════════
# 重复项检查 scan_duplicates（F22-1/2/3/4）
# ═══════════════════════════════════════════════════════════

class TestScanDuplicates:
    def test_same_guid_same_location_grouped(self, svc, dirs):
        make_zipmod(dirs.game / "A", "ModA_old", "g.a", version="1.0.0")
        make_zipmod(dirs.game / "A", "ModA_new", "g.a", version="2.0.0")
        make_zipmod(dirs.game / "B", "ModB", "g.b", version="1.0.0")

        report = svc.scan_duplicates()

        assert len(report.groups) == 1
        group = report.groups[0]
        assert group.guid == "g.a" and group.location == ModLocation.GAME
        assert group.name == "ModA_new"                 # 展示名取保留建议项
        assert len(group.files) == 2
        assert report.scanned_game == 3 and report.scanned_repo == 0
        # 保留建议：版本高者保留，其余为建议删除项（F22-4）
        keeper, old = group.files
        assert keeper.suggested_keep and keeper.version == "2.0.0"
        assert not old.suggested_keep and old.version == "1.0.0"
        assert group.suggested_delete_files == [old]
        assert report.suggested_delete_count == 1

    def test_cross_location_same_guid_not_duplicate(self, svc, dirs):
        """F22-2：跨位置各一份同 GUID 不算重复（BR-D04 正常收藏形态）"""
        make_zipmod(dirs.game / "A", "ModA", "g.a", version="1.0.0")
        make_zipmod(dirs.repo / "A", "ModA", "g.a", version="1.0.0")

        report = svc.scan_duplicates()

        assert report.groups == []
        assert report.scanned_game == 1 and report.scanned_repo == 1

    def test_disabled_file_counted_and_annotated(self, svc, dirs):
        make_zipmod(dirs.game / "A", "ModA", "g.a", version="2.0.0")
        make_zipmod(dirs.game / "A", "ModA_old", "g.a", version="1.0.0", disabled=True)

        report = svc.scan_duplicates()

        group = report.groups[0]
        disabled = [f for f in group.files if f.is_disabled]
        assert len(disabled) == 1 and disabled[0].version == "1.0.0"
        assert group.files[0].suggested_keep and not group.files[0].is_disabled

    def test_md5_identical_twin_flagged_within_group(self, svc, dirs):
        make_zipmod(dirs.game / "A", "ModA", "g.a", version="2.0.0")
        p_old = make_zipmod(dirs.game / "B", "ModA_old", "g.a", version="1.0.0")
        twin = dirs.game / "C" / "ModA_old.zipmod"
        twin.parent.mkdir(parents=True)
        shutil.copy2(p_old, twin)  # 字节级相同副本（手工拷贝场景）

        report = svc.scan_duplicates()

        group = report.groups[0]
        twins = {f.path for f in group.files if f.has_identical_twin}
        assert twins == {p_old, twin}
        assert all(f.md5 for f in group.files)
        assert not group.files[0].has_identical_twin  # 保留项没有相同副本

    def test_keeper_prefers_enabled_on_same_version(self, svc, dirs):
        enabled = make_zipmod(dirs.game / "A", "ModA", "g.a", version="1.0.0")
        disabled = make_zipmod(
            dirs.game / "A", "ModA_old", "g.a", version="1.0.0", disabled=True
        )
        os.utime(disabled, (2_000_000_000, 2_000_000_000))  # 禁用副本修改时间反而更新

        report = svc.scan_duplicates()

        assert report.groups[0].files[0].path == enabled

    def test_keeper_falls_back_to_mtime_on_unparseable_versions(self, svc, dirs):
        p_old = make_zipmod(dirs.game / "A", "ModA", "g.a", version="ver.one")
        p_new = make_zipmod(dirs.game / "A", "ModA2", "g.a", version="ver.two")
        os.utime(p_old, (1_000_000_000, 1_000_000_000))
        os.utime(p_new, (1_500_000_000, 1_500_000_000))

        report = svc.scan_duplicates()

        assert report.groups[0].files[0].path == p_new

    def test_parse_failure_reported_not_grouped(self, svc, dirs):
        make_bad_file(dirs.game / "A", "broken.zipmod")
        make_zipmod(dirs.game / "A", "ModA", "g.a")

        report = svc.scan_duplicates()

        assert report.groups == []
        assert len(report.parse_failures) == 1
        assert report.parse_failures[0].path.name == "broken.zipmod"
        assert report.parse_failures[0].location == ModLocation.GAME

    def test_skips_unconfigured_location(self, svc, dirs):
        make_zipmod(dirs.game / "A", "ModA", "g.a")
        set_config(repo_path="")

        report = svc.scan_duplicates()

        assert report.groups == []
        assert "未配置" in report.skipped_repo
        assert report.skipped_game == ""

    def test_progress_callback_shape(self, svc, dirs):
        """解析阶段逐文件 + MD5 阶段逐文件 + 末次「完成」哨兵"""
        _seed_leftover(svc, dirs)
        progress = MagicMock()

        svc.scan_duplicates(progress_callback=progress)

        # 游戏侧 2 个文件：解析 2 次 + 组内 MD5 2 次 + 完成哨兵 1 次
        assert progress.call_count == 5
        assert progress.call_args == ((2, 2, "完成"),)
        assert progress.call_args_list[0].args[2] == "ModA.zipmod"


# ═══════════════════════════════════════════════════════════
# 重复项清理 cleanup_duplicates（F22-5）
# ═══════════════════════════════════════════════════════════

def _seed_leftover(svc, dirs, guid="g.a"):
    """游戏装 v2（DB 跟踪该文件），磁盘作者目录残留 v1 旧文件。

    模拟 BR-F05/F06 版本化重命名积累 + 手工拷贝副本的典型重复来源；
    返回残留文件路径。
    """
    f2 = make_zipmod(dirs.root / "src2", "ModA", guid, version="2.0.0", author="Xavier")
    assert svc.install_to_game(f2).success
    f1 = make_zipmod(dirs.root / "src1", "ModAold", guid, version="1.0.0", author="Xavier")
    leftover = dirs.game / "Xavier" / "ModAold.zipmod"
    shutil.copy2(f1, leftover)
    return leftover


class TestCleanupDuplicates:
    def test_deletes_suggested_leftover_keeps_tracked_file(self, svc, dirs):
        leftover = _seed_leftover(svc, dirs)

        report = svc.scan_duplicates()
        group = report.groups[0]
        assert [f.path for f in group.suggested_delete_files] == [leftover]

        result = svc.cleanup_duplicates(group.suggested_delete_files)

        assert result.success_count == 1 and result.failed_count == 0
        assert files_under(dirs.game) == {"Xavier/ModA.zipmod"}    # 残留已删
        assert svc.get_game_mod_by_guid("g.a").version == "2.0.0"  # DB 跟踪行原样
        assert len(result.sync_reports) == 1                        # 删除后对账
        assert result.sync_reports[0].change_count == 0             # 对账无变更

    def test_repoints_db_row_when_tracked_file_deleted(self, svc, dirs):
        """被删的恰是 DB 跟踪文件时，对账把行重新指向磁盘幸存文件"""
        _seed_leftover(svc, dirs)
        report = svc.scan_duplicates()
        group = report.groups[0]
        tracked = group.files[0]   # 建议保留项 = DB 跟踪的 v2
        survivor = group.files[1]  # 残留的 v1

        result = svc.cleanup_duplicates([tracked])

        assert result.success_count == 1
        assert files_under(dirs.game) == {"Xavier/ModAold.zipmod"}
        row = svc.get_game_mod_by_guid("g.a")
        assert row.version == "1.0.0" and row.file_name == "ModAold.zipmod"
        assert row.game_relative_path == "Xavier/ModAold.zipmod"
        assert db_op_count(svc, "sync") == 1  # 对账更新了行 → 有 sync 日志

    def test_missing_file_skipped_without_sync_or_log(self, svc, dirs):
        _seed_leftover(svc, dirs)
        report = svc.scan_duplicates()
        entries = report.groups[0].suggested_delete_files
        entries[0].path.unlink()  # 扫描后文件被手动删掉

        result = svc.cleanup_duplicates(entries)

        assert result.skipped_count == 1 and result.success_count == 0
        assert result.items[0].status is BatchItemStatus.SKIPPED
        assert result.sync_reports == []  # 无实际删除 → 不对账
        assert db_op_count(svc, "dedup") == 0

    def test_writes_dedup_log_with_file_names(self, svc, dirs):
        _seed_leftover(svc, dirs)
        report = svc.scan_duplicates()

        svc.cleanup_duplicates(report.groups[0].suggested_delete_files)

        assert db_op_count(svc, "dedup") == 1
        log = svc.session.query(OperationLog).filter_by(operation="dedup").one()
        assert log.mod_name == "(重复项清理)"
        assert log.location == "game"
        assert "1 个重复文件" in log.detail and "ModAold.zipmod" in log.detail
        # 本场景 DB 跟踪行未变 → 对账无变更、无 sync 日志
        assert db_op_count(svc, "sync") == 0

    def test_empty_author_dir_cleaned_after_delete(self, svc, dirs):
        """残留独占的子目录在删除后顺手移除（clean_empty_dirs 默认开）"""
        _seed_leftover(svc, dirs)
        leftover_dir = dirs.game / "OldAuthor"
        leftover_dir.mkdir()
        shutil.move(
            str(dirs.game / "Xavier" / "ModAold.zipmod"),
            str(leftover_dir / "ModAold.zipmod"),
        )

        report = svc.scan_duplicates()
        result = svc.cleanup_duplicates(report.groups[0].suggested_delete_files)

        assert result.success_count == 1
        assert files_under(dirs.game) == {"Xavier/ModA.zipmod"}
        assert not leftover_dir.exists()


# ═══════════════════════════════════════════════════════════
# 应用内回收站集成（F25：卸载 / 升级替换 / 重复项清理）
# ═══════════════════════════════════════════════════════════

def _trash_today(trash_root: Path) -> Path:
    return trash_root / datetime.now().strftime("%Y-%m-%d")


class TestTrashIntegration:
    def test_uninstall_moves_to_trash_by_default(self, svc, dirs, trash_root):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        res = svc.uninstall(mod.id)

        assert res.success and "卸载成功" in res.message
        assert files_under(dirs.game) == set()
        assert files_under(trash_root) == {"%s/ModA.zipmod" % datetime.now().strftime("%Y-%m-%d")}
        assert svc.get_mod_by_id(mod.id) is None
        log = svc.session.query(OperationLog).filter_by(operation="uninstall").one()
        assert "（→ 回收站）" in log.detail

    def test_uninstall_hard_deletes_when_trash_disabled(self, svc, dirs, trash_root):
        set_config(use_trash=False)
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        res = svc.uninstall(mod.id)

        assert res.success
        assert files_under(dirs.game) == set()
        assert not trash_root.exists()  # 回收站目录都未创建

    def test_uninstall_trash_failure_fails_whole_operation(
        self, svc, dirs, monkeypatch
    ):
        """F25-5：移入回收站失败 → 操作失败，文件与 DB 行都保留"""
        def _boom(_path):
            raise OSError("simulated trash failure")

        monkeypatch.setattr("app.services.mod_service.move_to_trash", _boom)
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        mod = svc.install_to_game(src).mod

        res = svc.uninstall(mod.id)

        assert not res.success
        assert "移入回收站失败" in res.message
        assert (dirs.game / "Alice" / "ModA.zipmod").is_file()
        assert svc.get_mod_by_id(mod.id) is not None
        assert svc.get_game_mod_by_guid("guid.a").file_name == "ModA.zipmod"

    def test_upgrade_supersede_moves_old_file_to_trash(self, svc, dirs, trash_root):
        """升级替换删除旧文件同样入回收站（失败仅告警，F25-5 宽松条款）"""
        v1 = make_zipmod(dirs.root / "v1", "ModA", "guid.a", version="1.0.0", author="Alice")
        assert svc.install_to_game(v1).success
        v2 = make_zipmod(dirs.root / "v2", "ModA", "guid.a", version="2.0.0", author="Alice")

        res = svc.install_to_game(v2, confirm_upgrade=True)

        assert res.success
        assert files_under(dirs.game) == {"Alice/ModAv2.0.0.zipmod"}
        assert files_under(_trash_today(trash_root)) == {"ModA.zipmod"}

    def test_cleanup_duplicates_moves_to_trash(self, svc, dirs, trash_root):
        leftover = _seed_leftover(svc, dirs)
        report = svc.scan_duplicates()

        result = svc.cleanup_duplicates(report.groups[0].suggested_delete_files)

        assert result.success_count == 1
        assert "已移入回收站" in result.items[0].message
        assert files_under(dirs.game) == {"Xavier/ModA.zipmod"}
        assert files_under(_trash_today(trash_root)) == {"ModAold.zipmod"}
        log = svc.session.query(OperationLog).filter_by(operation="dedup").one()
        assert "移入回收站 1 个重复文件" in log.detail


class TestAddGameModToRepo:
    def test_success_copies_to_repo(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        game_mod = svc.install_to_game(src).mod

        res = svc.add_game_mod_to_repo(game_mod.id)
        assert res.success
        assert "已加入仓库" in res.message
        assert (dirs.repo / "Alice" / "ModA.zipmod").is_file()
        repo_mod = svc.get_repo_mod_by_guid("guid.a")
        assert repo_mod.repo_relative_path == "Alice/ModA.zipmod"

    def test_same_version_in_repo_skips(self, svc, dirs):
        src = make_zipmod(dirs.root / "src", "ModA", "guid.a", author="Alice")
        game_mod = svc.install_to_game(src).mod
        assert svc.add_game_mod_to_repo(game_mod.id).success

        res = svc.add_game_mod_to_repo(game_mod.id)
        assert not res.success
        assert "仓库中已存在相同版本" in res.message

    def test_repo_mod_id_rejected(self, svc):
        mod = seed_mod(svc.session, "guid.r", "RepoMod", "Xavier",
                       location=ModLocation.REPO)
        res = svc.add_game_mod_to_repo(mod.id)
        assert not res.success
        assert res.message == "只能将游戏中的 Mod 添加到仓库"


# ═══════════════════════════════════════════════════════════
# 查询 / 统计 / 分组
# ═══════════════════════════════════════════════════════════

class TestQueries:
    @pytest.mark.parametrize("keyword", ["Dress", "dress", "alice", "com.alice"])
    def test_search_hits_name_author_guid(self, svc, keyword):
        seed_mod(svc.session, "com.alice.dress", "Alice Dress", "Alice",
                 location=ModLocation.GAME)
        hits = svc.get_mods_by_location(ModLocation.GAME, search=keyword)
        assert [m.guid for m in hits] == ["com.alice.dress"]

    def test_search_isolated_by_location(self, svc):
        seed_mod(svc.session, "g1", "Alpha Mod", "Zed", location=ModLocation.REPO)
        assert svc.get_mods_by_location(ModLocation.GAME, search="Alpha") == []

    def test_order_and_pagination(self, svc):
        seed_mod(svc.session, "g1", "Zeta", "Bob")
        seed_mod(svc.session, "g2", "Alpha", "anna")   # 小写 normalized 排最前
        seed_mod(svc.session, "g3", "Mid", "Bob")
        seed_mod(svc.session, "g4", "Beta", "Cara")

        # 按 (author_normalized, name) 排序：anna < bob < cara；Bob 组内 Mid < Zeta
        all_mods = svc.get_mods_by_location(ModLocation.GAME)
        assert [m.name for m in all_mods] == ["Alpha", "Mid", "Zeta", "Beta"]

        page1 = svc.get_mods_by_location(ModLocation.GAME, limit=2)
        assert [m.name for m in page1] == ["Alpha", "Mid"]
        page2 = svc.get_mods_by_location(ModLocation.GAME, limit=2, offset=2)
        assert [m.name for m in page2] == ["Zeta", "Beta"]

    def test_count_with_and_without_search(self, svc):
        seed_mod(svc.session, "g1", "Alpha", "Bob")
        seed_mod(svc.session, "g2", "Beta", "Bob")
        seed_mod(svc.session, "g3", "Gamma", "Cara", location=ModLocation.REPO)

        assert svc.get_mods_count(ModLocation.GAME) == 2
        assert svc.get_mods_count(ModLocation.GAME, search="bob") == 2
        assert svc.get_mods_count(ModLocation.GAME, search="gamma") == 0

    def test_get_mod_by_id(self, svc):
        mod = seed_mod(svc.session, "g1", "Alpha", "Bob")
        assert svc.get_mod_by_id(mod.id).guid == "g1"
        assert svc.get_mod_by_id(-1) is None

    def test_guid_queries_isolated_by_location(self, svc):
        seed_mod(svc.session, "shared", "GameCopy", "Bob", location=ModLocation.GAME)
        seed_mod(svc.session, "shared", "RepoCopy", "Bob", location=ModLocation.REPO)

        assert svc.get_game_mod_by_guid("shared").name == "GameCopy"
        assert svc.get_repo_mod_by_guid("shared").name == "RepoCopy"
        assert svc.get_game_mod_by_guid("missing") is None

    def test_statistics(self, svc):
        seed_mod(svc.session, "g1", "A", "Bob")
        seed_mod(svc.session, "g2", "B", "Bob")
        seed_mod(svc.session, "g3", "C", "Bob", status=ModStatus.DISABLED)
        seed_mod(svc.session, "g4", "D", "Bob", location=ModLocation.REPO)

        assert svc.get_statistics(ModLocation.GAME) == {
            "total": 3, "enabled": 2, "disabled": 1,
        }
        assert svc.get_statistics(ModLocation.REPO)["total"] == 1

    def test_group_by_author_blank_becomes_unknown(self, svc):
        seed_mod(svc.session, "g1", "A", "Bob")
        seed_mod(svc.session, "g2", "B", "")
        groups = svc.group_by_author(ModLocation.GAME)
        assert set(groups.keys()) == {"Bob", "Unknown"}
        assert [m.name for m in groups["Unknown"]] == ["B"]

    def test_group_by_author_paginated(self, svc):
        seed_mod(svc.session, "g1", "A", "Bob")
        seed_mod(svc.session, "g2", "B", "Bob")
        seed_mod(svc.session, "g3", "C", "Cara")

        groups, total = svc.group_by_author_paginated(
            ModLocation.GAME, page=1, page_size=2,
        )
        assert total == 3                      # 总数不受分页影响
        assert len(groups["Bob"]) == 2         # 本页恰好两行


# ═══════════════════════════════════════════════════════════
# collect_zipmods / batch_install
# ═══════════════════════════════════════════════════════════

class TestCollectAndBatch:
    def test_collect_zipmods_mixed(self, svc, dirs):
        loose = make_zipmod(dirs.root, "Loose", "guid.loose")
        batch_dir = dirs.root / "batch"
        make_zipmod(batch_dir, "InDir", "guid.in_dir")
        make_zipmod(batch_dir, "Disabled", "guid.dis", disabled=True)
        (batch_dir / "notes.txt").write_text("不是 mod", encoding="utf-8")

        result = ModService.collect_zipmods([loose, batch_dir, loose])
        names = {p.name for p in result}
        assert names == {"Loose.zipmod", "InDir.zipmod", "Disabled.zipmod_disabled"}
        assert len(result) == 3  # 重复传入去重

    def test_batch_install_mixed_statuses(self, svc, dirs):
        # 预装 guid.same v1.0.0
        pre = make_zipmod(dirs.root / "pre", "Same", "guid.same", author="Alice")
        assert svc.install_to_game(pre).success

        batch_dir = dirs.root / "batch"
        new_file = make_zipmod(batch_dir, "New", "guid.new", author="Alice")
        same_file = make_zipmod(batch_dir, "Same", "guid.same", version="1.0.0",
                                author="Alice")
        # 不同版本须用不同文件名，否则同目录同名会互相覆盖 manifest
        diff_file = make_zipmod(batch_dir, "SameV2", "guid.same", version="2.0.0",
                                author="Alice")
        bad_file = make_bad_file(batch_dir, "Broken.zipmod")

        progress = MagicMock()
        batch = svc.batch_install(
            [new_file, same_file, diff_file, bad_file],
            ModLocation.GAME, progress_callback=progress,
        )

        assert batch.total == 4
        assert batch.success_count == 1
        assert batch.skipped_count == 1
        assert batch.conflict_count == 1
        assert batch.failed_count == 1
        by_name = {i.file_path.name: i for i in batch.items}
        assert by_name["New.zipmod"].status is BatchItemStatus.SUCCESS
        assert by_name["Same.zipmod"].status is BatchItemStatus.SKIPPED
        assert by_name["SameV2.zipmod"].status is BatchItemStatus.CONFLICT
        assert by_name["Broken.zipmod"].status is BatchItemStatus.FAILED
        assert len(batch.conflicts) == 1 and len(batch.failures) == 1
        # 进度回调：每个文件一次 + 末次“完成”
        assert progress.call_count == 5
        assert progress.call_args == ((4, 4, "完成"),)
        # 磁盘只多了新文件，DB 多了一行
        assert files_under(dirs.game) == {
            "Alice/Same.zipmod", "Alice/New.zipmod",
        }
        assert svc.get_mods_count(ModLocation.GAME) == 2

    def test_batch_install_path_not_configured(self, svc, dirs):
        set_config(game_mod_path="")
        batch_dir = dirs.root / "batch"
        f1 = make_zipmod(batch_dir, "A", "guid.a")
        f2 = make_zipmod(batch_dir, "B", "guid.b")

        batch = svc.batch_install([f1, f2], ModLocation.GAME)
        assert batch.failed_count == 2
        assert all("未配置" in i.message for i in batch.items)

    def test_batch_install_to_repo_sets_relative_path(self, svc, dirs):
        batch_dir = dirs.root / "batch"
        f = make_zipmod(batch_dir, "RepoMod", "guid.r", author="Xavier")

        batch = svc.batch_install([f], ModLocation.REPO)
        assert batch.success_count == 1
        mod = svc.get_repo_mod_by_guid("guid.r")
        assert mod.repo_relative_path == "Xavier/RepoMod.zipmod"
        assert (dirs.repo / "Xavier" / "RepoMod.zipmod").is_file()


# ═══════════════════════════════════════════════════════════
# 数据类与工具函数
# ═══════════════════════════════════════════════════════════

class TestDataclasses:
    def test_sync_report_change_count(self):
        report = SyncReport()
        report.added = [SyncChangeItem("added", "g", "n", "f")]
        report.removed = [SyncChangeItem("removed", "g", "n", "f")] * 2
        report.updated = [SyncChangeItem("updated", "g", "n", "f")] * 3
        assert report.change_count == 6

    def test_sync_report_summary_game(self):
        report = SyncReport(location=ModLocation.GAME, scanned=5)
        report.added = [SyncChangeItem("added", "g", "n", "f")] * 2
        report.removed = [SyncChangeItem("removed", "g", "n", "f")]
        report.parse_failures = [SyncChangeItem("parse_failed", "", "n", "f")]
        s = report.summary()
        assert s.startswith("[游戏]")
        assert "扫描 5" in s and "新增 2" in s and "删除 1" in s
        assert "更新 0" in s and "解析失败 1" in s

    def test_sync_report_summary_repo_and_skipped(self):
        report = SyncReport(location=ModLocation.REPO, skipped=True,
                            message="未配置仓库路径")
        assert report.summary() == "[repo] 跳过：未配置仓库路径"

    def test_normalize_author(self):
        assert _normalize_author(None) == "Unknown"
        assert _normalize_author("") == "Unknown"
        assert _normalize_author("   ") == "Unknown"
        assert _normalize_author(" Alice ") == "Alice"
