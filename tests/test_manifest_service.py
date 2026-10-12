"""ManifestService 测试：manifest.xml 解析（含 UTF-16 BOM）、重复检测与版本比较。"""

import pytest

from app.models.mod import Mod, ModLocation
from app.services.manifest_service import (
    DuplicateReport,
    DuplicateResult,
    ManifestParseError,
    ManifestService,
    ModManifest,
)
from tests.conftest import (
    make_bad_file,
    make_raw_zipmod,
    make_zip_without_manifest,
    make_zipmod,
)


@pytest.fixture()
def ms():
    return ManifestService()


# ── parse_from_zipmod ───────────────────────────────────────

class TestParseFromZipmod:
    def test_parse_success_all_fields(self, ms, tmp_path):
        p = make_zipmod(
            tmp_path, "ModA", "guid.a", version="2.1.0", author="Alice",
            description="描述信息", website="https://example.com",
        )
        m = ms.parse_from_zipmod(p)
        assert m.guid == "guid.a"
        assert m.name == "ModA"
        assert m.version == "2.1.0"
        assert m.author == "Alice"
        assert m.description == "描述信息"
        assert m.website == "https://example.com"
        assert str(m) == "ModA v2.1.0 by Alice [guid.a]"

    def test_optional_fields_default_empty(self, ms, tmp_path):
        p = make_zipmod(tmp_path, "ModB", "guid.b", author=None)
        m = ms.parse_from_zipmod(p)
        assert m.author == ""
        assert m.description == ""
        assert m.website == ""
        # 空作者展示为 Unknown（display_author 兜底）
        assert str(m) == "ModB v1.0.0 by Unknown [guid.b]"

    def test_file_not_found(self, ms, tmp_path):
        with pytest.raises(ManifestParseError, match="文件不存在"):
            ms.parse_from_zipmod(tmp_path / "nope.zipmod")

    def test_not_a_zip_file(self, ms, tmp_path):
        p = make_bad_file(tmp_path, "fake.zipmod")
        with pytest.raises(ManifestParseError, match="不是有效的 ZIP"):
            ms.parse_from_zipmod(p)

    def test_missing_manifest_xml(self, ms, tmp_path):
        p = make_zip_without_manifest(tmp_path, "no_manifest.zipmod")
        with pytest.raises(ManifestParseError, match="未找到 manifest.xml"):
            ms.parse_from_zipmod(p)

    def test_empty_guid_rejected(self, ms, tmp_path):
        raw = "<manifest><guid></guid><name>N</name><version>1.0.0</version></manifest>"
        p = make_raw_zipmod(tmp_path, "empty_guid.zipmod", raw.encode("utf-8"))
        with pytest.raises(ManifestParseError, match=r"<guid> 不能为空"):
            ms.parse_from_zipmod(p)

    def test_missing_name_tag_rejected(self, ms, tmp_path):
        raw = "<manifest><guid>g</guid><version>1.0.0</version></manifest>"
        p = make_raw_zipmod(tmp_path, "no_name.zipmod", raw.encode("utf-8"))
        with pytest.raises(ManifestParseError, match=r"缺少必要字段 <name>"):
            ms.parse_from_zipmod(p)

    def test_empty_version_rejected(self, ms, tmp_path):
        raw = "<manifest><guid>g</guid><name>N</name><version>  </version></manifest>"
        p = make_raw_zipmod(tmp_path, "empty_ver.zipmod", raw.encode("utf-8"))
        with pytest.raises(ManifestParseError, match=r"<version> 不能为空"):
            ms.parse_from_zipmod(p)

    def test_malformed_xml_rejected(self, ms, tmp_path):
        raw = "<manifest><guid>g</guid>"  # 未闭合
        p = make_raw_zipmod(tmp_path, "broken.zipmod", raw.encode("utf-8"))
        with pytest.raises(ManifestParseError, match="格式错误"):
            ms.parse_from_zipmod(p)

    def test_field_values_stripped(self, ms, tmp_path):
        raw = ("<manifest><guid> g </guid><name> N </name>"
               "<version> 1.0.0 </version><author> Alice </author></manifest>")
        p = make_raw_zipmod(tmp_path, "spaced.zipmod", raw.encode("utf-8"))
        m = ms.parse_from_zipmod(p)
        assert (m.guid, m.name, m.version, m.author) == ("g", "N", "1.0.0", "Alice")


# ── _decode_manifest（UTF-16 BOM 兼容，commit 69fae4d）──────

class TestDecodeManifest:
    BODY = "<manifest><guid>guid.中文</guid><name>中文名</name><version>1.0.0</version></manifest>"

    def test_utf16_le_bom(self, ms):
        data = b"\xff\xfe" + self.BODY.encode("utf-16-le")
        text = ms._decode_manifest(data, "t.zipmod")
        assert "guid.中文" in text and "中文名" in text

    def test_utf16_be_bom(self, ms):
        data = b"\xfe\xff" + self.BODY.encode("utf-16-be")
        text = ms._decode_manifest(data, "t.zipmod")
        assert "guid.中文" in text

    def test_utf8_bom(self, ms):
        data = b"\xef\xbb\xbf" + self.BODY.encode("utf-8")
        text = ms._decode_manifest(data, "t.zipmod")
        assert "guid.中文" in text

    def test_plain_utf8(self, ms):
        text = ms._decode_manifest(self.BODY.encode("utf-8"), "t.zipmod")
        assert "guid.中文" in text

    def test_undecodable_raises(self, ms):
        with pytest.raises(ManifestParseError, match="编码无法识别"):
            ms._decode_manifest(b"\x81\x82\x83", "t.zipmod")

    @pytest.mark.parametrize("bom,encoding", [
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
        (b"\xef\xbb\xbf", "utf-8"),
    ], ids=["utf16le", "utf16be", "utf8bom"])
    def test_full_parse_via_zipmod(self, ms, tmp_path, bom, encoding):
        """全链路：带 BOM 的 zipmod → parse_from_zipmod 正确解析中文"""
        p = make_raw_zipmod(
            tmp_path, "bom.zipmod", bom + self.BODY.encode(encoding),
        )
        m = ms.parse_from_zipmod(p)
        assert m.guid == "guid.中文"
        assert m.name == "中文名"


