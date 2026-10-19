"""CardService 测试：PNG 解析、服装卡/角色卡 GUID 提取、可用性检查、KKS→KK 转换。

字节级 fixture 构造器见 tests/conftest.py（与 card_service 的解析契约逐字节对应）。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.mod import Mod, ModLocation, ModStatus
from app.services.card_service import (
    CHARA_SOURCE_PREFIX,
    CHECK_KEY,
    CardModItem,
    CardModStatus,
    CardParseResult,
    CardService,
    GameVersion,
    STUDIO_SOURCE_TAGS,
)
from tests.conftest import (
    UAR_KEY,
    StubKC,
    make_cloth_tail,
    make_info_blob,
    make_png_bytes,
)


def write_card(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def stub_kc_with_blobs(blobs, version="0.0.5") -> StubKC:
    """构造带 UAR info blob 的角色卡桩数据"""
    return StubKC({
        "Parameter": {"version": version},
        "KKEx": {UAR_KEY: [0, {"info": blobs}]},
    })


# ═══════════════════════════════════════════════════════════
# get_game_version / kks_to_kk
# ═══════════════════════════════════════════════════════════

class TestGetGameVersion:
    def test_kk_version(self):
        kc = StubKC({"Parameter": {"version": "0.0.5"}})
        assert CardService.get_game_version(kc) is GameVersion.KK

    def test_kks_version(self):
        kc = StubKC({"Parameter": {"version": "0.0.6"}})
        assert CardService.get_game_version(kc) is GameVersion.KKS

    def test_unknown_version_treated_as_kks(self):
        kc = StubKC({"Parameter": {"version": "9.9.9"}})
        assert CardService.get_game_version(kc) is GameVersion.KKS

    def test_missing_parameter_returns_kks(self):
        assert CardService.get_game_version(StubKC()) is GameVersion.KKS

    def test_missing_version_key_returns_kks(self):
        kc = StubKC({"Parameter": {}})
        assert CardService.get_game_version(kc) is GameVersion.KKS


class TestKksToKk:
    def _stub(self, version="0.0.6") -> StubKC:
        stub = StubKC({"Parameter": {"version": version}})
        stub.Parameter = SimpleNamespace(version=version)
        return stub

    def test_rewrites_and_saves(self, card_svc, tmp_path):
        stub = self._stub()
        card_svc.kc = stub
        out = tmp_path / "converted.png"

        ok, err = card_svc.kks_to_kk(out)
        assert ok is True and err == ""
        assert stub._data["Parameter"]["version"] == "0.0.5"   # 字典式写入
        assert stub.Parameter.version == "0.0.5"               # 属性式写入
        assert stub.header == b'\xe3\x80\x90KoiKatuChara\xe3\x80\x91'
        stub.save.assert_called_once_with(str(out))

    def test_none_kc_returns_failure(self, card_svc, tmp_path):
        card_svc.kc = None  # 类无 __init__，需显式赋 None
        ok, err = card_svc.kks_to_kk(tmp_path / "out.png")
        assert ok is False
        assert err == "转换失败"

    def test_save_error_returns_failure(self, card_svc, tmp_path):
        stub = self._stub()
        stub.save = MagicMock(side_effect=OSError("disk full"))
        card_svc.kc = stub

        ok, err = card_svc.kks_to_kk(tmp_path / "out.png")
        assert ok is False
        assert err.startswith("转换失败：")


# ═══════════════════════════════════════════════════════════
# PNG 结构 / 服装卡标识
# ═══════════════════════════════════════════════════════════

class TestFindPngEnd:
    def test_minimal_png_offset(self):
        # 签名 8 + IHDR(12+13) + IDAT(12+4) + IEND(12+0)
        assert CardService._find_png_end(make_png_bytes()) == 61

    def test_no_iend_returns_none(self):
        assert CardService._find_png_end(make_png_bytes(with_iend=False)) is None

    def test_truncated_returns_none(self):
        assert CardService._find_png_end(make_png_bytes()[:11]) is None

    def test_extra_chunks_walked(self):
        data = make_png_bytes(b"\x00" * 30)  # IEND 后有附加数据不影响定位
        assert CardService._find_png_end(data) == 61


class TestIsClothCard:
    def test_marker_at_offset8_true(self):
        extra = b"\x00" * 8 + CHECK_KEY + b"whatever"
        assert CardService._is_cloth_card(extra) is True

    def test_too_short_false(self):
        assert CardService._is_cloth_card(b"\x00" * 8 + CHECK_KEY[:-1]) is False

    def test_marker_misaligned_false(self):
        assert CardService._is_cloth_card(b"\x00" * 7 + CHECK_KEY + b"x") is False


# ═══════════════════════════════════════════════════════════
# parse_character_card / parse_cloth_card
# ═══════════════════════════════════════════════════════════

class TestParseCharacterCard:
    def test_extracts_guids_from_blobs(self):
        kc = stub_kc_with_blobs([
            make_info_blob("guid.one"),
            make_info_blob("guid.two"),
        ])
        assert CardService.parse_character_card(kc) == {"guid.one", "guid.two"}

    def test_blob_without_slot_marker_skipped(self):
        kc = stub_kc_with_blobs([
            make_info_blob("guid.good"),
            make_info_blob("guid.bad", with_slot=False),
        ])
        assert CardService.parse_character_card(kc) == {"guid.good"}

    def test_missing_uar_key_returns_empty(self):
        kc = StubKC({"KKEx": {}})  # 无 universalautoresolver 键
        assert CardService.parse_character_card(kc) == set()

    def test_no_kkex_returns_empty(self):
        assert CardService.parse_character_card(StubKC()) == set()


class TestParseClothCard:
    def test_extracts_guids(self, tmp_path):
        p = write_card(tmp_path, "cloth.png",
                       make_png_bytes(make_cloth_tail([b"guid.one", b"guid.two"])))
        assert CardService.parse_cloth_card(p) == {"guid.one", "guid.two"}

    def test_stop_tag_truncates_matching(self, tmp_path):
        """STOP_TAG（additionalAccessories）之后的 ModID 不计入"""
        p = write_card(tmp_path, "cloth.png",
                       make_png_bytes(make_cloth_tail([b"guid.real"])))
        result = CardService.parse_cloth_card(p)
        assert "guid.real" in result
        assert "after_stop_tag" not in result and "ModID.after_stop_tag" not in result

    def test_no_stop_tag_matches_to_end(self, tmp_path):
        p = write_card(tmp_path, "cloth.png",
                       make_png_bytes(make_cloth_tail([b"a.b"], stop_tag=False)))
        assert CardService.parse_cloth_card(p) == {"a.b"}

    @pytest.mark.parametrize("name,data", [
        ("not_png.txt", make_png_bytes(make_cloth_tail([b"g"]))),          # 非 .png 后缀
        ("bad_sig.png", b"XXXX" + make_png_bytes(make_cloth_tail([b"g"]))[4:]),  # 坏签名
        ("no_iend.png", make_png_bytes(make_cloth_tail([b"g"]), with_iend=False)),
        ("too_short.png", make_png_bytes(b"\x00" * 5)),                    # extra 过短
        ("not_cloth.png", make_png_bytes(b"\x00" * 40)),                   # 无 KoiKatuClothes 标识
    ], ids=["suffix", "signature", "no_iend", "short_extra", "no_marker"])
    def test_invalid_inputs_return_empty(self, tmp_path, name, data):
        p = write_card(tmp_path, name, data)
        assert CardService.parse_cloth_card(p) == set()


# ═══════════════════════════════════════════════════════════
# parse_card（全流程）
# ═══════════════════════════════════════════════════════════

class TestParseCard:
    def test_file_missing(self, card_svc, tmp_path):
        r = card_svc.parse_card(tmp_path / "ghost.png")
        assert not r.is_valid
        assert "文件不存在" in r.error_message
        assert r.card_type == "unknown"

    def test_non_png_suffix_rejected(self, card_svc, tmp_path):
        p = write_card(tmp_path, "card.jpg", make_png_bytes())
        r = card_svc.parse_card(p)
        assert not r.is_valid
        assert "必须是 PNG 格式" in r.error_message

    def test_bad_signature_rejected(self, card_svc, tmp_path):
        p = write_card(tmp_path, "card.png", b"NOT_PNG" + b"\x00" * 20)
        r = card_svc.parse_card(p)
        assert not r.is_valid
        assert "不是有效的 PNG 格式" in r.error_message

    def test_no_iend_rejected(self, card_svc, tmp_path):
        p = write_card(tmp_path, "card.png", make_png_bytes(with_iend=False))
        r = card_svc.parse_card(p)
        assert not r.is_valid
        assert "无法解析 PNG 文件结构" in r.error_message

    def test_cloth_card_full_flow(self, card_svc, tmp_path):
        p = write_card(tmp_path, "cloth.png",
                       make_png_bytes(make_cloth_tail([b"guid.two", b"guid.one"])))
        r = card_svc.parse_card(p)

        assert r.is_valid and r.error_message == ""
        assert r.card_type == "服装卡"
        assert r.game_version is GameVersion.KK      # 服装卡固定按 KK 处理
        assert card_svc.kc is None
        assert [m.guid for m in r.mods] == ["guid.one", "guid.two"]  # sorted
        assert all(m.status is CardModStatus.MISSING for m in r.mods)
        assert r.total_count == 2 and r.missing_count == 2

    def test_cloth_guids_stripped(self, card_svc, tmp_path):
        """GUID 前后空格 / 引号在 parse_card 层被 strip 清洗"""
        p = write_card(tmp_path, "cloth.png", make_png_bytes(make_cloth_tail(
            [b" guid.x ", b"'guid.y'"],
        )))
        r = card_svc.parse_card(p)
        assert {m.guid for m in r.mods} == {"guid.x", "guid.y"}

    def test_cloth_card_no_mods_valid_empty(self, card_svc, tmp_path):
        p = write_card(tmp_path, "cloth.png",
                       make_png_bytes(make_cloth_tail([])))
        r = card_svc.parse_card(p)
        assert r.is_valid
        assert r.mods == [] and r.total_count == 0

    def test_character_card_branch(self, card_svc, tmp_path, fake_kc):
        payload = fake_kc(stub_kc_with_blobs(
            [make_info_blob(" guid.char "), make_info_blob("guid.char2")],
            version="0.0.5",
        ))
        p = write_card(tmp_path, "char.png", make_png_bytes(b"\x00" * 40))

        r = card_svc.parse_card(p)
        assert r.is_valid
        assert r.card_type == "角色卡"
        assert r.game_version is GameVersion.KK
        assert {m.guid for m in r.mods} == {"guid.char", "guid.char2"}
        assert card_svc.kc is payload


# ═══════════════════════════════════════════════════════════
# check_mod_availability
# ═══════════════════════════════════════════════════════════

def _result_with(guids) -> CardParseResult:
    from app.services.card_service import CardModItem
    return CardParseResult(
        card_path=None, card_name="x.png", card_type="服装卡",
        is_valid=True,
        mods=[CardModItem(guid=g, status=CardModStatus.MISSING) for g in guids],
    )


class TestCheckModAvailability:
    def test_three_statuses_and_field_fill(self, card_svc, db_session):
        def seed(guid, location, name, author, version):
            db_session.add(Mod(
                guid=guid, name=name, version=version, author=author,
                author_normalized=author.lower(), file_name=f"{name}.zipmod",
                location=location, status=ModStatus.ENABLED,
            ))
        seed("g.game", ModLocation.GAME, "GameMod", "Gina", "1.1.0")
        seed("g.repo", ModLocation.REPO, "RepoMod", "Ryan", "2.2.0")
        db_session.commit()

        result = _result_with(["g.game", "g.repo", "g.missing"])
        result = card_svc.check_mod_availability(result, db_session)
        by_guid = {m.guid: m for m in result.mods}

        game_item = by_guid["g.game"]
        assert game_item.status is CardModStatus.INSTALLED
        assert game_item.mod_name == "GameMod"
        assert game_item.mod_version == "1.1.0"
        assert game_item.mod_author == "Gina"
        assert game_item.mod_id is not None
        assert game_item.location == "game"

        repo_item = by_guid["g.repo"]
        assert repo_item.status is CardModStatus.IN_REPO
        assert repo_item.location == "repo"

        assert by_guid["g.missing"].status is CardModStatus.MISSING
        assert by_guid["g.missing"].mod_name == ""

        assert result.installed_count == 1
        assert result.in_repo_count == 1
        assert result.missing_count == 1
        assert result.total_count == 3

    def test_game_takes_priority_over_repo(self, card_svc, db_session):
        """同 GUID 两端都有 → 优先标 INSTALLED（BR-C03 ①先查游戏）"""
        for loc in (ModLocation.GAME, ModLocation.REPO):
            db_session.add(Mod(
                guid="g.both", name="Both", version="1.0.0", author="A",
                author_normalized="a", file_name="Both.zipmod", location=loc,
                status=ModStatus.ENABLED,
            ))
        db_session.commit()

        result = card_svc.check_mod_availability(
            _result_with(["g.both"]), db_session,
        )
        assert result.mods[0].status is CardModStatus.INSTALLED
        assert result.mods[0].location == "game"

    def test_empty_mods_all_zero(self, card_svc, db_session):
        result = card_svc.check_mod_availability(_result_with([]), db_session)
        assert (result.total_count, result.installed_count,
                result.in_repo_count, result.missing_count) == (0, 0, 0, 0)

    # ── 版本差异角标（F15-12 / Q-04）──

    @staticmethod
    def _seed_both_locations(db_session, guid, game_ver, repo_ver):
        """同 GUID 在 GAME / REPO 各播一行"""
        for loc, ver in ((ModLocation.GAME, game_ver), (ModLocation.REPO, repo_ver)):
            db_session.add(Mod(
                guid=guid, name="Both", version=ver, author="A",
                author_normalized="a", file_name=f"Both_{loc.value}.zipmod",
                location=loc, status=ModStatus.ENABLED,
            ))
        db_session.commit()

    def test_repo_newer_marks_version_badge(self, card_svc, db_session):
        """游戏旧版 + 仓库新版 → 仍 INSTALLED 但带角标（F15-12）"""
        self._seed_both_locations(db_session, "g.diff", "1.0.0", "2.0.0")

        result = card_svc.check_mod_availability(
            _result_with(["g.diff"]), db_session,
        )
        item = result.mods[0]
        assert item.status is CardModStatus.INSTALLED  # 状态仍是 🟢，仅角标
        assert item.location == "game"
        assert item.has_newer_in_repo
        assert item.repo_version == "2.0.0"
        assert item.repo_mod_id is not None
        assert result.installed_count == 1  # 计数口径不变（不挤占 🟡）

    def test_repo_same_or_older_no_badge(self, card_svc, db_session):
        """同版本 / 仓库更旧 → 不标角标"""
        self._seed_both_locations(db_session, "g.same", "1.0.0", "1.0.0")
        self._seed_both_locations(db_session, "g.older", "2.0.0", "1.0.0")

        result = card_svc.check_mod_availability(
            _result_with(["g.same", "g.older"]), db_session,
        )
        by_guid = {m.guid: m for m in result.mods}
        assert by_guid["g.same"].status is CardModStatus.INSTALLED
        assert not by_guid["g.same"].has_newer_in_repo
        assert by_guid["g.same"].repo_mod_id is None
        assert not by_guid["g.older"].has_newer_in_repo
        assert by_guid["g.older"].repo_mod_id is None

    def test_unparseable_version_no_badge(self, card_svc, db_session):
        """版本无法解析（BR-V03）→ 保守不标角标，不误导"""
        self._seed_both_locations(db_session, "g.weird", "1.0.0", "v2?final")

        result = card_svc.check_mod_availability(
            _result_with(["g.weird"]), db_session,
        )
        item = result.mods[0]
        assert item.status is CardModStatus.INSTALLED
        assert not item.has_newer_in_repo
        assert item.repo_mod_id is None


# ═══════════════════════════════════════════════════════════
# 依赖粗分类型（F17-13）
# ═══════════════════════════════════════════════════════════

class TestDepType:
    @staticmethod
    def _item(sources: list[str]) -> CardModItem:
        return CardModItem(guid="g", status=CardModStatus.MISSING, sources=sources)

    def test_empty_sources_is_chara(self):
        """空来源（人物/服装卡）→ 角色（F19-3 卡片视角）"""
        assert self._item([]).dep_type == "角色"

    def test_chara_sources_only(self):
        assert self._item(["角色: 雪", "角色: 角色3"]).dep_type == "角色"

    def test_studio_sources_only(self):
        assert self._item(["场景物品", "地图"]).dep_type == "工作室"

    def test_mixed_sources(self):
        assert self._item(["场景物品", "角色: 雪"]).dep_type == "角色+工作室"
