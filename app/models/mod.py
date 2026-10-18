"""Mod 数据库 ORM 模型

需求文档 v1.3: 
  - 同一个 Mod（相同 GUID）可以同时存在于 GAME 和 REPO 两条记录中
  - status 仅对 GAME 位置的 Mod 有效
  - author_normalized 用于目录创建和大小写无关匹配
"""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import String, Enum, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.version_utils import display_version


class ModLocation(PyEnum):
    """Mod 所在位置"""
    GAME = "game"   # 游戏 Mod 路径（已安装到游戏）
    REPO = "repo"   # 本地仓库路径（已收藏但未安装）


class ModStatus(PyEnum):
    """Mod 状态（仅 GAME 位置有效）"""
    ENABLED = "enabled"    # 启用（扩展名为 .zipmod）
    DISABLED = "disabled"  # 禁用（扩展名为 .zipmod_disabled）


class Mod(Base):
    """Mod 记录表

    每条记录代表一个 zipmod 文件在某个位置（游戏或仓库）的存在。
    同一个 GUID 可以有两条记录（GAME + REPO）。
    """
    __tablename__ = "mods"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ── manifest.xml 字段 ──
    guid: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    author: Mapped[str] = mapped_column(String(100), default="")
    author_normalized: Mapped[str] = mapped_column(
        String(100), default="", index=True,
        comment="规范化后的作者名（小写），用于目录创建和匹配"
    )
    description: Mapped[str] = mapped_column(String(1000), default="")
    website: Mapped[str] = mapped_column(String(500), default="")

    # ── 文件信息 ──
    file_name: Mapped[str] = mapped_column(
        String(300), nullable=False,
        comment="zipmod 文件名，如 [Makise]GS-Alice.zipmod"
    )

    # ── 位置与状态 ──
    location: Mapped[ModLocation] = mapped_column(
        Enum(ModLocation), nullable=False,
        comment="Mod 位置：GAME（游戏已装）或 REPO（仓库）"
    )
    status: Mapped[ModStatus] = mapped_column(
        Enum(ModStatus), default=ModStatus.ENABLED,
        comment="状态：仅 GAME 位置有效"
    )

    # ── 时间 ──
    installed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now,
        comment="安装/添加时间"
    )

    # ── 用户备注 ──
    notes: Mapped[str] = mapped_column(String(1000), default="")

    # —— 仓库mod相对路径 ——
    repo_relative_path: Mapped[str] = mapped_column(String(1000), default="", comment="仓库mod的相对路径")

    # —— 游戏mod相对路径（F19，仅 GAME 记录使用，REPO 记录始终为 ""）——
    game_relative_path: Mapped[str] = mapped_column(String(1000), default="", comment="游戏mod的相对路径")

    # ── 复合索引：同一位置下 GUID 唯一 ──
    __table_args__ = (
        Index("idx_location_guid", "location", "guid", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<Mod(id={self.id}, guid='{self.guid}', name='{self.name}', "
            f"{display_version(self.version)}, location={self.location.value}, "
            f"status={self.status.value})>"
        )

    @property
    def is_disabled_file(self) -> str:
        """禁用状态下的文件名"""
        if self.file_name.endswith(".zipmod"):
            return self.file_name + "_disabled"
        return self.file_name

    @property
    def current_file_name(self) -> str:
        """当前状态下的实际文件名"""
        if self.status == ModStatus.DISABLED:
            return self.is_disabled_file
        return self.file_name

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "guid": self.guid,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "website": self.website,
            "file_name": self.file_name,
            "location": self.location.value,
            "status": self.status.value,
            "installed_at": self.installed_at.isoformat() if self.installed_at else None,
            "notes": self.notes,
            "repo_relative_path": self.repo_relative_path,
            "game_relative_path": self.game_relative_path
        }


class OperationLog(Base):
    """操作日志表"""
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    operation: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="操作类型：install / uninstall / repo_add / repo_remove / enable / disable / upgrade / sync / dedup"
    )
    mod_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Mod 名称"
    )
    mod_guid: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Mod GUID"
    )
    location: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="操作位置：game / repo"
    )
    detail: Mapped[str] = mapped_column(
        String(500), default="", comment="操作详情"
    )
    result: Mapped[str] = mapped_column(
        String(20), nullable=False, default="success",
        comment="操作结果：success / failed"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, comment="操作时间"
    )
