"""sync_from_disk 增量同步手动测试（无 pytest，__main__ 直接运行）。

不触碰真实数据库与配置：
  - 临时目录模拟游戏目录 / 仓库目录
  - 内存 SQLite（create_all）
  - 直接替换 app.config._config_instance 单例

运行：python tests/sync_service_test.py
"""

import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.config as cfg
from app.config import AppConfig
from app.database import Base
from app.models.mod import Mod, ModLocation, ModStatus
from app.services.mod_service import ModService

_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(f"断言失败: {msg}")
    _passed += 1
    print(f"  ✓ {msg}")


def make_zipmod(
    dir_path: Path,
    file_stem: str,
    guid: str,
    version: str = "1.0.0",
    author: str = "TestAuthor",
    disabled: bool = False,
) -> Path:
    """造一个含 manifest.xml 的 zipmod 文件"""
    body = (
        "<manifest>"
        f"<guid>{guid}</guid>"
        f"<name>{file_stem}</name>"
        f"<version>{version}</version>"
        f"<author>{author}</author>"
        "</manifest>"
    )
    fn = file_stem + (".zipmod_disabled" if disabled else ".zipmod")
    p = dir_path / fn
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.xml", body)
    return p


def fresh_service(game_dir: Path, repo_dir: Path) -> ModService:
    """用临时路径 + 内存库构造独立 ModService"""
    cfg._config_instance = AppConfig(
        game_mod_path=str(game_dir), repo_path=str(repo_dir)
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return ModService(sessionmaker(bind=engine)())


def query_mods(svc: ModService, location: ModLocation) -> list[Mod]:
    return (
        svc.session.query(Mod)
        .filter(Mod.location == location)
        .order_by(Mod.guid)
        .all()
    )


if __name__ == "__main__":
    # Windows 控制台默认 GBK，重配为 UTF-8 避免中文/符号打印失败
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        game, repo = root / "game", root / "repo"
        game.mkdir()
        repo.mkdir()
        svc = fresh_service(game, repo)

        # ── 用例 1：空库首次同步（含禁用文件）──────────────────
        print("[1] 空库 + 2 启用 + 1 禁用 → 3 条记录，禁用状态正确")
        make_zipmod(game, "ModA", "guid.a", author="Alice")
        make_zipmod(game, "ModB", "guid.b", author="Bob")
        make_zipmod(game, "ModC", "guid.c", author="Carol", disabled=True)
        report = svc.sync_from_disk(ModLocation.GAME)
        mods = query_mods(svc, ModLocation.GAME)
        check(len(mods) == 3, "新增 3 条记录")
        check(report.change_count == 3 and len(report.added) == 3, "报告 added=3")
        by_guid = {m.guid: m for m in mods}
        check(by_guid["guid.c"].file_name == "ModC.zipmod_disabled",
              "禁用文件 file_name 保留实际名 ModC.zipmod_disabled")
        check(by_guid["guid.c"].status == ModStatus.DISABLED, "禁用文件 status=DISABLED")
        check(by_guid["guid.a"].status == ModStatus.ENABLED, "启用文件 status=ENABLED")

        # ── 用例 2：幂等 + notes/installed_at 保留 ─────────────
        print("[2] 再同步 → 无变更，notes/installed_at 不动")
        mod_a = by_guid["guid.a"]
        mod_a.notes = "我的珍贵备注"
        mod_a.installed_at = datetime(2020, 1, 1, 12, 0, 0)
        svc.session.commit()
        report = svc.sync_from_disk(ModLocation.GAME)
        check(report.change_count == 0, "无变更（幂等）")
        svc.session.expire_all()
        mod_a = query_mods(svc, ModLocation.GAME)[0]
        check(mod_a.notes == "我的珍贵备注", "notes 保留")
        check(mod_a.installed_at == datetime(2020, 1, 1, 12, 0, 0), "installed_at 保留")

        # ── 用例 3：GAME 改名 / REPO 移动 → 更新 ───────────────
        print("[3] 文件改名与移动 → 更新记录")
        (game / "ModA.zipmod").rename(game / "ModA_renamed.zipmod")
        repo_file = make_zipmod(repo, "RepoX", "guid.x", author="Xavier")
        report = svc.sync_from_disk(ModLocation.REPO)
        check(len(report.added) == 1, "REPO 首次同步新增 1 条")
        report = svc.sync_from_disk(ModLocation.GAME)
        mod_a = next(m for m in query_mods(svc, ModLocation.GAME) if m.guid == "guid.a")
        check(mod_a.file_name == "ModA_renamed.zipmod", "GAME 文件改名已同步")
        check(any("文件名" in i.detail for i in report.updated), "报告含文件名变更明细")
        subdir = repo / "Xavier"
        subdir.mkdir()
        repo_file.rename(subdir / repo_file.name)
        report = svc.sync_from_disk(ModLocation.REPO)
        mod_x = query_mods(svc, ModLocation.REPO)[0]
        check(mod_x.repo_relative_path == "Xavier/RepoX.zipmod",
              "REPO 移入子目录 → repo_relative_path 更新")

        # ── 用例 4：删文件 → 记录删除 ──────────────────────────
        print("[4] 删除磁盘文件 → 对应记录 removed")
        (game / "ModB.zipmod").unlink()
        report = svc.sync_from_disk(ModLocation.GAME)
        guids = {m.guid for m in query_mods(svc, ModLocation.GAME)}
        check("guid.b" not in guids, "记录已删除")
        check(any(i.guid == "guid.b" and "文件已消失" in i.detail
                  for i in report.removed), "报告 removed 含原因")

        # ── 用例 5：同名文件被替换成其他 GUID ──────────────────
        print("[5] 同名文件内容换成不同 GUID → 删旧增新（唯一索引不炸）")
        (game / "ModC.zipmod_disabled").unlink()
        make_zipmod(game, "ModC", "guid.brand_new", author="NewGuy")
        report = svc.sync_from_disk(ModLocation.GAME)
        guids = {m.guid for m in query_mods(svc, ModLocation.GAME)}
        check("guid.c" not in guids, "旧 GUID 记录已删除")
        check("guid.brand_new" in guids, "新 GUID 记录已新增")
        check(any(i.guid == "guid.brand_new" for i in report.added)
              and any(i.guid == "guid.c" for i in report.removed),
              "报告同时含 added 与 removed")

        # ── 用例 6：版本变化 → 更新 ────────────────────────────
        print("[6] manifest 版本升级 → updated 含版本明细")
        target = next(m for m in query_mods(svc, ModLocation.GAME)
                      if m.guid == "guid.brand_new")
        make_zipmod(game, "ModC", "guid.brand_new", version="2.0.0", author="NewGuy")
        report = svc.sync_from_disk(ModLocation.GAME)
        svc.session.expire_all()
        target = next(m for m in query_mods(svc, ModLocation.GAME)
                      if m.guid == "guid.brand_new")
        check(target.version == "2.0.0", "版本已更新为 2.0.0")
        check(any(i.guid == "guid.brand_new" and "v1.0.0" in i.detail
                  for i in report.updated), "报告含 v1.0.0 → v2.0.0 明细")

        # ── 用例 7：老 REPO 数据 repo_relative_path 为空 → 回填 ─
        print("[7] 老数据 repo_relative_path 为空 → 同步后回填")
        rel = repo / "Legacy" / "LegacyMod.zipmod"
        rel.parent.mkdir(exist_ok=True)
        make_zipmod(rel.parent, "LegacyMod", "guid.legacy", author="Legacy")
        legacy = Mod(
            guid="guid.legacy", name="LegacyMod", version="1.0.0",
            author="Legacy", author_normalized="legacy",
            file_name="LegacyMod.zipmod", location=ModLocation.REPO,
            repo_relative_path="",
        )
        svc.session.add(legacy)
        svc.session.commit()
        report = svc.sync_from_disk(ModLocation.REPO)
        svc.session.expire_all()
        legacy = next(m for m in query_mods(svc, ModLocation.REPO)
                      if m.guid == "guid.legacy")
        check(legacy.repo_relative_path == "Legacy/LegacyMod.zipmod",
              "repo_relative_path 已回填")
        check(any("路径" in i.detail for i in report.updated), "报告含路径回填明细")

        # ── 用例 8：损坏文件 → 解析失败，不误删 ────────────────
        print("[8] 损坏 zip → parse_failures 记录、不增不删")
        bad = game / "ModA_renamed.zipmod"
        bad.unlink()
        bad.write_text("这不是一个zip文件")
        report = svc.sync_from_disk(ModLocation.GAME)
        check(len(report.parse_failures) == 1, "记录 1 条解析失败")
        svc.session.expire_all()
        mod_a = next(m for m in query_mods(svc, ModLocation.GAME)
                     if m.guid == "guid.a")
        check(mod_a is not None, "解析失败文件的既有记录保留（不误删）")
        check(report.change_count == 0, "损坏文件不产生增删")

        # ── 用例 9：同 GUID 双文件（一禁用一启用）→ 取启用 ────
        print("[9] 同 GUID 两个文件 → 仅 1 条记录，优先启用态")
        dup_dir = game / "DupAuthor"
        dup_dir.mkdir()
        make_zipmod(dup_dir, "DupMod", "guid.dup", author="DupAuthor")
        make_zipmod(dup_dir, "DupMod2", "guid.dup", author="DupAuthor", disabled=True)
        report = svc.sync_from_disk(ModLocation.GAME)
        dups = [m for m in query_mods(svc, ModLocation.GAME) if m.guid == "guid.dup"]
        check(len(dups) == 1, "仅 1 条记录")
        check(dups[0].file_name == "DupMod.zipmod", "选中启用态文件")
        check(dups[0].status == ModStatus.ENABLED, "status=ENABLED")

        # ── 用例 10：REPO 的禁用后缀文件 → status 仍 ENABLED ──
        print("[10] REPO 放 .zipmod_disabled → file_name 保留实际名，status=ENABLED")
        make_zipmod(repo, "RepoDis", "guid.repo_dis", author="Yvonne", disabled=True)
        report = svc.sync_from_disk(ModLocation.REPO)
        svc.session.expire_all()
        rd = next(m for m in query_mods(svc, ModLocation.REPO)
                  if m.guid == "guid.repo_dis")
        check(rd.file_name == "RepoDis.zipmod_disabled", "file_name 为实际磁盘名")
        check(rd.status == ModStatus.ENABLED, "REPO 恒 ENABLED（与现状语义一致）")

        # ── 用例 11：路径未配置 → skipped ─────────────────────
        print("[11] 路径未配置 → skipped 报告")
        cfg._config_instance = AppConfig(
            game_mod_path="", repo_path=str(repo)
        )
        report = svc.sync_from_disk(ModLocation.GAME)
        check(report.skipped and "未配置" in report.message, "GAME 未配置 → skipped")

        print(f"\nALL SYNC TESTS PASSED ({_passed} assertions)")
