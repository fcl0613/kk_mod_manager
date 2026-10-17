"""ModService 业务方法测试（install / uninstall / set_enabled / 查询 / 批量）。

sync_from_disk / refresh_from_disk 已由 tests/sync_service_test.py 手动脚本覆盖，
此处不重复；仅测 SyncReport 数据类。
"""

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
        assert files_under(dirs.game) == {"Alice/ModA.zipmod"}  # 旧文件已删，仍只一份
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
        assert files_under(dirs.game) == {"Xavier/RepoMod.zipmod"}

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
