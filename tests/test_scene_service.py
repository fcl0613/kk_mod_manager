"""场景卡解析测试（F17）— _is_scene_card / parse_card 场景分发 / SceneService

不依赖 Qt：卡片页展示逻辑在 UI 层，这里覆盖服务层契约。
"""

from pathlib import Path

from app.services import scene_service
from app.services.card_service import (
    SCENE_KEY,
    CardParseResult,
    CardService,
    GameVersion,
)
from app.services.scene_service import SceneService
from tests.conftest import (
    UAR_KEY,
    StubKC,
    StubScene,
    make_chara_uar_blob,
    make_cloth_tail,
    make_png_bytes,
    make_scene_item_info,
)


def make_scene_png_bytes() -> bytes:
    """最小场景卡字节：PNG + 8 字节跳过 + 【KStudio】 标识"""
    return make_png_bytes(extra=b"\x00" * 8 + SCENE_KEY + b"\x00" * 64)


# ═══════════════════════════════════════════════════════════
# 卡片类型检测
# ═══════════════════════════════════════════════════════════

class TestIsSceneCard:
    def test_marker_present(self):
        assert CardService._is_scene_card(b"\x00" * 8 + SCENE_KEY) is True

    def test_marker_absent(self):
        assert CardService._is_scene_card(b"\x00" * 64) is False

    def test_cloth_tail_not_scene(self):
        # 服装卡尾部不含场景标识，不误判
        assert CardService._is_scene_card(make_cloth_tail([b"a.mod"])) is False

    def test_chara_tail_not_scene(self):
        # 场景内嵌角色数据含【KoiKatuChara】，但角色卡本身不含场景标识
        assert CardService._is_scene_card("【KoiKatuChara】".encode() + b"\x00" * 64) is False


# ═══════════════════════════════════════════════════════════
# parse_card 场景分发（BR-C05：服装 → 场景 → 角色顺序）
# ═══════════════════════════════════════════════════════════

class TestParseCardSceneRouting:
    def test_scene_routed_to_scene_service(self, tmp_path, card_svc, monkeypatch):
        sentinel = CardParseResult(
            card_path=Path("x.png"), card_name="x", card_type="场景卡", is_valid=True,
        )
        calls = []

        class _StubSceneService:
            @staticmethod
            def parse_scene(path, progress=None):
                calls.append(path)
                return sentinel

        monkeypatch.setattr(scene_service, "SceneService", _StubSceneService)
        p = tmp_path / "s.png"
        p.write_bytes(make_scene_png_bytes())

        result = card_svc.parse_card(p)
        assert result is sentinel
        assert calls == [p]

    def test_scene_resets_kc(self, tmp_path, card_svc, fake_kc, monkeypatch):
        """先解析角色卡再解析场景卡：kc 必须清空（转卡脏数据防护）"""
        kc = fake_kc(StubKC({"Parameter": {"nickname": "雪", "version": "0.0.5"}}))

        chara_png = tmp_path / "c.png"
        chara_png.write_bytes(make_png_bytes(extra=b"\x00" * 16))
        card_svc.parse_card(chara_png)
        assert card_svc.kc is kc

        class _StubSceneService:
            @staticmethod
            def parse_scene(path, progress=None):
                return CardParseResult(
                    card_path=path, card_name=path.name, card_type="场景卡", is_valid=True,
                )

        monkeypatch.setattr(scene_service, "SceneService", _StubSceneService)
        scene_png = tmp_path / "s.png"
        scene_png.write_bytes(make_scene_png_bytes())
        card_svc.parse_card(scene_png)
        assert card_svc.kc is None

    def test_full_flow(self, tmp_path, card_svc, fake_scene):
        """端到端：场景 PNG → parse_card → SceneService 真实解析"""
        kc = StubKC({
            "Parameter": {"nickname": "雪", "version": "0.0.5"},
            "KKEx": {UAR_KEY: [0, {"info": [
                make_chara_uar_blob("chara.mod", name="CN", author="CA", website="CW"),
            ]}]},
        })
        sd = StubScene(
            version="1.0.4.2", map_id=5,
            mod_data={UAR_KEY: [0, {
                "itemInfo": [make_scene_item_info("scene.mod", name="SN", author="SA", website="SW")],
                "mapInfoGUID": "map.mod",
            }]},
            characters=[kc],
            counts={"Character": 1, "Item": 3},
        )
        fake_scene(sd)
        p = tmp_path / "scene.png"
        p.write_bytes(make_scene_png_bytes())

        result = card_svc.parse_card(p)
        assert result.card_type == "场景卡"
        assert result.is_valid
        assert result.game_version is None  # 场景卡不触发转卡按钮（F17-12）
        assert result.scene_summary is not None
        assert result.scene_summary.game_version == GameVersion.KK  # 来自内嵌角色
        assert {m.guid for m in result.mods} == {"chara.mod", "scene.mod", "map.mod"}


