"""场景卡解析服务（F17）

场景卡 = Koikatsu Studio 保存的 PNG 场景数据（尾部含【KStudio】标识，
由 CardService.parse_card 识别后委托本服务）。

Mod 依赖存在于两处（BR-C06）：
  ① 场景级扩展数据块 sd.mod_data（msgpack 字典）中 Sideloader
     UniversalAutoResolver 记录的物品/图案/地图/滤镜/Ramp/BGM 依赖
  ② 每个内嵌角色（CHARACTER 对象内嵌完整角色卡数据）的 KKEx 依赖列表
     —— 复用 CardService.parse_character_card

提取后按 GUID 合并去重并保留来源（BR-C07）；单个内嵌角色提取失败仅
跳过并记录（BR-C08）；kkloader 加载阶段的失败则整卡失败（F17-12）。
"""

from collections.abc import Callable
from pathlib import Path

import msgpack
from kkloader import KoikatuSceneData
from loguru import logger

from app.services.card_service import (
    CardModItem,
    CardModStatus,
    CardParseResult,
    CardService,
    SceneSummary,
)

UAR_KEY = "com.bepis.sideloader.universalautoresolver"

# 场景级 UAR 中的列表型依赖：键名 → 来源标签（每条为 msgpack 序列化 bytes）
LIST_DEPS = (
    ("itemInfo", "场景物品"),
    ("patternInfo", "场景图案"),
)

# 场景级 UAR 中的字符串型依赖：GUID 键名 → 来源标签
# 元数据 sibling 键按 {前缀}InfoAuthor / InfoWebsite / InfoName 拼接
# （结构来自 Sideloader Core.UAR.Studio.cs；sibling 键名待带物品场景样本验证）
STRING_DEPS = (
    ("mapInfoGUID", "地图"),
    ("filterInfoGUID", "滤镜"),
    ("rampInfoGUID", "Ramp"),
    ("bgmInfoGUID", "BGM"),
)

# GUID 清洗字符集（与 CardService.parse_card 的公共 strip 保持一致；
# 场景分支提前 return，绕过了 parse_card 的清洗，须在此补做）
_STRIP_CHARS = " !$'\""


