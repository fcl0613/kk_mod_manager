"""zipmod 缩略图提取（F12-1）

需求文档 v1.3 §4.13（F12）:
  - F12-1: 若 zipmod 内含 `abdata/*/thumb/*.png` 格式的缩略图，系统应在 Mod 详情面板中展示

提取约定（按优先级）:
  1. abdata/**/thumb/*.png          —— 需求文档规范路径（如 abdata/chara/thumb/[Makise]GS-Alice.png）
  2. abdata/studio_thumbnails/**    —— QuickAccessBox 约定的 Studio 物品缩略图
  3. 与 zipmod 同名 / 名字含 preview|thumbnail|sampl 的图片
  4. 根目录图片
  5. 其他图片条目（兜底）

设计原则: best-effort —— 任何异常（文件缺失 / 非 zip / 加密 / 损坏）一律
返回 None 并记日志，绝不向 UI 抛错。提取结果带 LRU 内存缓存。
"""

import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class ModThumbnail:
    """从 zipmod 中提取出的缩略图"""
    entry_name: str    # zip 内条目路径（尽力修复日文名乱码，仅展示用）
    data: bytes        # 图片原始字节
    score_class: int   # 命中的优先级类别 1~5（测试/调试用）


class ThumbnailService:
    """zipmod 缩略图提取服务（Qt 无关，永不抛异常）"""

    MAX_ENTRY_SIZE = 20 * 1024 * 1024   # 单条目 20MB 上限
    IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
    _CACHE_LIMIT = 64

    # 非候选分数（大于所有合法类别）
    _REJECTED = 99

    def __init__(self):
        # LRU 缓存：键 (路径小写, mtime_ns, size)，值含 None（未命中也缓存，
        # 避免反复扫描无缩略图的大 zipmod）
        self._cache: OrderedDict[tuple, ModThumbnail | None] = OrderedDict()

    # ── 提取 ──

    def extract(self, zipmod_path: Path) -> ModThumbnail | None:
        """从 .zipmod（或 .zipmod_disabled）中提取一张最佳缩略图。

        任何失败均返回 None，不抛异常。
        """
        path = Path(zipmod_path)
        if not path.is_file():
            return None

        try:
            st = path.stat()
            key = (str(path).lower(), st.st_mtime_ns, st.st_size)
        except OSError as e:
            logger.debug("缩略图缓存键获取失败 {}: {}", path.name, e)
            return None

        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        result = self._extract_uncached(path)
        self._cache[key] = result
        if len(self._cache) > self._CACHE_LIMIT:
            self._cache.popitem(last=False)
        return result

    def _extract_uncached(self, path: Path) -> ModThumbnail | None:
        try:
            if not zipfile.is_zipfile(path):
                return None
            with zipfile.ZipFile(path, "r") as zf:
                info = self._select_entry(zf, path.stem)
                if info is None:
                    return None
                return ModThumbnail(
                    entry_name=self._display_name(info),
                    data=zf.read(info),
                    score_class=self._score(info, path.stem),
                )
        except Exception as e:  # noqa: BLE001 —— best-effort，缩略图绝不向 UI 抛错
            logger.debug("缩略图提取失败 {}: {}", path.name, e)
            return None

    def clear_cache(self) -> None:
        """清空内存缓存"""
        self._cache.clear()

    # ── 条目挑选 ──

    def _select_entry(
        self, zf: zipfile.ZipFile, zip_stem: str
    ) -> zipfile.ZipInfo | None:
        """按 (优先级, 文件大小, 文件名) 确定性挑选最佳图片条目。

        同优先级取最小文件 —— 真缩略图体积小，大 PNG 多半是贴图。
        """
        entries = zf.infolist()
        if not entries:
            return None
        best = min(
            entries,
            key=lambda i: (self._score(i, zip_stem), i.file_size, i.filename),
        )
        if self._score(best, zip_stem) >= self._REJECTED:
            return None
        return best

    @staticmethod
    def _score(info: zipfile.ZipInfo, zip_stem: str) -> int:
        """给条目打优先级分，越小越优先；_REJECTED 表示非候选"""
        name = info.filename.replace("\\", "/")
        if info.is_dir() or Path(name).suffix.lower() not in ThumbnailService.IMAGE_EXTS:
            return ThumbnailService._REJECTED
        if not 0 < info.file_size <= ThumbnailService.MAX_ENTRY_SIZE:
            return ThumbnailService._REJECTED

        lower = name.lower()
        if lower.startswith("abdata/") and "/thumb/" in lower:
            return 1
        if "/studio_thumbnails/" in lower or lower.startswith("abdata/studio_thumbnails"):
            return 2
        if (
            Path(lower).stem == zip_stem.lower()
            or "preview" in lower
            or "thumbnail" in lower
            or "sampl" in lower
        ):
            return 3
        if "/" not in name:
            return 4
        return 5

    @staticmethod
    def _display_name(info: zipfile.ZipInfo) -> str:
        """尽力修复日文条目名乱码（仅展示用，不影响路径匹配）。

        无 UTF-8 标志（0x800）的条目名被 zipfile 按 cp437 解码，
        若原始编码是 cp932（日文版压缩工具常见），转回来可恢复可读文本。
        ASCII 路径段两种解码结果一致，匹配逻辑不受影响。
        """
        if info.flag_bits & 0x800:
            return info.filename
        try:
            return info.filename.encode("cp437").decode("cp932")
        except (UnicodeError, LookupError):
            return info.filename
