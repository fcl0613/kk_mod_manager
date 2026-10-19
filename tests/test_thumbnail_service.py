"""ThumbnailService 单元测试（F12-1）

覆盖：条目优先级启发式、无图/异常 best-effort、LRU 缓存、日文条目名乱码修复。
"""

import zipfile
from pathlib import Path

import pytest

from app.services.thumbnail_service import ThumbnailService
from tests.conftest import make_bad_file, make_png_bytes


def _make_zip_with(
    dir_path: Path,
    stem: str,
    entries: dict[str, bytes],
    name: str | None = None,
) -> Path:
    """造带任意条目的 zipmod（总含 manifest.xml 保持真实结构）"""
    p = Path(dir_path) / (name or f"{stem}.zipmod")
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(
            "manifest.xml",
            f"<manifest><guid>g.{stem}</guid><name>{stem}</name>"
            f"<version>1.0.0</version></manifest>",
        )
        for ename, data in entries.items():
            zf.writestr(ename, data)
    return p


@pytest.fixture
def ts():
    return ThumbnailService()


# ── 条目优先级 ──

def test_priority_thumb_dir_beats_root_preview(ts, tmp_path):
    """abdata/*/thumb/（类别1）优先于根目录 preview.png（类别3）"""
    p = _make_zip_with(tmp_path, "ModA", {
        "preview.png": make_png_bytes(extra=b"x" * 50),
        "abdata/chara/thumb/[Author]Item.png": make_png_bytes(),
    })
    assert ts.extract(p).entry_name == "abdata/chara/thumb/[Author]Item.png"


def test_priority_studio_thumbnails(ts, tmp_path):
    """abdata/studio_thumbnails/（类别2）优先于根目录图片（类别3）"""
    p = _make_zip_with(tmp_path, "ModB", {
        "preview.png": make_png_bytes(extra=b"x" * 50),
        "abdata/studio_thumbnails/items/微缩图.png": make_png_bytes(),
    })
    assert ts.extract(p).entry_name == "abdata/studio_thumbnails/items/微缩图.png"


def test_stem_match_and_keyword_fallback(ts, tmp_path):
    """与 zipmod 同名的图片（类别3）优先于普通嵌套图片（类别5）；preview 关键字亦然"""
    p = _make_zip_with(tmp_path, "ModC", {
        "zzz/other.png": make_png_bytes(extra=b"x" * 50),
        "abdata/list/ModC.png": make_png_bytes(),
    })
    assert ts.extract(p).entry_name == "abdata/list/ModC.png"

    p2 = _make_zip_with(tmp_path, "ModD", {
        "zzz/other.png": make_png_bytes(extra=b"x" * 50),
        "docs/my_preview_image.png": make_png_bytes(),
    })
    assert ts.extract(p2).entry_name == "docs/my_preview_image.png"


def test_prefers_smaller_within_same_class(ts, tmp_path):
    """同优先级取最小文件（真缩略图小，大 PNG 多半是贴图）"""
    p = _make_zip_with(tmp_path, "ModE", {
        "abdata/chara/thumb/big.png": make_png_bytes(extra=b"x" * 500),
        "abdata/chara/thumb/small.png": make_png_bytes(),
    })
    assert ts.extract(p).entry_name == "abdata/chara/thumb/small.png"


# ── 无图 / 非候选 ──

def test_no_images_returns_none(ts, tmp_path):
    p = _make_zip_with(tmp_path, "ModF", {})
    assert ts.extract(p) is None


def test_non_image_extensions_ignored(ts, tmp_path):
    p = _make_zip_with(tmp_path, "ModG", {
        "abdata/chara/thumb/item.unity3d": b"fake-bundle",
        "abdata/list/characustom/item.csv": "id,name\n1,x\n",
        "preview.txt": b"not an image",
    })
    assert ts.extract(p) is None


def test_size_cap_skips_oversized_entry():
    """超过 20MB 上限的条目直接拒绝（单测 _score，不落盘大文件）"""
    zi = zipfile.ZipInfo("abdata/chara/thumb/big.png")
    zi.file_size = 20 * 1024 * 1024 + 1
    assert ThumbnailService._score(zi, "ModH") == ThumbnailService._REJECTED


# ── best-effort：异常一律 None ──

def test_corrupt_file_returns_none(ts, tmp_path):
    p = make_bad_file(tmp_path, "bad.zipmod")
    assert ts.extract(p) is None


def test_not_a_zip_returns_none(ts, tmp_path):
    p = tmp_path / "fake.zipmod"
    p.write_text("plain text", encoding="utf-8")
    assert ts.extract(p) is None


def test_missing_file_returns_none(ts, tmp_path):
    assert ts.extract(tmp_path / "ghost.zipmod") is None


def test_zipmod_disabled_extension_works(ts, tmp_path):
    """".zipmod_disabled 仍是 zip，照常提取"""
    p = _make_zip_with(
        tmp_path, "ModI",
        {"abdata/chara/thumb/item.png": make_png_bytes()},
        name="ModI.zipmod_disabled",
    )
    assert ts.extract(p) is not None


# ── LRU 缓存 ──

def test_cache_hit_same_object_and_invalidation(ts, tmp_path):
    p = _make_zip_with(tmp_path, "ModJ", {
        "abdata/chara/thumb/a.png": make_png_bytes(extra=b"v1"),
    })
    t1 = ts.extract(p)
    t2 = ts.extract(p)
    assert t1 is t2   # 命中缓存，同一对象

    # 改写文件（size 变化 → 键失效）→ 重新提取
    _make_zip_with(tmp_path, "ModJ", {
        "abdata/chara/thumb/a.png": make_png_bytes(extra=b"v2-different"),
    })
    t3 = ts.extract(p)
    assert t3 is not t1
    assert t3.data != t1.data


def test_cache_caches_none_misses(ts, tmp_path, monkeypatch):
    """无图 zipmod 的 None 也缓存，不反复开 zip"""
    p = _make_zip_with(tmp_path, "ModK", {})

    calls = {"n": 0}
    real_zipfile = zipfile.ZipFile

    class CountingZipFile(real_zipfile):
        def __init__(self, *args, **kwargs):
            calls["n"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "app.services.thumbnail_service.zipfile.ZipFile", CountingZipFile
    )
    assert ts.extract(p) is None
    assert ts.extract(p) is None
    assert calls["n"] == 1   # 第二次走缓存，未再打开


# ── 日文条目名乱码修复 ──

def test_mojibake_entry_name_decoded_best_effort():
    """无 UTF-8 标志的 cp932 条目名按 cp437 误解码后可恢复；ASCII 不受影响"""
    raw = "牧瀬スケベ衣装.png".encode("cp932")
    mojibake = raw.decode("cp437")          # zipfile 实际给出的样子

    zi = zipfile.ZipInfo(mojibake)
    zi.flag_bits = 0                        # 无 UTF-8 标志
    assert ThumbnailService._display_name(zi) == "牧瀬スケベ衣装.png"

    zi_utf8 = zipfile.ZipInfo(mojibake)
    zi_utf8.flag_bits = 0x800               # 有 UTF-8 标志 → 原样返回
    assert ThumbnailService._display_name(zi_utf8) == mojibake

    ascii_zi = zipfile.ZipInfo("abdata/chara/thumb/item.png")
    ascii_zi.flag_bits = 0
    assert ThumbnailService._display_name(ascii_zi) == "abdata/chara/thumb/item.png"