class SceneService:
    """场景卡解析服务（全静态方法）"""

    @staticmethod
    def parse_scene(
        path: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> CardParseResult:
        """
        解析场景卡 PNG，提取场景级与内嵌角色的 Mod 依赖（F17-3~6）。

        Args:
            path: 场景卡 PNG 文件路径
            progress: 进度回调 (已处理内嵌角色数, 总角色数)，供后台线程
                显示进度（F17-11）；无内嵌角色时只回调一次 (0, 0)

        Returns:
            CardParseResult（card_type="场景卡"，scene_summary 含摘要；
            解析失败时 is_valid=False 且带友好错误信息，F17-12）
        """
        path = Path(path)
        try:
            sd = KoikatuSceneData.load(str(path))
        except Exception as e:
            logger.warning("场景数据加载失败: {} - {}", path.name, e)
            return CardParseResult(
                card_path=path,
                card_name=path.name,
                card_type="场景卡",
                is_valid=False,
                error_message="场景数据解析失败：文件可能已损坏，或是不受支持的场景格式",
            )

        summary = SceneService._build_summary(sd)
        merged: dict[str, CardModItem] = {}
        SceneService._collect_scene_deps(sd, merged)
        SceneService._collect_character_deps(sd, summary, merged, progress)

        mods = sorted(merged.values(), key=lambda m: m.guid)
        logger.info(
            "场景卡解析完成: {} - 角色={} 物品={} 依赖={} (跳过角色={})",
            path.name, summary.character_count, summary.item_count,
            len(mods), len(summary.skipped_characters),
        )
        # game_version 保持 None：场景卡不提供 KK→KKS 转卡（F17-12），
        # KK/KKS 线索记录在 scene_summary.game_version 中供 UI 展示
        return CardParseResult(
            card_path=path,
            card_name=path.name,
            card_type="场景卡",
            is_valid=True,
            scene_summary=summary,
            mods=mods,
        )

    # ── 内部方法 ──

    @staticmethod
    def _build_summary(sd: KoikatuSceneData) -> SceneSummary:
        """构建场景摘要（F17-2）"""
        summary = SceneSummary(version=str(getattr(sd, "version", "") or ""))
        try:
            counts = sd.count_object_types() or {}
            summary.character_count = int(counts.get("Character", 0))
            summary.item_count = int(counts.get("Item", 0))
        except Exception:
            pass
        try:
            summary.map_id = int(getattr(sd, "map", -1))
        except (TypeError, ValueError):
            summary.map_id = -1
        mod_data = getattr(sd, "mod_data", None) or {}
        # Timeline 等扩展插件数据以插件名为键写入 mod_data（键名大小写不固定）
        summary.has_timeline = any(str(k).lower() == "timeline" for k in mod_data)
        return summary

    @staticmethod
    def _collect_scene_deps(sd: KoikatuSceneData, merged: dict[str, CardModItem]) -> None:
        """提取场景级 UAR 依赖：物品/图案列表 + 地图/滤镜/Ramp/BGM 字符串（F17-3/4）"""
        mod_data = getattr(sd, "mod_data", None) or {}
        uar = mod_data.get(UAR_KEY)
        # 无 Mod 物品的场景没有场景级 UAR 数据，属正常情况
        if not (isinstance(uar, (list, tuple)) and len(uar) >= 2 and isinstance(uar[1], dict)):
            return
        data = uar[1]

        # 列表型：逐条 msgpack 解码取 GUID 与元数据，单条损坏跳过（BR-C08）
        for key, label in LIST_DEPS:
            for raw in data.get(key) or []:
                try:
                    entry = msgpack.unpackb(raw, raw=False)
                    if not isinstance(entry, dict):
                        continue
                    guid = str(entry.get("GUID", "")).strip(_STRIP_CHARS)
                    if not guid:
                        continue
                    SceneService._merge_guid(
                        merged, guid, label,
                        meta=(entry.get("Name") or "", entry.get("Author") or "", entry.get("Website") or ""),
                    )
                except Exception:
                    continue

        # 字符串型：GUID 直接可用，元数据取 sibling 键
        for guid_key, label in STRING_DEPS:
            guid = data.get(guid_key)
            if not isinstance(guid, str):
                continue
            guid = guid.strip(_STRIP_CHARS)
            if not guid:
                continue
            prefix = guid_key[: -len("GUID")]  # 如 "mapInfo"
            SceneService._merge_guid(
                merged, guid, label,
                meta=(
                    data.get(prefix + "Name") or "",
                    data.get(prefix + "Author") or "",
                    data.get(prefix + "Website") or "",
                ),
            )

    @staticmethod
    def _collect_character_deps(
        sd: KoikatuSceneData,
        summary: SceneSummary,
        merged: dict[str, CardModItem],
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """遍历内嵌角色，复用角色卡解析提取依赖（F17-5）。

        容错边界（BR-C08）：kkloader 加载阶段的整卡失败已在 parse_scene
        处理；此处仅遍历/提取阶段的单角色失败跳过并记入 skipped_characters。
        progress 按角色数汇报（成功与跳过都计入，F17-11）。
        """
        try:
            characters = list(sd.walk(object_type=KoikatuSceneData.CHARACTER))
        except Exception as e:
            logger.warning("场景角色遍历失败: {}", e)
            return

        total = len(characters)
        if progress is not None:
            progress(0, total)

        for i, (_, obj) in enumerate(characters):
            try:
                kc = obj["data"]["character"]
            except (KeyError, TypeError):
                summary.skipped_characters.append(f"角色{i + 1}")
            else:
                try:
                    nickname = str(kc["Parameter"]["nickname"])
                except Exception:
                    nickname = ""
                if not nickname.strip():  # 昵称缺失/空白 → 回退序号（真实卡常见空昵称）
                    nickname = f"角色{i + 1}"

                try:
                    guids = CardService.parse_character_card(kc)
                    meta_map = SceneService._decode_chara_meta(kc)
                    for raw_guid in guids:
                        guid = raw_guid.strip(_STRIP_CHARS)
                        if not guid:
                            continue
                        name, author, website = meta_map.get(guid, ("", "", ""))
                        SceneService._merge_guid(
                            merged, guid, f"角色: {nickname}",
                            meta=(name, author, website),
                        )
                    summary.character_names.append(nickname)
                    if summary.game_version is None:
                        summary.game_version = CardService.get_game_version(kc)
                except Exception as e:
                    logger.warning("内嵌角色解析失败，跳过: {} - {}", nickname, e)
                    summary.skipped_characters.append(nickname)
            if progress is not None:
                progress(i + 1, total)

    @staticmethod
    def _decode_chara_meta(kc) -> dict[str, tuple[str, str, str]]:
        """解码内嵌角色 KKEx UAR info 列表，建立 GUID → (Name, Author, Website) 映射。

        元数据是缺失 Mod 的下载线索（F17-7），非必需——解码失败静默返回空映射。
        """
        meta: dict[str, tuple[str, str, str]] = {}
        try:
            info_list = kc["KKEx"][UAR_KEY][1]["info"]
        except (KeyError, IndexError, TypeError):
            return meta
        for raw in info_list:
            try:
                entry = msgpack.unpackb(raw, raw=False)
                if not isinstance(entry, dict):
                    continue
                mod_id = str(entry.get("ModID", "")).strip(_STRIP_CHARS)
                if not mod_id:
                    continue
                meta.setdefault(mod_id, (
                    str(entry.get("Name") or ""),
                    str(entry.get("Author") or ""),
                    str(entry.get("Website") or ""),
                ))
            except Exception:
                continue
        return meta

    @staticmethod
    def _merge_guid(
        merged: dict[str, CardModItem],
        guid: str,
        source: str,
        meta: tuple[str, str, str] | None = None,
    ) -> None:
        """合并依赖：同 GUID 多来源合并为一条并累积来源（BR-C07/F17-6）。

        元数据字段空位回填（首个非空值优先）。
        """
        guid = guid.strip(_STRIP_CHARS)
        if not guid:
            return
        item = merged.get(guid)
        if item is None:
            item = CardModItem(guid=guid, status=CardModStatus.MISSING, sources=[source])
            if meta:
                item.meta_name = meta[0].strip()
                item.meta_author = meta[1].strip()
                item.meta_website = meta[2].strip()
            merged[guid] = item
            return
        if source not in item.sources:
            item.sources.append(source)
        if meta:
            if not item.meta_name and meta[0].strip():
                item.meta_name = meta[0].strip()
            if not item.meta_author and meta[1].strip():
                item.meta_author = meta[1].strip()
            if not item.meta_website and meta[2].strip():
                item.meta_website = meta[2].strip()
