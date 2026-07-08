"""文件操作工具

实现需求文档 BR-F01~F06 文件操作规则：
  - BR-F01: .zipmod 就是 Mod，安装 = 复制，不解压
  - BR-F02: 卸载/移除 = 删除 .zipmod（或 .zipmod_disabled）
  - BR-F03: 禁用 = 改扩展名，不移动
  - BR-F05: 同名文件重命名规则
  - BR-F06: 冲突时追加序号
"""

from pathlib import Path
import shutil
import os


def ensure_dir(path: Path) -> Path:
    """确保目录存在，不存在则创建"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_copy(
    src: Path,
    dst_dir: Path,
    author: str,
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

    if not target.exists():
        shutil.copy2(src, target)
        return target

    # 同名文件已存在 → 用版本号重命名
    new_name = f"{stem}v{version}{suffix}"
    target = dst_dir / new_name

    # 仍冲突 → 追加序号
    if target.exists():
        counter = 1
        while True:
            new_name = f"{stem}v{version}_{counter}{suffix}"
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
        exclude_names: 要排除的目录名集合

    Returns:
        清理的目录数
    """
    if exclude_names is None:
        exclude_names = set()

    cleaned = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dirpath = Path(dirpath)
        if dirpath == root:
            continue
        if dirpath.name in exclude_names:
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


def compute_repo_relative_path(repo_root: Path, file_path: Path) -> str:
    """计算仓库 Mod 文件相对于仓库根的相对路径（POSIX 风格）。

    Args:
        repo_root: 仓库根目录
        file_path: Mod 文件的绝对路径

    Returns:
        相对于 repo_root 的 POSIX 风格相对路径，如 "Makise/[Makise]GS-Alice.zipmod"
    """
    return Path(file_path).resolve().relative_to(Path(repo_root).resolve()).as_posix()


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
