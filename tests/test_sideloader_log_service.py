"""Sideloader 运行日志解析服务测试（F23）。

日志行样本取自真实 logs/LogOutput.log（丽莎卡样本，旧版 Sideloader 措辞，
同一缺失在 Message / Info / Warning 三级行各出现一次）与 BepisPlugins
当前源码 Core.UAR.cs ShowGUIDError 的输出格式（website 不带方括号、
伴随 "Missing/Outdated zipmod! … - 作者 : 名称" 行）。
"""

from pathlib import Path

from app.config import AppConfig
from app.models.mod import Mod, ModLocation, ModStatus
from app.services.card_service import CardModStatus, CardService
from app.services.sideloader_log_service import (
    LogEntryKind,
    SideloaderLogService,
)

# 真实样本节选（旧版措辞）+ 干扰行
OLD_FORMAT_SAMPLE = "\n".join([
    "[Info   :Sideloader] ----------------------------------------",
    "[Info   :Sideloader] Loading card [丽莎.png]",
    "[Info   :Sideloader] Sideloader marker found, external info count: 61",
    "[Info   :Sideloader] External info: ChaFileFace.headId : HeadMod : bo_head : 1",
    "[Message:Sideloader] [UAR] WARNING! Missing mod detected! [LisaMod_Cetol]",
    "[Info   :Sideloader] [UAR] WARNING! Missing mod detected! [LisaMod_Cetol][https://www.pixiv.net/users/70808817]",
    "[Warning:Sideloader] Missing mod [LisaMod_Cetol][ao_head][524032][outfit0.accessory1.ChaFileAccessory.PartsInfo.id]",
    "[Message:Sideloader] [UAR] WARNING! Missing mod detected! [bodytights.AccClothes]",
    "[Warning:Sideloader] Missing mod [bodytights.AccClothes][ao_body][19][outfit0.accessory5.ChaFileAccessory.PartsInfo.id]",
    "[Message:Sideloader] [UAR] WARNING! Missing mod detected! [com.Quokka.weddingdressa]",
    "[Info   :Sideloader] [UAR] WARNING! Missing mod detected! [com.Quokka.weddingdressa][https://twitter.com/kouryaku2]",
    "[Warning:Sideloader] Missing mod [com.Quokka.weddingdressa][co_top][127][outfit4.ChaFileClothes.ClothesTop]",
    '[Warning:Sideloader] Multiple versions detected, only "ForCharactersLoading\\Madevil\\[Madevil] gloves.AccClothes.zipmod" will be loaded.',
    "[Debug  :Sideloader] Missing mod detected [hard.mod.guid] but matching ID found, most likely you have a hard mod version of this mod",
    "[Info   : BepInEx] Loading [Restore missing functions 21.3]",
])


def write_log(tmp_path: Path, text: str) -> Path:
    """写一个临时 LogOutput.log"""
    log = tmp_path / "LogOutput.log"
    log.write_text(text, encoding="utf-8")
    return log


def _add_mod(session, guid, location, version="1.0.0", author="TestAuthor",
             status=ModStatus.ENABLED):
    """直接插入一条 Mod 行（绕过文件解析，只测对照逻辑）"""
    mod = Mod(
        guid=guid,
        name=f"[{author}] {guid}",
        version=version,
        author=author,
        author_normalized=author.lower(),
        file_name=f"[{author}]{guid}.zipmod",
        location=location,
        status=status,
    )
    session.add(mod)
    session.commit()
    return mod