# ── ModManifest 属性 ────────────────────────────────────────

class TestManifestProperties:
    def test_author_normalized_and_display(self):
        m = ModManifest(guid="g", name="n", version="1", author=" Alice ")
        assert m.author_normalized == "alice"
        assert m.display_author == "Alice"

    def test_blank_author_falls_back_unknown(self):
        m = ModManifest(guid="g", name="n", version="1", author="")
        assert m.author_normalized == "unknown"
        assert m.display_author == "Unknown"


# ── check_duplicate ─────────────────────────────────────────

def _seed_mod(session, guid="guid.d", version="1.0.0",
              location=ModLocation.GAME, author="Dave") -> Mod:
    mod = Mod(
        guid=guid, name="ModD", version=version, author=author,
        author_normalized=author.lower(), file_name="ModD.zipmod",
        location=location,
    )
    session.add(mod)
    session.commit()
    return mod


def _manifest(guid="guid.d", version="2.0.0") -> ModManifest:
    return ModManifest(guid=guid, name="ModD", version=version, author="Dave")


class TestCheckDuplicate:
    def test_no_duplicate(self, ms, db_session):
        report = ms.check_duplicate(_manifest(), ModLocation.GAME, db_session)
        assert report.result is DuplicateResult.NO_DUPLICATE
        assert report.can_proceed
        assert report.existing_mod is None
        assert report.incoming_version == "2.0.0"

    def test_same_version_blocked(self, ms, db_session):
        _seed_mod(db_session, version="2.0.0")
        report = ms.check_duplicate(_manifest(version="2.0.0"), ModLocation.GAME, db_session)
        assert report.result is DuplicateResult.SAME_VERSION
        assert report.is_blocked
        assert not report.can_proceed and not report.needs_confirmation
        assert report.existing_version == report.incoming_version == "2.0.0"
        assert report.existing_mod is not None

    def test_newer_version_needs_confirmation(self, ms, db_session):
        _seed_mod(db_session, version="1.0.0")
        report = ms.check_duplicate(_manifest(version="2.0.0"), ModLocation.GAME, db_session)
        assert report.result is DuplicateResult.NEWER_VERSION
        assert report.needs_confirmation and not report.is_blocked

    def test_older_version_needs_confirmation(self, ms, db_session):
        _seed_mod(db_session, version="2.0.0")
        report = ms.check_duplicate(_manifest(version="1.0.0"), ModLocation.GAME, db_session)
        assert report.result is DuplicateResult.OLDER_VERSION
        assert report.needs_confirmation

    def test_unknown_version_when_unparseable(self, ms, db_session):
        _seed_mod(db_session, version="abc")
        report = ms.check_duplicate(_manifest(version="1.0.0"), ModLocation.GAME, db_session)
        assert report.result is DuplicateResult.UNKNOWN_VERSION
        assert report.needs_confirmation

    def test_location_isolation(self, ms, db_session):
        """同 GUID 只在 REPO，查 GAME → 不算重复"""
        _seed_mod(db_session, location=ModLocation.REPO)
        report = ms.check_duplicate(_manifest(), ModLocation.GAME, db_session)
        assert report.result is DuplicateResult.NO_DUPLICATE


# ── _compare_versions 与 DuplicateReport 真值表 ─────────────

class TestCompareVersions:
    @pytest.mark.parametrize("incoming,existing,expected", [
        ("2.0.0", "1.0.0", DuplicateResult.NEWER_VERSION),
        ("1.0.0", "2.0.0", DuplicateResult.OLDER_VERSION),
        ("1.0.0", "1.0.0", DuplicateResult.SAME_VERSION),
        ("1.0", "1.0.0", DuplicateResult.SAME_VERSION),      # 语义等价版本
        ("1.0.1", "abc", DuplicateResult.UNKNOWN_VERSION),
        ("abc", "1.0.0", DuplicateResult.UNKNOWN_VERSION),
        ("v2.0", "v1.0", DuplicateResult.NEWER_VERSION),      # v 前缀可解析
    ])
    def test_matrix(self, incoming, existing, expected):
        assert ManifestService._compare_versions(incoming, existing) is expected

    @pytest.mark.parametrize("result,can,confirm,blocked", [
        (DuplicateResult.NO_DUPLICATE, True, False, False),
        (DuplicateResult.SAME_VERSION, False, False, True),
        (DuplicateResult.NEWER_VERSION, False, True, False),
        (DuplicateResult.OLDER_VERSION, False, True, False),
        (DuplicateResult.UNKNOWN_VERSION, False, True, False),
    ])
    def test_duplicate_report_truth_table(self, result, can, confirm, blocked):
        report = DuplicateReport(result=result, incoming_version="1.0.0")
        assert report.can_proceed is can
        assert report.needs_confirmation is confirm
        assert report.is_blocked is blocked