# ═══════════════════════════════════════════════════════════
# SceneService.parse_scene
# ═══════════════════════════════════════════════════════════

class TestParseScene:
    def test_summary_fields(self, fake_scene):
        fake_scene(StubScene(
            version="1.0.4.2", map_id=42,
            mod_data={"Timeline": {}},  # 插件名大小写不固定
            counts={"Character": 2, "Item": 7},
        ))
        r = SceneService.parse_scene(Path("s.png"))
        assert r.is_valid
        s = r.scene_summary
        assert s.version == "1.0.4.2"
        assert s.character_count == 2
        assert s.item_count == 7
        assert s.map_id == 42
        assert s.has_timeline is True

    def test_summary_defaults(self, fake_scene):
        """version=None / 无 map：字段回退默认值而不崩溃"""
        fake_scene(StubScene(version=None, map_id=-1))
        r = SceneService.parse_scene(Path("s.png"))
        assert r.is_valid
        s = r.scene_summary
        assert s.version == ""
        assert s.map_id == -1
        assert s.has_timeline is False
        assert s.character_names == []
        assert s.skipped_characters == []

    def test_item_and_pattern_deps_with_meta(self, fake_scene):
        mod_data = {UAR_KEY: [0, {
            "itemInfo": [make_scene_item_info("a.mod", name="AN", author="AA", website="AW")],
            "patternInfo": [make_scene_item_info("b.mod")],
        }]}
        fake_scene(StubScene(mod_data=mod_data))

        r = SceneService.parse_scene(Path("s.png"))
        by = {m.guid: m for m in r.mods}
        assert set(by) == {"a.mod", "b.mod"}
        assert by["a.mod"].sources == ["场景物品"]
        assert (by["a.mod"].meta_name, by["a.mod"].meta_author, by["a.mod"].meta_website) == ("AN", "AA", "AW")
        assert by["b.mod"].sources == ["场景图案"]
        assert by["b.mod"].meta_name == ""

    def test_string_guid_deps_with_siblings(self, fake_scene):
        mod_data = {UAR_KEY: [0, {
            "mapInfoGUID": "map.mod ",  # 带空白 → strip
            "mapInfoName": "MN", "mapInfoAuthor": "MA", "mapInfoWebsite": "MW",
            "filterInfoGUID": "filter.mod",
            "rampInfoGUID": "ramp.mod",
            "bgmInfoGUID": "bgm.mod",
        }]}
        fake_scene(StubScene(mod_data=mod_data))

        r = SceneService.parse_scene(Path("s.png"))
        by = {m.guid: m for m in r.mods}
        assert set(by) == {"map.mod", "filter.mod", "ramp.mod", "bgm.mod"}
        assert by["map.mod"].sources == ["地图"]
        assert by["map.mod"].meta_name == "MN"
        assert by["map.mod"].meta_author == "MA"
        assert by["map.mod"].meta_website == "MW"
        assert by["filter.mod"].sources == ["滤镜"]

    def test_empty_string_guid_filtered(self, fake_scene):
        mod_data = {UAR_KEY: [0, {"mapInfoGUID": "  ", "filterInfoGUID": ""}]}
        fake_scene(StubScene(mod_data=mod_data))
        r = SceneService.parse_scene(Path("s.png"))
        assert r.is_valid
        assert r.mods == []

    def test_mod_data_none(self, fake_scene):
        """无 Mod 物品的场景 mod_data 为 None：正常，零依赖"""
        fake_scene(StubScene(mod_data=None))
        r = SceneService.parse_scene(Path("s.png"))
        assert r.is_valid
        assert r.mods == []
        assert r.scene_summary.has_timeline is False

    def test_malformed_uar_tolerated(self, fake_scene):
        for uar in ([0], "junk", [0, "not-a-dict"]):
            fake_scene(StubScene(mod_data={UAR_KEY: uar}))
            r = SceneService.parse_scene(Path("s.png"))
            assert r.is_valid
            assert r.mods == []

    def test_bad_blob_skipped_good_kept(self, fake_scene):
        mod_data = {UAR_KEY: [0, {
            "itemInfo": [b"\xff\xff garbage", make_scene_item_info("good.mod")],
        }]}
        fake_scene(StubScene(mod_data=mod_data))
        r = SceneService.parse_scene(Path("s.png"))
        assert {m.guid for m in r.mods} == {"good.mod"}

    def test_character_deps_reuse_chara_parser(self, fake_scene):
        """内嵌角色复用 parse_character_card，来源带昵称标签"""
        kc = StubKC({
            "Parameter": {"nickname": "雪", "version": "0.0.5"},
            "KKEx": {UAR_KEY: [0, {"info": [
                make_chara_uar_blob("koux.mod", name="KN", author="KA", website="KW"),
            ]}]},
        })
        fake_scene(StubScene(characters=[kc], counts={"Character": 1, "Item": 0}))

        r = SceneService.parse_scene(Path("s.png"))
        assert len(r.mods) == 1
        m = r.mods[0]
        assert m.guid == "koux.mod"
        assert m.sources == ["角色: 雪"]
        assert (m.meta_name, m.meta_author, m.meta_website) == ("KN", "KA", "KW")
        assert r.scene_summary.character_names == ["雪"]
        assert r.scene_summary.game_version == GameVersion.KK

    def test_same_guid_merged_across_sources(self, fake_scene):
        """场景物品与内嵌角色同 GUID：合并为一条、双来源（BR-C07）"""
        kc = StubKC({
            "Parameter": {"nickname": "雪", "version": "0.0.5"},
            "KKEx": {UAR_KEY: [0, {"info": [make_chara_uar_blob("dup.mod")]}]},
        })
        mod_data = {UAR_KEY: [0, {"itemInfo": [
            make_scene_item_info("dup.mod", name="SN", author="SA"),
        ]}]}

        class _BrokenObjScene(StubScene):
            """walk 产出缺少 data.character 的对象 + 正常对象混合"""

            def walk(self, include_depth=False, object_type=None):
                yield 0, {"type": 0, "data": {}}  # 破损对象 → 跳过
                for i, k in enumerate(self._characters):
                    yield i + 1, {"type": 0, "data": {"character": k}}

        fake_scene(_BrokenObjScene(mod_data=mod_data, characters=[kc]))
        r = SceneService.parse_scene(Path("s.png"))
        assert len(r.mods) == 1
        m = r.mods[0]
        assert m.sources == ["场景物品", "角色: 雪"]
        assert (m.meta_name, m.meta_author) == ("SN", "SA")
        assert r.scene_summary.skipped_characters == ["角色1"]
        assert r.scene_summary.character_names == ["雪"]

    def test_blank_nickname_fallback(self, fake_scene):
        """内嵌角色昵称为空串：来源标签回退为序号（真实卡常见）"""
        kc = StubKC({
            "Parameter": {"nickname": "", "version": "0.0.5"},
            "KKEx": {UAR_KEY: [0, {"info": [make_chara_uar_blob("g.mod")]}]},
        })
        fake_scene(StubScene(characters=[kc]))
        r = SceneService.parse_scene(Path("s.png"))
        assert r.mods[0].sources == ["角色: 角色1"]
        assert r.scene_summary.character_names == ["角色1"]

    def test_char_failure_skipped(self, fake_scene):
        """单角色提取阶段异常：跳过并记入 skipped（BR-C08）"""
        class _BoomKC(StubKC):
            def __getitem__(self, key):
                if key == "KKEx":
                    raise RuntimeError("boom")  # 不在 parse_character_card 的容错元组内
                return super().__getitem__(key)

        good = StubKC({
            "Parameter": {"nickname": "好", "version": "0.0.5"},
            "KKEx": {UAR_KEY: [0, {"info": [make_chara_uar_blob("g.mod")]}]},
        })
        boom = _BoomKC({"Parameter": {"nickname": "坏"}})
        fake_scene(StubScene(characters=[good, boom]))

        r = SceneService.parse_scene(Path("s.png"))
        assert {m.guid for m in r.mods} == {"g.mod"}
        assert r.scene_summary.skipped_characters == ["坏"]
        assert r.scene_summary.character_names == ["好"]

    def test_progress_per_character(self, fake_scene):
        """进度回调按角色数汇报：初始 (0,N)，每处理一个 +1（F17-11）"""
        good = StubKC({
            "Parameter": {"nickname": "雪", "version": "0.0.5"},
            "KKEx": {UAR_KEY: [0, {"info": [make_chara_uar_blob("a.mod")]}]},
        })
        broken = StubKC({"Parameter": {"nickname": "坏"}})  # 无 KKEx：无论提取成败都计入进度
        fake_scene(StubScene(characters=[good, broken]))

        events: list[tuple[int, int]] = []
        r = SceneService.parse_scene(
            Path("s.png"), progress=lambda d, t: events.append((d, t)),
        )
        assert r.is_valid
        assert events == [(0, 2), (1, 2), (2, 2)]

    def test_progress_zero_characters(self, fake_scene):
        """无内嵌角色：只回调一次 (0, 0)"""
        fake_scene(StubScene(characters=[]))
        events: list[tuple[int, int]] = []
        SceneService.parse_scene(Path("s.png"), progress=lambda d, t: events.append((d, t)))
        assert events == [(0, 0)]

    def test_load_failure_invalid(self, monkeypatch):
        """kkloader 加载阶段失败：整卡失败 + 友好提示（F17-12）"""

        class _BoomLoader:
            CHARACTER = 0

            @staticmethod
            def load(path):
                raise ValueError("corrupt scene")

        monkeypatch.setattr(scene_service, "KoikatuSceneData", _BoomLoader)
        r = SceneService.parse_scene(Path("x.png"))
        assert r.is_valid is False
        assert r.card_type == "场景卡"
        assert "场景数据解析失败" in r.error_message
        assert r.mods == []

    def test_mods_sorted_by_guid(self, fake_scene):
        mod_data = {UAR_KEY: [0, {"itemInfo": [
            make_scene_item_info(g) for g in ("c.mod", "a.mod", "b.mod")
        ]}]}
        fake_scene(StubScene(mod_data=mod_data))
        r = SceneService.parse_scene(Path("s.png"))
        assert [m.guid for m in r.mods] == ["a.mod", "b.mod", "c.mod"]


