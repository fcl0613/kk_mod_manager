"""文件操作工具

实现需求文档 BR-F01~F06 文件操作规则：
  - BR-F01: .zipmod 就是 Mod，安装 = 复制，不解压
  - BR-F02: 卸载/移除 = 删除 .zipmod（或 .zipmod_disabled）
  - BR-F03: 禁用 = 改扩展名，不移动
  - BR-F05: 同名文件重命名规则
  - BR-F06: 冲突时追加序号
"""

from pathlib import Path, PurePosixPath
import shutil
import os

from app.utils.version_utils import display_version


def ensure_dir(path: Path) -> Path:
    """确保目录存在，不存在则创建"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_copy(
    src: Path,
    dst_dir: Path,
    file_name: str,
    version: str,
) -> Path:
    """
    安全复制文件到目标目录，自动处理同名冲突。

    规则（BR-F05 / F04-10/11）：
      若目标目录下已存在同名文件但版本号不同，
      新文件重命名为 {原文件名（不含扩展名）}v{版本号}.zipmod

    规则（BR-F06）：
      重命名后仍冲突，追加 _序号

    Returns:
        实际写入的文件路径
    """
    ensure_dir(dst_dir)

    # 解析文件名
    stem = file_name
    suffix = ""
    if file_name.lower().endswith(".zipmod"):
        stem = file_name[:-7]
        suffix = ".zipmod"
    elif file_name.lower().endswith(".zipmod_disabled"):
        stem = file_name[:-16]
        suffix = ".zipmod_disabled"

    target = dst_dir / file_name

    # 源文件已在目标位置（如对仓库目录内的文件执行「添加到仓库」）
    # → 原地生效，避免自拷贝出带版本后缀的副本
    if src.resolve() == target.resolve():
        return target

    if not target.exists():
        shutil.copy2(src, target)
        return target

    # 同名文件已存在 → 用版本号重命名（display_version 保证前缀唯一，
    # 版本号自带 v 前缀时不会出现 "vv" 文件名）
    v = display_version(version)
    new_name = f"{stem}{v}{suffix}"
    target = dst_dir / new_name

    # 仍冲突 → 追加序号
    if target.exists():
        counter = 1
        while True:
            new_name = f"{stem}{v}_{counter}{suffix}"
            target = dst_dir / new_name
            if not target.exists():
                break
            counter += 1

    shutil.copy2(src, target)
    return target


def safe_copy_simple(src: Path, dst: Path) -> Path:
    """简单复制（不重命名），目标存在则覆盖"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def delete_file(path: Path) -> bool:
    """删除文件，不存在时返回 False"""
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def rename_extension(path: Path, old_suffix: str, new_suffix: str) -> Path | None:
    """
    修改文件扩展名（用于启用/禁用）。

    Args:
        path: 文件路径
        old_suffix: 当前扩展名（含点）
        new_suffix: 目标扩展名（含点）

    Returns:
        新文件路径，或 None（重命名失败）
    """
    if not path.exists():
        return None
    new_path = path.with_suffix(new_suffix)
    try:
        path.rename(new_path)
        return new_path
    except OSError:
        return None


def clean_empty_dirs(root: Path, exclude_names: set | None = None) -> int:
    """
    清理空目录（从深到浅遍历）。

    Args:
        root: 根目录
        exclude_names: 要排除的目录名集合（大小写不敏感，BR-CAT06 分类目录保护）

    Returns:
        清理的目录数
    """
    if exclude_names is None:
        exclude_names = set()
    exclude_lower = {str(name).lower() for name in exclude_names}

    cleaned = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dirpath = Path(dirpath)
        if dirpath == root:
            continue
        if dirpath.name.lower() in exclude_lower:
            continue
        # 检查目录是否为空（且无子目录）
        remaining = [d for d in dirpath.iterdir()]
        if not remaining:
            try:
                dirpath.rmdir()
                cleaned += 1
            except OSError:
                pass
    return cleaned


