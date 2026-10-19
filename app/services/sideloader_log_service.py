"""Sideloader 运行日志解析服务（F23）

读取 BepInEx `LogOutput.log` 中 Sideloader 输出的缺失 / 过旧警告，
按 GUID 去重后对照本地库（复用 CardService.check_mod_availability）。

前提认知（需求文档 4.20）：
  - missing 警告仅在游戏内**实际加载**含缺失依赖的卡 / 场景时产生
    （每次槽位引用刷一行，重复多），仅进游戏主菜单不触发
  - 日志无版本号，只能按 GUID 匹配

多版本措辞兼容（F23-4，以方括号内 GUID 为稳定锚点）：
  旧版（实测样本 logs/LogOutput.log 丽莎卡样本，三级行各一次）：
    [Message:Sideloader] [UAR] WARNING! Missing mod detected! [guid]
    [Info   :Sideloader] [UAR] WARNING! Missing mod detected! [guid][website]
    [Warning:Sideloader] Missing mod [guid][分类][槽位ID][属性路径]
  当前源码（BepisPlugins Core.UAR.cs ShowGUIDError）：
    [Warning|Message:Sideloader] Missing zipmod! Some items are missing! - 作者 : 名称
    [Warning:Sideloader] [UAR] WARNING! Missing|Outdated mod detected! [guid]  website
    （website 不带方括号、跟在两个空格后、可空；无名称时上一行尾巴是裸 GUID）

暂不解析（F23-6，留二期）：`External info:` 全量依赖行（含已装 Mod）、
启动期 `Multiple versions detected` 重复报告（可作 F22 交叉验证）。
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.services.card_service import (
    CardModItem,
    CardModStatus,
    CardParseResult,
    CardService,
)


class LogEntryKind(Enum):
    """日志警告类别"""
    MISSING = "missing"    # 完全缺失（🔴）
    OUTDATED = "outdated"  # GUID 已装但槽位未找到 = 版本过旧（⚠，F23-3）


@dataclass
class LogMissingEntry:
    """去重后的单条缺失 / 过旧警告"""
    guid: str
    kind: LogEntryKind
    website: str = ""      # 日志携带的下载线索（F23-5）
    meta_author: str = ""  # 当前源码伴随行 "Missing zipmod! … - 作者 : 名称" 提取
    meta_name: str = ""
    occurrences: int = 0   # 原始命中行数（仅诊断用，展示层不显示）


class SideloaderLogService:
    """无状态服务：日志路径探测 + 警告解析 + 本地库对照"""

    # [UAR] WARNING! Missing/Outdated mod detected! [guid] + 可选网址。
    # 旧版网址带方括号紧随 GUID；当前源码两个空格后裸 URL（可空）
    _UAR_RE = re.compile(
        r"\[UAR\]\s+WARNING!\s+(?P<kind>Missing|Outdated)\s+mod\s+detected!\s+"
        r"\[(?P<guid>[^\[\]]+)\]"
        r"(?:\[(?P<web_bracket>https?://[^\[\]]*)\]|(?P<web_bare>\s+https?://\S+))?"
    )

    # 旧版槽位行：Missing mod [guid][分类][槽位ID][属性路径]
    _SLOT_RE = re.compile(
        r"Missing mod \[(?P<guid>[^\[\]]+)\]\[[^\[\]]+\]\[\d+\]\[[^\[\]]+\]"
    )

    # 当前源码伴随行：Missing/Outdated zipmod! Some items are missing! - 作者 : 名称
    # 与同一次调用的 [UAR] 行紧邻出现，元数据挂到下一条 [UAR] 命中
    _ZIPMOD_RE = re.compile(
        r"(?P<kind>Missing|Outdated) zipmod! Some items are missing! - (?P<tail>.+)"
    )

    @staticmethod
    def detect_log_path(config: AppConfig) -> Path | None:
        """按 game_mod_path 常见布局探测 BepInEx/LogOutput.log（F23-2）。

        游戏根与 Mods 根可能分离（如目录联动场景，实证无法纯推导），
        探测失败由 UI 引导用户选择一次。
        """
        mods_dir_str = (config.game_mod_path or "").strip()
        if not mods_dir_str:
            return None
        mods_dir = Path(mods_dir_str)
        seen: set[Path] = set()
        # 常见布局优先级：{游戏根}/mods（游戏根 = 父目录）> 更上层 > 本身即游戏根
        for base in (mods_dir.parent, mods_dir.parent.parent, mods_dir):
            cand = base / "BepInEx" / "LogOutput.log"
            if cand not in seen:
                seen.add(cand)
                if cand.is_file():
                    return cand
        return None

    def parse_log(self, path: Path) -> list[LogMissingEntry]:
        """逐行扫描日志，按 GUID 去重聚合缺失 / 过旧警告（F23-4）。

        同一缺失在 Message / Info / Warning 三级行各出现一次，
        全部计入 occurrences 但只产出一个条目；不抛异常以外的错误，
        文件编码容错（BepInEx 输出 UTF-8，混入异常字节按替换处理）。
        """
        entries: dict[str, LogMissingEntry] = {}
        # 上一条 "Missing/Outdated zipmod! … - 作者 : 名称" 行的元数据，
        # 挂到紧随其后的同类别 [UAR] 命中（同一 ShowGUIDError 调用的两行）；
        # 中间隔了任何其他行即作废，防止把上一个 Mod 的元数据误挂给下一个
        pending_zipmod: tuple[LogEntryKind, str, str] | None = None

        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                m = self._UAR_RE.search(line)
                if m is not None:
                    guid = m.group("guid").strip()
                    kind = self._zipmod_kind(m.group("kind"))
                    website = m.group("web_bracket") or (m.group("web_bare") or "").strip()
                    entry = entries.get(guid)
                    if entry is None:
                        entry = LogMissingEntry(guid=guid, kind=kind)
                        entries[guid] = entry
                    entry.occurrences += 1
                    # 同一 GUID 同时报缺失与过旧（单会话内理论上不会发生）以缺失为准
                    if kind is LogEntryKind.MISSING:
                        entry.kind = LogEntryKind.MISSING
                    if website and not entry.website:
                        entry.website = website
                    if pending_zipmod is not None and pending_zipmod[0] is kind:
                        _, author, name = pending_zipmod
                        if name == guid:
                            name = ""  # 无名称时尾巴是裸 GUID，不算元数据
                        if author and not entry.meta_author:
                            entry.meta_author = author
                        if name and not entry.meta_name:
                            entry.meta_name = name
                    pending_zipmod = None
                    continue

                m = self._SLOT_RE.search(line)
                if m is not None:
                    guid = m.group("guid").strip()
                    entry = entries.get(guid)
                    if entry is None:
                        entry = LogMissingEntry(guid=guid, kind=LogEntryKind.MISSING)
                        entries[guid] = entry
                    entry.occurrences += 1
                    pending_zipmod = None
                    continue

                m = self._ZIPMOD_RE.search(line)
                if m is not None:
                    tail = m.group("tail").strip()
                    if " : " in tail:
                        author, name = (s.strip() for s in tail.split(" : ", 1))
                    else:
                        author, name = "", tail
                    pending_zipmod = (self._zipmod_kind(m.group("kind")), author, name)
                    continue

                pending_zipmod = None

        return list(entries.values())

    def analyze(
            self,
            log_path: Path,
            db_session: Session,
            card_service: CardService,
    ) -> tuple[CardParseResult, dict[str, LogMissingEntry]]:
        """解析日志并对照本地库（F23-1）。

        复用 CardService.check_mod_availability 的 GAME→REPO→缺失判定与
        版本差异角标逻辑（F15-12/13，BR-V03 保守口径）；结果以
        CardParseResult 承载，返回 (result, guid→日志条目)，后者供 UI
        渲染 ⚠过旧 / 日志报缺失等日志特有标注。
        """
        entries = self.parse_log(log_path)
        result = CardParseResult(
            card_path=log_path,
            card_name=f"游戏日志（{log_path.name}）",
            card_type="游戏日志",
            is_valid=True,
            mods=[
                CardModItem(
                    guid=e.guid,
                    status=CardModStatus.MISSING,  # 占位，check_mod_availability 会覆写
                    meta_website=e.website,
                    meta_author=e.meta_author,
                    meta_name=e.meta_name,
                )
                for e in entries
            ],
        )
        card_service.check_mod_availability(result, db_session)
        return result, {e.guid: e for e in entries}

    @staticmethod
    def _zipmod_kind(word: str) -> LogEntryKind:
        return (
            LogEntryKind.MISSING if word == "Missing" else LogEntryKind.OUTDATED
        )
