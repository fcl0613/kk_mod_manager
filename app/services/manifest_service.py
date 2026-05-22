"""manifest.xml 解析与重复检测（F02 / F03）

需求文档 v1.3:
  - F02-1: 读取 .zipmod 内的 manifest.xml
  - F02-2: 提取 guid/name/version/author/description/website
  - F02-3: 无 manifest.xml → 拒绝
  - F02-5: 兼容 UTF-8 BOM
  - F03-1~6: 基于 GUID 的重复检测和版本比较
  - BR-V01~V03: 语义化版本比较，失败则保守处理
"""

import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from packaging.version import Version, InvalidVersion
from sqlalchemy.orm import Session

from app.models.mod import Mod, ModLocation


# ── 数据类 ──

@dataclass
class ModManifest:
    """从 manifest.xml 解析出的 Mod 元数据"""
    guid: str
    name: str
    version: str
    author: str = ""
    description: str = ""
    website: str = ""

    @property
    def author_normalized(self) -> str:
        """规范化作者名（小写），用于目录匹配"""
        return self.author.strip().lower() if self.author else "unknown"

    @property
    def display_author(self) -> str:
        """展示用作者名"""
        return self.author.strip() if self.author else "Unknown"

    def __str__(self) -> str:
        return f"{self.name} v{self.version} by {self.display_author} [{self.guid}]"


# ── 重复检测枚举 ──

class DuplicateResult(Enum):
    """重复检测结果"""
    NO_DUPLICATE = "no_duplicate"     # 未发现重复
    SAME_VERSION = "same_version"     # 同 GUID + 同版本 → 拒绝
    NEWER_VERSION = "newer_version"   # 新版本更高 → 提示升级
    OLDER_VERSION = "older_version"   # 新版本更低 → 提示降级
    UNKNOWN_VERSION = "unknown_version"  # 无法比较版本


@dataclass
class DuplicateReport:
    """重复检测报告"""
    result: DuplicateResult
    existing_mod: Mod | None = None
    existing_version: str = ""
    incoming_version: str = ""

    @property
    def can_proceed(self) -> bool:
        """是否可以直接继续（无需用户确认）"""
        return self.result == DuplicateResult.NO_DUPLICATE

    @property
    def needs_confirmation(self) -> bool:
        """是否需要用户确认"""
        return self.result in (
            DuplicateResult.NEWER_VERSION,
            DuplicateResult.OLDER_VERSION,
            DuplicateResult.UNKNOWN_VERSION,
        )

    @property
    def is_blocked(self) -> bool:
        """是否被完全阻止"""
        return self.result == DuplicateResult.SAME_VERSION


# ── 解析异常 ──

class ManifestParseError(Exception):
    """manifest.xml 解析异常"""
    pass


# ── 解析服务 ──

class ManifestService:
    """manifest.xml 解析与重复检测服务"""

    MANIFEST_FILENAME = "manifest.xml"

    # ── 解析 ──

    def parse_from_zipmod(self, zipmod_path: Path) -> ModManifest:
        """
        从 .zipmod 文件中读取并解析 manifest.xml。

        Raises:
            ManifestParseError: 文件无效或格式错误
        """
        path = Path(zipmod_path)
        if not path.exists():
            raise ManifestParseError(f"文件不存在：{path}")

        if not zipfile.is_zipfile(path):
            raise ManifestParseError(f"不是有效的 ZIP/zipmod 文件：{path.name}")

        with zipfile.ZipFile(path, "r") as zf:
            if self.MANIFEST_FILENAME not in zf.namelist():
                raise ManifestParseError(
                    f"压缩包中未找到 manifest.xml，"
                    f"请确认这是有效的 Sideloader zipmod 文件"
                )
            with zf.open(self.MANIFEST_FILENAME) as f:
                raw = f.read().decode("utf-8-sig")  # 处理 BOM（F02-5）

        return self._parse_xml(raw, path.name)

    def _parse_xml(self, xml_content: str, source_name: str) -> ModManifest:
        """解析 XML 内容为 ModManifest"""
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            raise ManifestParseError(
                f"manifest.xml 格式错误：{e}（来源：{source_name}）"
            )

        def _get_text(tag: str, required: bool = True) -> str:
            elem = root.find(tag)
            if elem is None:
                if required:
                    raise ManifestParseError(
                        f"manifest.xml 缺少必要字段 <{tag}>（来源：{source_name}）"
                    )
                return ""
            return (elem.text or "").strip()

        guid = _get_text("guid", required=True)
        name = _get_text("name", required=True)
        version = _get_text("version", required=True)

        if not guid:
            raise ManifestParseError(
                f"manifest.xml 中 <guid> 不能为空（来源：{source_name}）"
            )
        if not name:
            raise ManifestParseError(
                f"manifest.xml 中 <name> 不能为空（来源：{source_name}）"
            )
        if not version:
            raise ManifestParseError(
                f"manifest.xml 中 <version> 不能为空（来源：{source_name}）"
            )

        return ModManifest(
            guid=guid,
            name=name,
            version=version,
            author=_get_text("author", required=False),
            description=_get_text("description", required=False),
            website=_get_text("website", required=False),
        )

    # ── 重复检测（F03）──

    def check_duplicate(
        self,
        manifest: ModManifest,
        location: ModLocation,
        db_session: Session,
    ) -> DuplicateReport:
        """
        在指定位置检测是否存在相同 GUID 的 Mod。

        Args:
            manifest: 待安装 Mod 的元数据
            location: 目标位置（游戏或仓库）
            db_session: 数据库会话

        Returns:
            DuplicateReport 包含检测结果
        """
        existing: Mod | None = (
            db_session.query(Mod)
            .filter(
                Mod.guid == manifest.guid,
                Mod.location == location,
            )
            .first()
        )

        if existing is None:
            return DuplicateReport(
                result=DuplicateResult.NO_DUPLICATE,
                incoming_version=manifest.version,
            )

        if existing.version == manifest.version:
            return DuplicateReport(
                result=DuplicateResult.SAME_VERSION,
                existing_mod=existing,
                existing_version=existing.version,
                incoming_version=manifest.version,
            )

        # 版本比较
        result = self._compare_versions(manifest.version, existing.version)
        return DuplicateReport(
            result=result,
            existing_mod=existing,
            existing_version=existing.version,
            incoming_version=manifest.version,
        )

    @staticmethod
    def _compare_versions(incoming: str, existing: str) -> DuplicateResult:
        """
        语义化版本比较（BR-V01~V03）。

        优先使用 packaging.version.Version，
        格式不规范时保守处理（标记为 NEWER，给用户选择权）。
        """
        try:
            v_in = Version(incoming)
            v_ex = Version(existing)
            if v_in > v_ex:
                return DuplicateResult.NEWER_VERSION
            elif v_in < v_ex:
                return DuplicateResult.OLDER_VERSION
            else:
                return DuplicateResult.SAME_VERSION
        except InvalidVersion:
            # 无法解析版本号，保守处理
            return DuplicateResult.UNKNOWN_VERSION