def get_author_dir(base_path: Path, author: str) -> Path:
    """
    根据作者名获取目标目录。

    规则（BR-A02）：作者名匹配忽略大小写，先查找已有目录。
    规则（BR-A03）：作者为空 → "Unknown"。
    规则（BR-A04）：新目录使用规范化后的作者名。

    Args:
        base_path: 游戏 Mod 路径或仓库路径
        author: manifest.xml 中的作者名

    Returns:
        作者目录路径
    """
    author_key = author.strip().lower() if author else "unknown"
    display_name = author.strip() if author else "Unknown"

    # 确保 base_path 存在
    base_path = Path(base_path)
    if not base_path.exists():
        return base_path / display_name

    # 查找已有目录（忽略大小写匹配）
    for child in base_path.iterdir():
        if child.is_dir() and child.name.lower() == author_key:
            return child

    # 不存在 → 使用规范化作者名
    return base_path / display_name


def compute_relative_path(root: Path, file_path: Path) -> str:
    """计算文件相对根目录的相对路径（POSIX 风格，GAME/REPO 通用，F19-6）。

    Args:
        root: 根目录（游戏 Mods 根或仓库根）
        file_path: Mod 文件的绝对路径

    Returns:
        相对于 root 的 POSIX 风格相对路径，如 "ForCharactersLoading/Makise/[Makise]GS-Alice.zipmod"
    """
    return Path(file_path).resolve().relative_to(Path(root).resolve()).as_posix()


def compute_repo_relative_path(repo_root: Path, file_path: Path) -> str:
    """计算仓库 Mod 文件相对于仓库根的相对路径（POSIX 风格）。

    Args:
        repo_root: 仓库根目录
        file_path: Mod 文件的绝对路径

    Returns:
        相对于 repo_root 的 POSIX 风格相对路径，如 "Makise/[Makise]GS-Alice.zipmod"
    """
    return compute_relative_path(repo_root, file_path)


def resolve_repo_mod_path(mod, repo_root: Path) -> Path:
    """解析仓库 Mod 的磁盘绝对路径。

    优先使用 mod.repo_relative_path；为空时回退到旧的"作者目录/文件名"公式。

    Args:
        mod: Mod 实例
        repo_root: 仓库根目录

    Returns:
        Mod 文件的绝对路径
    """
    repo_root = Path(repo_root)
    if mod.repo_relative_path:
        return repo_root / mod.repo_relative_path
    # 回退：兼容老数据（迁移前 + 找不到文件的孤儿记录）
    author_dir = get_author_dir(repo_root, mod.author)
    return author_dir / mod.current_file_name


def resolve_game_mod_path(mod, game_root: Path) -> Path:
    """解析游戏 Mod 的磁盘绝对路径（F19-6，与 resolve_repo_mod_path 对称）。

    优先使用 mod.game_relative_path（basename 按当前启停状态替换为
    mod.current_file_name，兼容 .zipmod ↔ .zipmod_disabled 双向改名）；
    为空时（老数据）回退到"作者目录/文件名"旧公式。

    Args:
        mod: Mod 实例
        game_root: 游戏 Mods 根目录

    Returns:
        Mod 文件的绝对路径
    """
    game_root = Path(game_root)
    if mod.game_relative_path:
        parent = PurePosixPath(mod.game_relative_path).parent
        if str(parent) != ".":
            return game_root / Path(*parent.parts) / mod.current_file_name
        return game_root / mod.current_file_name
    # 回退：兼容老数据（分类安装上线前的记录）
    author_dir = get_author_dir(game_root, mod.author)
    return author_dir / mod.current_file_name


def infer_author_from_path(
    file_path: Path,
    base_path: Path,
    category_names: list[str] | set[str],
) -> str | None:
    """从文件所在目录推断作者名（BR-CAT03 护栏，F19-7）。

    取文件相对根的目录段自底向上找第一个不属于分类清单的段作为作者名；
    全为分类段或文件直躺在根下 → None（归入 Unknown）。
    分类目录名为保留名，不得被推断为作者（BR-CAT05）；
    护栏不受分类开关控制（关闭开关后磁盘上的分类布局仍需正确扫描）。
    比较大小写不敏感（与 get_author_dir 的目录匹配规则一致，BR-CAT01）。

    Args:
        file_path: zipmod 文件路径
        base_path: Mods 根目录
        category_names: 分类目录名清单

    Returns:
        推断出的作者目录名，或 None
    """
    try:
        rel = Path(file_path).resolve().relative_to(Path(base_path).resolve())
    except ValueError:
        return None
    category_lower = {str(c).strip().lower() for c in category_names if str(c).strip()}
    for part in reversed(rel.parts[:-1]):  # 排除文件名，自底向上
        if part.lower() not in category_lower:
            return part
    return None