# ═══════════════════════════════════════════════════════════
# _merge_guid 合并语义
# ═══════════════════════════════════════════════════════════

class TestMergeGuid:
    def test_new_item_with_meta(self):
        merged = {}
        SceneService._merge_guid(merged, " a.mod ", "场景物品", ("N", "A", "W"))
        m = merged["a.mod"]
        assert m.sources == ["场景物品"]
        assert (m.meta_name, m.meta_author, m.meta_website) == ("N", "A", "W")

    def test_blank_guid_ignored(self):
        merged = {}
        SceneService._merge_guid(merged, "  ", "场景物品")
        SceneService._merge_guid(merged, "!", "场景物品")
        assert merged == {}

    def test_source_accumulate_and_dedup(self):
        merged = {}
        SceneService._merge_guid(merged, "a.mod", "场景物品")
        SceneService._merge_guid(merged, "a.mod", "角色: 雪")
        SceneService._merge_guid(merged, "a.mod", "角色: 雪")  # 重复来源不累积
        assert merged["a.mod"].sources == ["场景物品", "角色: 雪"]

    def test_meta_backfill_first_non_empty(self):
        merged = {}
        SceneService._merge_guid(merged, "a.mod", "场景物品", ("", "SA", ""))
        SceneService._merge_guid(merged, "a.mod", "角色: 雪", ("CN", "CA", "CW"))
        m = merged["a.mod"]
        assert m.meta_name == "CN"  # 空位回填
        assert m.meta_author == "SA"  # 已有值不被覆盖
        assert m.meta_website == "CW"