class TestParseLog:
    def test_old_format_sample_dedup(self, tmp_path):
        """真实样本：三级行重复报告按 GUID 去重，website 从 Info 行取到"""
        svc = SideloaderLogService()
        entries = {e.guid: e for e in svc.parse_log(write_log(tmp_path, OLD_FORMAT_SAMPLE))}

        assert set(entries) == {
            "LisaMod_Cetol", "bodytights.AccClothes", "com.Quokka.weddingdressa",
        }
        lisa = entries["LisaMod_Cetol"]
        assert lisa.kind is LogEntryKind.MISSING
        assert lisa.website == "https://www.pixiv.net/users/70808817"
        # 同一缺失刷了 Message/Info/Warning 多行，occurrences 累计但条目唯一
        assert lisa.occurrences >= 3
        assert entries["bodytights.AccClothes"].website == ""
        assert entries["com.Quokka.weddingdressa"].website == "https://twitter.com/kouryaku2"

    def test_noise_lines_not_parsed(self, tmp_path):
        """External info / Multiple versions / hardmod debug 行均不解析（F23-4 / F23-6）"""
        svc = SideloaderLogService()
        entries = svc.parse_log(write_log(tmp_path, OLD_FORMAT_SAMPLE))
        assert all(e.guid != "hard.mod.guid" for e in entries)

    def test_current_source_format(self, tmp_path):
        """当前源码：website 裸跟 GUID 后；伴随 zipmod 行提供 作者 : 名称 元数据。

        ShowGUIDError 先 Log 两份 zipmod 行（Warning|Message 合并级别）再
        LogWarning [UAR] 行，故伴随行与 [UAR] 行紧邻。
        """
        svc = SideloaderLogService()
        log = write_log(tmp_path, "\n".join([
            "[Warning:Sideloader] Missing zipmod! Some items are missing! - Lisa : Lisa Head Mod",
            "[Message:Sideloader] Missing zipmod! Some items are missing! - Lisa : Lisa Head Mod",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [LisaMod_Cetol]  https://www.pixiv.net/users/70808817",
            "[Warning:Sideloader] Missing zipmod! Some items are missing! - mod.no_site",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [mod.no_site]",
            # 伴随行与 [UAR] 行之间插入无关行 → 元数据作废，不误挂
            "[Warning:Sideloader] Missing zipmod! Some items are missing! - Ghost : Ghost Mod",
            "[Info   : BepInEx] Loading [SomePlugin 1.0]",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [g.ghost]",
        ]))
        entries = {e.guid: e for e in svc.parse_log(log)}

        lisa = entries["LisaMod_Cetol"]
        assert lisa.kind is LogEntryKind.MISSING
        assert lisa.website == "https://www.pixiv.net/users/70808817"
        assert lisa.meta_author == "Lisa"
        assert lisa.meta_name == "Lisa Head Mod"

        # website 为空的当前格式行 + 尾巴是裸 GUID 的伴随行（无作者无名称）
        no_site = entries["mod.no_site"]
        assert no_site.website == ""
        assert no_site.meta_name == ""
        assert no_site.meta_author == ""

        # 伴随行与 [UAR] 行被无关行隔开 → 不挂元数据
        ghost = entries["g.ghost"]
        assert ghost.meta_name == ""
        assert ghost.meta_author == ""

    def test_bare_guid_zipmod_tail_not_metadata(self, tmp_path):
        """无名称时伴随行尾巴是裸 GUID，不得误当作名称元数据"""
        svc = SideloaderLogService()
        log = write_log(tmp_path, "\n".join([
            "[Warning:Sideloader] Missing zipmod! Some items are missing! - mod.bare",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [mod.bare]",
        ]))
        entry = svc.parse_log(log)[0]
        assert entry.guid == "mod.bare"
        assert entry.meta_name == ""
        assert entry.meta_author == ""

    def test_outdated_kind(self, tmp_path):
        """Outdated（GUID 已装但槽位未找到 = 版本过旧，F23-3）"""
        svc = SideloaderLogService()
        log = write_log(tmp_path, "\n".join([
            "[Warning:Sideloader] Outdated zipmod! Some items are missing! - KK : KK Overhaul",
            "[Warning:Sideloader] [UAR] WARNING! Outdated mod detected! [com.kk.overhaul]  https://example.com",
        ]))
        entry = svc.parse_log(log)[0]
        assert entry.guid == "com.kk.overhaul"
        assert entry.kind is LogEntryKind.OUTDATED
        assert entry.website == "https://example.com"
        assert entry.meta_author == "KK"
        assert entry.meta_name == "KK Overhaul"

    def test_kind_conflict_prefers_missing(self, tmp_path):
        """同一 GUID 同时报缺失与过旧（理论不应发生）以缺失为准"""
        svc = SideloaderLogService()
        log = write_log(tmp_path, "\n".join([
            "[Warning:Sideloader] [UAR] WARNING! Outdated mod detected! [conflict.mod]",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [conflict.mod]",
        ]))
        assert svc.parse_log(log)[0].kind is LogEntryKind.MISSING

    def test_bom_tolerated(self, tmp_path):
        """UTF-8 BOM 开头的日志首行照常命中"""
        log = tmp_path / "LogOutput.log"
        log.write_bytes(
            b"\xef\xbb\xbf[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [bom.mod]\n"
        )
        entries = SideloaderLogService().parse_log(log)
        assert [e.guid for e in entries] == ["bom.mod"]


