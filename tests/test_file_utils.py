"""file_utils 纯文件操作工具测试（BR-F01~F06 / BR-A02~A04）。"""

from pathlib import Path

from app.models.mod import Mod, ModLocation, ModStatus
from app.utils.file_utils import (
    clean_empty_dirs,
    compute_relative_path,
    compute_repo_relative_path,
    delete_file,
    ensure_dir,
    get_author_dir,
    infer_author_from_path,
    rename_extension,
    resolve_game_mod_path,
    resolve_repo_mod_path,
    safe_copy,
    safe_copy_simple,
)


def _touch(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ── ensure_dir ──────────────────────────────────────────────

def test_ensure_dir_nested_and_idempotent(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    result = ensure_dir(target)
    assert result == target
    assert target.is_dir()
    # 重复调用不抛错
    assert ensure_dir(target) == target


# ── safe_copy ───────────────────────────────────────────────

def test_safe_copy_no_conflict(tmp_path):
    src = _touch(tmp_path / "src" / "ModA.zipmod", "content-a")
    dst_dir = tmp_path / "dst"
    result = safe_copy(src, dst_dir, "ModA.zipmod", "1.0.0")
    assert result == dst_dir / "ModA.zipmod"
    assert result.read_text(encoding="utf-8") == "content-a"


def test_safe_copy_conflict_renames_with_version(tmp_path):
    src = _touch(tmp_path / "src" / "ModA.zipmod", "new")
    dst_dir = tmp_path / "dst"
    _touch(dst_dir / "ModA.zipmod", "old")
    result = safe_copy(src, dst_dir, "ModA.zipmod", "2.0.0")
    assert result.name == "ModAv2.0.0.zipmod"
    # 原文件不被覆盖
    assert (dst_dir / "ModA.zipmod").read_text(encoding="utf-8") == "old"
    assert result.read_text(encoding="utf-8") == "new"


def test_safe_copy_v_prefixed_version_no_double_v(tmp_path):
    """manifest 版本自带 v 前缀 → 改名文件不出现 "vv"（display_version 归一）"""
    src = _touch(tmp_path / "src" / "ModA.zipmod", "new")
    dst_dir = tmp_path / "dst"
    _touch(dst_dir / "ModA.zipmod", "old")
    result = safe_copy(src, dst_dir, "ModA.zipmod", "v2.0.0")
    assert result.name == "ModAv2.0.0.zipmod"


def test_safe_copy_conflict_counter_chain(tmp_path):
    src = _touch(tmp_path / "src" / "ModA.zipmod", "new")
    dst_dir = tmp_path / "dst"
    _touch(dst_dir / "ModA.zipmod")
    _touch(dst_dir / "ModAv2.0.0.zipmod")
    _touch(dst_dir / "ModAv2.0.0_1.zipmod")
    result = safe_copy(src, dst_dir, "ModA.zipmod", "2.0.0")
    assert result.name == "ModAv2.0.0_2.zipmod"


def test_safe_copy_disabled_suffix_preserved(tmp_path):
    src = _touch(tmp_path / "src" / "ModB.zipmod", "new")
    dst_dir = tmp_path / "dst"
    _touch(dst_dir / "ModB.zipmod_disabled")
    result = safe_copy(src, dst_dir, "ModB.zipmod_disabled", "1.5.0")
    assert result.name == "ModBv1.5.0.zipmod_disabled"


def test_safe_copy_non_zipmod_name_appends_version(tmp_path):
    """非 zipmod 文件名：stem 取整个文件名、suffix 为空（现状行为）"""
    src = _touch(tmp_path / "src" / "card.png", "new")
    dst_dir = tmp_path / "dst"
    _touch(dst_dir / "card.png", "old")
    result = safe_copy(src, dst_dir, "card.png", "2.0.0")
    assert result.name == "card.pngv2.0.0"


def test_safe_copy_uppercase_zipmod_suffix(tmp_path):
    """大写 .ZIPMOD 后缀同样按 zipmod 解析（lower() 判断，suffix 归一为小写）"""
    src = _touch(tmp_path / "src" / "ModC.zipmod", "new")
    dst_dir = tmp_path / "dst"
    _touch(dst_dir / "ModC.ZIPMOD", "old")
    result = safe_copy(src, dst_dir, "ModC.ZIPMOD", "1.0.0")
    assert result.name == "ModCv1.0.0.zipmod"


def test_safe_copy_src_already_in_place(tmp_path):
    """源文件已在目标位置（对仓库目录内的文件执行「添加到仓库」）→ 原地生效

    不自拷贝：修复前会产出 "{文件名}v{版本}.zipmod" 副本与源文件并存。
    """
    src = _touch(tmp_path / "ModA.zipmod", "same")
    result = safe_copy(src, tmp_path, "ModA.zipmod", "2.0.0")
    assert result == src
    assert list(tmp_path.glob("*.zipmod")) == [src]


# ── safe_copy_simple / delete_file ──────────────────────────

def test_safe_copy_simple_overwrites_and_creates_parent(tmp_path):
    src = _touch(tmp_path / "src.bin", "new")
    dst = tmp_path / "deep" / "nested" / "dst.bin"
    _touch(dst, "old")
    result = safe_copy_simple(src, dst)
    assert result == dst
    assert dst.read_text(encoding="utf-8") == "new"


def test_delete_file_true_then_false(tmp_path):
    p = _touch(tmp_path / "gone.txt")
    assert delete_file(p) is True
    assert not p.exists()
    assert delete_file(p) is False


# ── rename_extension ────────────────────────────────────────

def test_rename_extension_round_trip(tmp_path):
    p = _touch(tmp_path / "ModA.zipmod", "data")
    disabled = rename_extension(p, ".zipmod", ".zipmod_disabled")
    assert disabled == tmp_path / "ModA.zipmod_disabled"
    assert disabled.exists() and not p.exists()
    assert disabled.read_text(encoding="utf-8") == "data"

    enabled = rename_extension(disabled, ".zipmod_disabled", ".zipmod")
    assert enabled == tmp_path / "ModA.zipmod"
    assert enabled.exists() and not disabled.exists()


def test_rename_extension_missing_returns_none(tmp_path):
    assert rename_extension(tmp_path / "nope.zipmod", ".zipmod", ".zipmod_disabled") is None


# ── clean_empty_dirs ────────────────────────────────────────
# 注意：autouse 的 dirs fixture 会在 tmp_path 下建空的 game/ 与 repo/，
# 因此这里统一用独立的 work 目录作为清理根，避免断言被污染。

def test_clean_empty_dirs_bottom_up(tmp_path):
    root = tmp_path / "work"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    cleaned = clean_empty_dirs(root)
    assert cleaned == 3
    assert not (root / "a").exists()
    assert root.is_dir()  # 根目录本身不动


def test_clean_empty_dirs_keeps_nonempty_levels(tmp_path):
    root = tmp_path / "work"
    keep_file = root / "a" / "b" / "file.txt"
    _touch(keep_file)
    (root / "a" / "empty").mkdir()
    cleaned = clean_empty_dirs(root)
    assert cleaned == 1
    assert keep_file.exists()
    assert (root / "a" / "b").is_dir()
    assert not (root / "a" / "empty").exists()


def test_clean_empty_dirs_excludes_names(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "keep").mkdir()
    (root / "gone").mkdir()
    cleaned = clean_empty_dirs(root, exclude_names={"keep"})
    assert cleaned == 1
    assert (root / "keep").is_dir()
    assert not (root / "gone").exists()


def test_clean_empty_dirs_root_skipped(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    assert clean_empty_dirs(root) == 0
    assert root.is_dir()


# ── get_author_dir ──────────────────────────────────────────

def test_get_author_dir_reuses_case_insensitive(tmp_path):
    existing = tmp_path / "ALICE"
    existing.mkdir()
    assert get_author_dir(tmp_path, "alice") == existing
    assert get_author_dir(tmp_path, "Alice") == existing


def test_get_author_dir_blank_uses_unknown(tmp_path):
    # 空串（falsy）→ Unknown；纯空白串 strip 后为空、joinpath 忽略空段 → base 本身。
    # 生产路径中 manifest 解析已 strip，纯空白不会传到这里，仅记录现状行为。
    assert get_author_dir(tmp_path, "") == tmp_path / "Unknown"
    assert get_author_dir(tmp_path, "   ") == tmp_path


def test_get_author_dir_base_missing_no_create(tmp_path):
    base = tmp_path / "not_exist"
    result = get_author_dir(base, "Bob")
    assert result == base / "Bob"
    assert not base.exists()  # 不创建目录


def test_get_author_dir_new_author_path(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    assert get_author_dir(tmp_path, "Carol") == tmp_path / "Carol"


def test_get_author_dir_ignores_same_name_file(tmp_path):
    (tmp_path / "alice").write_text("我是一个文件不是目录", encoding="utf-8")
    assert get_author_dir(tmp_path, "Alice") == tmp_path / "Alice"


# ── compute_repo_relative_path / resolve_repo_mod_path ──────

def test_compute_repo_relative_path_posix(tmp_path):
    repo_root = tmp_path / "repo"
    f = _touch(repo_root / "Author" / "sub" / "mod.zipmod")
    assert compute_repo_relative_path(repo_root, f) == "Author/sub/mod.zipmod"


def test_compute_repo_relative_path_outside_raises(tmp_path):
    # 用独立目录名，避开 dirs fixture 已建的 repo/
    repo_root = tmp_path / "warehouse"
    repo_root.mkdir()
    outside = _touch(tmp_path / "outside.zipmod")
    try:
        compute_repo_relative_path(repo_root, outside)
        raise AssertionError("文件在仓库外应抛 ValueError")
    except ValueError:
        pass


def _repo_mod(**kwargs) -> Mod:
    """构造用于路径解析的 Mod 行（不入库，只读属性）"""
    base = dict(
        guid="g", name="n", version="1.0.0", author="Xavier",
        file_name="mod.zipmod", location=ModLocation.REPO,
        status=ModStatus.ENABLED, repo_relative_path="",
    )
    base.update(kwargs)
    return Mod(**base)


def test_resolve_repo_mod_path_prefers_relative(tmp_path):
    mod = _repo_mod(repo_relative_path="Deep/Dir/mod.zipmod")
    assert resolve_repo_mod_path(mod, tmp_path) == tmp_path / "Deep" / "Dir" / "mod.zipmod"


def test_resolve_repo_mod_path_fallback_author_formula(tmp_path):
    mod = _repo_mod(repo_relative_path="")
    assert resolve_repo_mod_path(mod, tmp_path) == tmp_path / "Xavier" / "mod.zipmod"


def test_resolve_repo_mod_path_fallback_disabled_file(tmp_path):
    """回退公式取 current_file_name：DISABLED 行应得到 _disabled 文件名"""
    mod = _repo_mod(file_name="mod.zipmod", status=ModStatus.DISABLED)
    assert resolve_repo_mod_path(mod, tmp_path) == tmp_path / "Xavier" / "mod.zipmod_disabled"


# ── compute_relative_path / resolve_game_mod_path（F19-6）──

def test_compute_relative_path_posix(tmp_path):
    root = tmp_path / "mods"
    f = _touch(root / "ForCharactersLoading" / "Alice" / "mod.zipmod")
    assert compute_relative_path(root, f) == "ForCharactersLoading/Alice/mod.zipmod"


def _game_mod(**kwargs) -> Mod:
    """构造用于 GAME 端路径解析的 Mod 行（不入库，只读属性）"""
    base = dict(
        guid="g", name="n", version="1.0.0", author="Alice",
        file_name="mod.zipmod", location=ModLocation.GAME,
        status=ModStatus.ENABLED, game_relative_path="",
    )
    base.update(kwargs)
    return Mod(**base)


def test_resolve_game_mod_path_prefers_relative(tmp_path):
    mod = _game_mod(game_relative_path="ForStudioScenesLoading/Alice/mod.zipmod")
    assert (
        resolve_game_mod_path(mod, tmp_path)
        == tmp_path / "ForStudioScenesLoading" / "Alice" / "mod.zipmod"
    )


def test_resolve_game_mod_path_swaps_basename_by_status(tmp_path):
    """rel path 存启停前的启用名，定位时 basename 按当前状态替换（F19-6）"""
    mod = _game_mod(game_relative_path="Cat/Alice/mod.zipmod")
    assert resolve_game_mod_path(mod, tmp_path).name == "mod.zipmod"

    disabled = _game_mod(
        game_relative_path="Cat/Alice/mod.zipmod",
        file_name="mod.zipmod", status=ModStatus.DISABLED,
    )
    assert resolve_game_mod_path(disabled, tmp_path).name == "mod.zipmod_disabled"


def test_resolve_game_mod_path_rel_without_dir(tmp_path):
    """文件直躺 Mods 根：rel path 只有文件名"""
    mod = _game_mod(game_relative_path="mod.zipmod")
    assert resolve_game_mod_path(mod, tmp_path) == tmp_path / "mod.zipmod"


def test_resolve_game_mod_path_fallback_author_formula(tmp_path):
    """老数据（rel path 为空）回退 {作者}/文件名 公式"""
    mod = _game_mod(game_relative_path="")
    assert resolve_game_mod_path(mod, tmp_path) == tmp_path / "Alice" / "mod.zipmod"


def test_resolve_game_mod_path_fallback_reuses_existing_dir_case(tmp_path):
    """回退公式经 get_author_dir：大小写复用磁盘已有目录"""
    existing = tmp_path / "ALICE"
    existing.mkdir()
    mod = _game_mod(game_relative_path="")
    assert resolve_game_mod_path(mod, tmp_path) == existing / "mod.zipmod"


# ── infer_author_from_path（BR-CAT03 护栏，F19-7）────────────

_CATEGORIES = ["AdditionalPoses", "ForCharactersLoading", "MyMods"]


def test_infer_author_skips_category_segments(tmp_path):
    """{分类}/{作者}/x.zipmod → 作者段（跳过分类）"""
    f = tmp_path / "ForCharactersLoading" / "Zelwent" / "x.zipmod"
    assert infer_author_from_path(f, tmp_path, _CATEGORIES) == "Zelwent"


def test_infer_author_bottom_up_prefers_deepest(tmp_path):
    """{分类}/{嵌套段}/…：自底向上取第一个非分类段"""
    f = tmp_path / "ForCharactersLoading" / "Deep" / "Nest" / "x.zipmod"
    assert infer_author_from_path(f, tmp_path, _CATEGORIES) == "Nest"


def test_infer_author_all_category_segments_none(tmp_path):
    """全为分类段（如文件直躺分类目录）→ None（归 Unknown）"""
    f = tmp_path / "MyMods" / "x.zipmod"
    assert infer_author_from_path(f, tmp_path, _CATEGORIES) is None


def test_infer_author_file_at_root_none(tmp_path):
    """文件直躺 Mods 根 → None"""
    f = tmp_path / "x.zipmod"
    assert infer_author_from_path(f, tmp_path, _CATEGORIES) is None


def test_infer_author_category_case_insensitive(tmp_path):
    """分类段大小写不敏感排除（目录匹配与清单比较一致，BR-CAT01）"""
    f = tmp_path / "formycharsloading" / "Zelwent" / "x.zipmod"
    assert infer_author_from_path(f, tmp_path, _CATEGORIES) == "Zelwent"


def test_infer_author_plain_author_dir(tmp_path):
    """平铺旧布局 {作者}/x.zipmod（作者名不在清单）→ 作者名"""
    f = tmp_path / "Alice" / "x.zipmod"
    assert infer_author_from_path(f, tmp_path, _CATEGORIES) == "Alice"


# ── clean_empty_dirs 排除大小写（BR-CAT06）──────────────────

def test_clean_empty_dirs_exclude_case_insensitive(tmp_path):
    """分类目录名大小写与清单不一致时同样受保护"""
    root = tmp_path / "work"
    root.mkdir()
    (root / "ForCharactersLoading").mkdir()  # 磁盘目录
    (root / "gone").mkdir()
    cleaned = clean_empty_dirs(root, exclude_names={"forcharactersloading"})
    assert cleaned == 1
    assert (root / "ForCharactersLoading").is_dir()
    assert not (root / "gone").exists()