class TestDetectLogPath:
    def test_mods_subdir_layout(self, tmp_path):
        """常见布局 {游戏根}/mods → {游戏根}/BepInEx/LogOutput.log"""
        log = tmp_path / "BepInEx" / "LogOutput.log"
        log.parent.mkdir(parents=True)
        log.write_text("x", encoding="utf-8")
        config = AppConfig(game_mod_path=str(tmp_path / "mods"))
        assert SideloaderLogService.detect_log_path(config) == log

    def test_game_root_itself(self, tmp_path):
        """game_mod_path 即游戏根（BepInEx 与 mods 同级不存在时）"""
        log = tmp_path / "BepInEx" / "LogOutput.log"
        log.parent.mkdir(parents=True)
        log.write_text("x", encoding="utf-8")
        config = AppConfig(game_mod_path=str(tmp_path))
        assert SideloaderLogService.detect_log_path(config) == log

    def test_separated_layout_not_found(self, tmp_path):
        """游戏根与 Mods 根分离（目录联动场景）→ 探测失败交由 UI 引导"""
        config = AppConfig(game_mod_path=str(tmp_path / "media" / "mod_game"))
        assert SideloaderLogService.detect_log_path(config) is None

    def test_empty_game_mod_path(self):
        assert SideloaderLogService.detect_log_path(AppConfig()) is None


class TestAnalyze:
    def test_statuses_and_version_badge(self, tmp_path, db_session):
        """对照本地库：🟢（含 ⬆ 角标条件）/ 🟡 / 🔴 与日志元数据透传"""
        _add_mod(db_session, "g.outdated", ModLocation.GAME, version="1.0")
        _add_mod(db_session, "g.outdated", ModLocation.REPO, version="2.0")
        _add_mod(db_session, "g.repo", ModLocation.REPO)
        _add_mod(db_session, "g.logmissing", ModLocation.GAME)
        _add_mod(db_session, "g.outdated_stale", ModLocation.GAME, version="1.0")

        log = write_log(tmp_path, "\n".join([
            "[Warning:Sideloader] [UAR] WARNING! Outdated mod detected! [g.outdated]  https://a.example",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [g.repo]",
            "[Message:Sideloader] [UAR] WARNING! Missing mod detected! [g.missing][https://missing.example]",
            "[Warning:Sideloader] [UAR] WARNING! Missing mod detected! [g.logmissing]",
            "[Warning:Sideloader] [UAR] WARNING! Outdated mod detected! [g.outdated_stale]",
        ]))

        result, entries = SideloaderLogService().analyze(
            log, db_session, CardService(),
        )
        by_guid = {m.guid: m for m in result.mods}

        # 仓库有新版 → 版本差异角标条件成立（升级入口，F15-13 复用）
        item = by_guid["g.outdated"]
        assert item.status is CardModStatus.INSTALLED
        assert item.repo_version == "2.0"
        assert item.repo_mod_id is not None
        assert entries["g.outdated"].kind is LogEntryKind.OUTDATED

        assert by_guid["g.repo"].status is CardModStatus.IN_REPO
        assert by_guid["g.repo"].mod_id is not None

        # 缺失行保留日志 website 线索（F23-5）
        item = by_guid["g.missing"]
        assert item.status is CardModStatus.MISSING
        assert item.meta_website == "https://missing.example"

        # 日志报缺失但库中已装（多为禁用态 / 日志未更新）→ 按 DB 判 🟢
        assert by_guid["g.logmissing"].status is CardModStatus.INSTALLED

        # 过旧但仓库无更新 → 🟢 无角标（UI 层展示 ⚠过旧）
        item = by_guid["g.outdated_stale"]
        assert item.status is CardModStatus.INSTALLED
        assert item.repo_mod_id is None

        assert result.is_valid
        assert result.total_count == 5

    def test_empty_log_yields_no_mods(self, tmp_path, db_session):
        """仅含干扰行的日志 → 空结果（UI 提示「没有发现缺失」）"""
        noise_only = "\n".join([
            "[Info   :Sideloader] Found 2165 zipmods in directory: D:\\Koikatu\\mods",
            "[Info   :Sideloader] External info: ChaFileFace.headId : HeadMod : bo_head : 1",
            '[Warning:Sideloader] Multiple versions detected, only "x.zipmod" will be loaded.',
            "[Info   : BepInEx] Loading [Restore missing functions 21.3]",
        ])
        result, entries = SideloaderLogService().analyze(
            write_log(tmp_path, noise_only), db_session, CardService(),
        )
        assert result.mods == []
        assert entries == {}
        assert result.is_valid


class TestConfig:
    def test_sideloader_log_path_default_empty(self):
        """默认空 = 自动探测（F23-2），不破坏存量 config.json 加载"""
        assert AppConfig().sideloader_log_path == ""
