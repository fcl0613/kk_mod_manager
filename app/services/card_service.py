"""角色卡/服装卡解析服务（F15）

需求文档 v1.3:
  - F15-1: 拖入 PNG 文件进行解析
  - F15-2: 提取卡片所依赖的 Mod GUID 列表
  - F15-3: 依次检查游戏路径、仓库路径
  - F15-4: 标记状态（已安装/仓库有/缺失）
  - F15-8: 解析失败友好提示

Koikatsu 卡片格式:
  角色卡/服装卡为 PNG 文件，PNG 数据之后附加了一段数据块。
  数据块通常是 ZIP 格式，内含角色/服装 JSON 数据。
  其中 mod GUID 引用分布在 JSON 中。

  方法：
  1. 读取 PNG，找到 IEND chunk 结束位置
  2. 读取之后的数据
  3. 尝试作为 ZIP 解压，提取 JSON 中的 GUID 引用
  4. 若 ZIP 解析失败，使用正则匹配 GUID 模式
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
import struct

from sqlalchemy.orm import Session
from loguru import logger

from app.models.mod import Mod, ModLocation
from kkloader import KoikatuCharaData

SIGNATURE = b'\x89PNG\r\n\x1a\n'
IEND_TYPE = b'IEND'
CHECK_KEY = b"KoiKatuClothes"
SKIP_AFTER_IEND = 8  # 固定跳过8字节
SKIP_AFTER_CHECK = 10  # 检测成功后再跳过10字节
NAME_END_BYTES = b"\x28\x00\x00\xDF\x12\x00\x00"  # 卡片名结束标志
STOP_TAG = b"<additionalAccessories"
SCENE_KEY = "【KStudio】".encode("utf-8")  # 场景卡尾部标识（BR-C05）

# 依赖来源标签 → 粗分类型（F17-13）。
# 角色类来源：内嵌角色依赖的来源前缀（scene_service 拼接 "角色: 昵称"）；
# 工作室类来源：场景级 UAR 依赖标签（与 scene_service.LIST_DEPS/STRING_DEPS 一致，
# 由 tests/test_scene_service.py 一致性守护测试兜底）。
CHARA_SOURCE_PREFIX = "角色: "
STUDIO_SOURCE_TAGS = frozenset({"场景物品", "场景图案", "地图", "滤镜", "Ramp", "BGM"})


class CardModStatus(Enum):
    """卡片中依赖 Mod 的可用性状态"""
    INSTALLED = "installed"  # 🟢 已在游戏中安装
    IN_REPO = "in_repo"  # 🟡 在仓库中有（未安装）
    MISSING = "missing"  # 🔴 缺失


class CardType(Enum):
    """卡片类型"""
    CHARACTER = "character"  # 角色卡
    CLOTHING = "clothing"  # 服装卡


@dataclass
class CardModItem:
    """卡片依赖的单个 Mod 信息"""
    guid: str
    status: CardModStatus
    mod_name: str = ""  # 已安装/仓库中时填充 Mod 名称
    mod_version: str = ""  # Mod 版本号
    mod_author: str = ""  # 作者目录
    mod_id: int | None = None  # 数据库记录 ID（用于一键安装）
    location: str = ""  # 位置："game" / "repo"

    # ── 场景卡解析扩展（F17）──
    sources: list[str] = field(default_factory=list)  # 依赖来源标签，如 "场景物品" / "角色: 雪"
    meta_name: str = ""  # 卡内 UAR 元数据（缺失时的下载线索，F17-7）
    meta_author: str = ""
    meta_website: str = ""

    @property
    def status_icon(self) -> str:
        icons = {
            CardModStatus.INSTALLED: "🟢",
            CardModStatus.IN_REPO: "🟡",
            CardModStatus.MISSING: "🔴",
        }
        return icons[self.status]

    @property
    def status_text(self) -> str:
        texts = {
            CardModStatus.INSTALLED: "已安装",
            CardModStatus.IN_REPO: "仓库有",
            CardModStatus.MISSING: "缺失",
        }
        return texts[self.status]

    @property
    def dep_type(self) -> str:
        """依赖粗分类型（F17-13）：角色 / 工作室 / 角色+工作室。

        由来源标签推导：含 "角色: " 前缀来源 → 角色；含工作室标签 → 工作室；
        两者皆有 → 角色+工作室。空来源（人物/服装卡）按卡片视角归"角色"
        （F19-3：无场景上下文的角色类依赖统一落角色分类）。
        """
        has_chara = any(s.startswith(CHARA_SOURCE_PREFIX) for s in self.sources)
        has_studio = any(s in STUDIO_SOURCE_TAGS for s in self.sources)
        if has_chara and has_studio:
            return "角色+工作室"
        if has_studio:
            return "工作室"
        return "角色"


class GameVersion(Enum):
    """游戏版本"""
    KK = "kk"  # 一代卡（Koikatu）
    KKS = "kks"  # 二代卡（Koikatsu Sunshine）


@dataclass
class SceneSummary:
    """场景卡摘要信息（F17-2）"""
    version: str = ""  # 场景数据版本，如 "1.0.4.2"
    character_count: int = 0  # 内嵌角色数
    item_count: int = 0  # 物品数
    map_id: int = -1  # 地图 ID（kkloader 中 map 为 int；-1 表示无/未知）
    has_timeline: bool = False  # 是否 Timeline 动画场景
    game_version: GameVersion | None = None  # 取自首个成功解析的内嵌角色
    character_names: list[str] = field(default_factory=list)  # 内嵌角色名单
    skipped_characters: list[str] = field(default_factory=list)  # 解析失败被跳过的角色（BR-C08）


@dataclass
class CardParseResult:
    """卡片解析结果"""
    card_path: Path  # 卡片文件路径
    card_name: str  # 卡片文件名
    card_type: str  # "角色卡" / "服装卡" / "unknown"
    is_valid: bool  # 是否有效卡片
    error_message: str = ""  # 错误信息
    game_version: GameVersion | None = None  # 游戏版本（KK / KKS）
    mods: list[CardModItem] = field(default_factory=list)
    scene_summary: SceneSummary | None = None  # 场景摘要（仅场景卡非空）

    @property
    def total_count(self) -> int:
        return len(self.mods)

    @property
    def installed_count(self) -> int:
        return sum(1 for m in self.mods if m.status == CardModStatus.INSTALLED)

    @property
    def in_repo_count(self) -> int:
        return sum(1 for m in self.mods if m.status == CardModStatus.IN_REPO)

    @property
    def missing_count(self) -> int:
        return sum(1 for m in self.mods if m.status == CardModStatus.MISSING)


class CardService:
    """卡片解析服务"""

    # 一代卡（KK）的 Parameter version
    KK_PARAMETER_VERSION = '0.0.5'
    # 二代卡（KKS）的 Parameter version
    KKS_PARAMETER_VERSION = '0.0.6'

    kc: KoikatuCharaData | None

    @staticmethod
    def get_game_version(kc: KoikatuCharaData) -> "GameVersion":
        """
        获取卡片的游戏版本。

        Args:
            kc: KoikatuCharaData

        Returns:
            GameVersion.KK 或 GameVersion.KKS
        """
        try:
            version = kc["Parameter"]["version"]
            if version == '0.0.5':
                return GameVersion.KK
            else:
                return GameVersion.KKS
        except Exception:
            return GameVersion.KKS

    def kks_to_kk(self, output_path: Path) -> tuple[bool, str]:
        """
        将二代卡（KKS）转换为一代卡（KK）。

        转换方法：
          1. 使用 kkloader 加载卡片数据
          2. 将 Parameter.version 从 '0.0.6' 修改为 '0.0.5'
          3. 保存到指定输出路径

        Args:
            output_path: 输出的一代卡文件路径

        Returns:
            (成功与否, 错误信息)
        """
        if self.kc is None:
            return False, "转换失败"
        try:
            self.kc["Parameter"]["version"] = '0.0.5'
            self.kc.Parameter.version = '0.0.5'
            self.kc.header = b'\xe3\x80\x90KoiKatuChara\xe3\x80\x91'
            self.kc.save(str(output_path))
            return True, ""
        except Exception as e:
            return False, f"转换失败：{e}"

    def parse_card(
        self,
        card_path: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> CardParseResult:
        """
        解析角色卡/服装卡/场景卡 PNG 文件，提取依赖的 Mod GUID 列表。

        判断逻辑（BR-C02 / BR-C05）：
          1. 读取 PNG，定位 IEND 末尾
          2. IEND 后数据含 KoiKatuClothes 标识 → 服装卡（parse_cloth_card）
          3. 含 【KStudio】 标识 → 场景卡，委托 SceneService.parse_scene
             （顺序不可换：场景内嵌角色也含 【KoiKatuChara】 标识）
          4. 其余 → 角色卡，调用 parse_character_card
          5. 对提取的 GUID 统一 strip 处理（场景分支在 SceneService 内完成）

        Args:
            card_path: PNG 卡片文件路径
            progress: 进度回调 (已处理内嵌角色数, 总角色数)，仅场景卡触发
                （F17-11，供后台线程显示进度）；其余卡型忽略

        Returns:
            CardParseResult 包含 GUID 列表和解析状态
        """
        path = Path(card_path)
        if not path.exists():
            return CardParseResult(
                card_path=path,
                card_name=path.name,
                card_type="unknown",
                is_valid=False,
                error_message=f"文件不存在：{path}",
            )

        if path.suffix.lower() != ".png":
            return CardParseResult(
                card_path=path,
                card_name=path.name,
                card_type="unknown",
                is_valid=False,
                error_message="卡片必须是 PNG 格式文件（BR-C01）",
            )

        try:
            # 读取 PNG 文件
            data = path.read_bytes()

            # 验证 PNG 签名
            if data[:8] != SIGNATURE:
                logger.warning("卡片解析失败 - 非 PNG: {}", path.name)
                return CardParseResult(
                    card_path=path,
                    card_name=path.name,
                    card_type="unknown",
                    is_valid=False,
                    error_message="文件不是有效的 PNG 格式",
                )

            # 查找 IEND chunk 末尾
            iend_end = self._find_png_end(data)
            if iend_end is None:
                logger.warning("卡片解析失败 - 无法定位 IEND: {}", path.name)
                return CardParseResult(
                    card_path=path,
                    card_name=path.name,
                    card_type="unknown",
                    is_valid=False,
                    error_message="无法解析 PNG 文件结构",
                )

            # 读取 IEND 之后的数据
            extra = data[iend_end:]

            # 判断卡片类型：KoiKatuClothes → 服装卡，否则 → 角色卡
            is_cloth = self._is_cloth_card(extra)

            # 场景卡（BR-C05）：委托 SceneService，提前 return
            # （kc 置 None，避免残留上一张角色卡的数据）
            if self._is_scene_card(extra):
                from app.services import scene_service  # 惰性导入避免循环依赖
                self.kc = None
                logger.debug("识别为场景卡: {}", path.name)
                return scene_service.SceneService.parse_scene(path, progress=progress)

            guids: set[str] = set()
            if is_cloth:
                card_type = "服装卡"
                guids = self.parse_cloth_card(path)
                # 服装卡目前只支持kk
                game_version = GameVersion.KK
                self.kc = None
                logger.debug("解析服装卡: {} → {} 个 GUID", path.name, len(guids))
            else:
                card_type = "角色卡"
                kc = KoikatuCharaData.load(str(path))
                self.kc = kc
                guids = self.parse_character_card(kc)
                # 角色卡才需要判断游戏版本（KK / KKS）
                game_version = self.get_game_version(kc)
                logger.debug("解析角色卡: {} → {} 个 GUID", path.name, len(guids))

            # 对每个 GUID 做 strip 清洗
            guids = {g.strip(" !$'\"") for g in guids if g.strip(" !$'\"")}

            if not guids:
                logger.info("卡片解析完成 - 无依赖 Mod: {} ({})", path.name, card_type)
                return CardParseResult(
                    card_path=path,
                    card_name=path.name,
                    card_type=card_type,
                    is_valid=True,
                    error_message="",
                    game_version=game_version,
                    mods=[],
                )

            logger.info("卡片解析完成 - {} ({}): {} 个 GUID", path.name, card_type, len(guids))
            return CardParseResult(
                card_path=path,
                card_name=path.name,
                card_type=card_type,
                is_valid=True,
                error_message="",
                game_version=game_version,
                mods=[CardModItem(guid=g, status=CardModStatus.MISSING) for g in sorted(guids)],
            )

        except Exception as e:
            logger.exception("卡片解析异常: {}", path.name)
            return CardParseResult(
                card_path=path,
                card_name=path.name,
                card_type="unknown",
                is_valid=False,
                error_message=f"卡片解析失败：{e}",
            )

    def check_mod_availability(
            self,
            result: CardParseResult,
            db_session: Session,
    ) -> CardParseResult:
        """
        检查卡片依赖的每个 Mod 在游戏和仓库中的可用性。

        顺序（BR-C03）：
          ① 先查游戏 Mod 路径
          ② 再查仓库路径
            ㉃ 都没有 → 标记为缺失

        Returns:
            更新了 mods 状态的 CardParseResult
        """
        installed = 0
        in_repo = 0
        missing = 0
        for item in result.mods:
            # ① 查游戏中的 Mod
            game_mod: Mod | None = (
                db_session.query(Mod)
                .filter(
                    Mod.guid == item.guid,
                    Mod.location == ModLocation.GAME,
                )
                .first()
            )
            if game_mod is not None:
                item.status = CardModStatus.INSTALLED
                item.mod_name = game_mod.name
                item.mod_version = game_mod.version
                item.mod_author = game_mod.author
                item.mod_id = game_mod.id
                item.location = "game"
                installed += 1
                continue

            # ② 查仓库中的 Mod
            repo_mod: Mod | None = (
                db_session.query(Mod)
                .filter(
                    Mod.guid == item.guid,
                    Mod.location == ModLocation.REPO,
                )
                .first()
            )
            if repo_mod is not None:
                item.status = CardModStatus.IN_REPO
                item.mod_name = repo_mod.name
                item.mod_version = repo_mod.version
                item.mod_author = repo_mod.author
                item.mod_id = repo_mod.id
                item.location = "repo"
                in_repo += 1
                continue

            # ③ 都没有 → 缺失
            item.status = CardModStatus.MISSING
            missing += 1

        logger.info(
            "卡片 Mod 可用性检查完成: 已安装={}, 仓库有={}, 缺失={}",
            installed, in_repo, missing,
        )
        return result

    # ── 内部方法 ──

    @staticmethod
    def _is_cloth_card(extra: bytes) -> bool:
        """检测 IEND 后数据是否包含 KoiKatuClothes 标识（服装卡）"""
        if len(extra) < SKIP_AFTER_IEND + len(CHECK_KEY):
            return False
        return extra[SKIP_AFTER_IEND:SKIP_AFTER_IEND + len(CHECK_KEY)] == CHECK_KEY

    @staticmethod
    def _is_scene_card(extra: bytes) -> bool:
        """检测 IEND 后数据是否含【KStudio】场景标识（场景卡）"""
        return SCENE_KEY in extra

    @staticmethod
    def _find_png_end(data: bytes) -> int | None:
        """查找 PNG IEND chunk 结束位置"""
        # PNG signature: 8 bytes
        # Each chunk: 4 bytes length + 4 bytes type + data + 4 bytes CRC
        offset = 8  # skip PNG signature
        while offset + 12 <= len(data):
            chunk_len = struct.unpack(">I", data[offset:offset + 4])[0]
            chunk_type = data[offset + 4:offset + 8].decode("ascii", errors="ignore")
            chunk_total = 12 + chunk_len  # 4 len + 4 type + data + 4 crc
            if chunk_type == "IEND":
                return offset + chunk_total
            offset += chunk_total
        return None

    @staticmethod
    def parse_character_card(kc: KoikatuCharaData) -> set[str]:
        """
        解析角色卡 PNG 文件，通过 kkloader 提取 sideloader 依赖的 Mod GUID。

        角色卡是 KoiKatuChara 格式，PNG IEND 后嵌入了 msgpack 序列化的
        角色数据。其中 KKEx 块下的 universalautoresolver info 列表记录了
        角色所依赖的 Mod GUID。

        Args:
            kc: KoikatuCharaData

        Returns:
            GUID 集合（可能为空）
        """
        mod_ids: set[str] = set()
        try:
            resolver = kc['KKEx']['com.bepis.sideloader.universalautoresolver']
            info_list: list[bytes] = resolver[1]['info']
            start = 8
            for info in info_list:
                end = info.find(b'\xa4Slot', start)
                if end == -1:
                    continue
                mod_ids.add(info[start:end].decode('utf-8'))
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            pass
        return mod_ids

    @staticmethod
    def parse_cloth_card(file_path: Path) -> set[str]:
        """
        解析服装卡 PNG 文件，提取 ModID.{xxx}.Slot 格式的 GUID 集合。

        服装卡是 KoiKatuClothes 格式的特殊卡片，PNG IEND 后嵌入了
        额外的二进制数据块，其中包含服装依赖的 Mod GUID 列表。

        解析流程（BR-C02）：
          1. 验证 PNG 签名
          2. 定位 IEND chunk 末尾，读取其后数据
          3. 跳过固定 8 字节
          4. 检测 KoiKatuClothes 标识
          5. 跳过 10 字节，提取卡片名
          6. 定位 KKEx → info 数据区
          7. 正则匹配 ModID.{xxx}.Slot

        Args:
            file_path: PNG 服装卡文件路径

        Returns:
            GUID 集合（可能为空）
        """
        path = Path(file_path)
        mod_ids: set[str] = set()

        if not path.exists() or path.suffix.lower() != ".png":
            return mod_ids

        try:
            data = path.read_bytes()
        except OSError:
            return mod_ids

        # 1. 验证 PNG 签名
        if data[:8] != SIGNATURE:
            return mod_ids

        # 2. 定位 IEND chunk 末尾
        offset = 8
        iend_end: int | None = None
        while offset + 12 <= len(data):
            chunk_len = struct.unpack(">I", data[offset:offset + 4])[0]
            chunk_type = data[offset + 4:offset + 8]
            chunk_total = 12 + chunk_len
            if chunk_type == IEND_TYPE:
                iend_end = offset + chunk_total
                break
            offset += chunk_total

        if iend_end is None:
            return mod_ids

        extra = data[iend_end:]
        if len(extra) < SKIP_AFTER_IEND + len(CHECK_KEY):
            return mod_ids

        # 3. 跳过固定 8 字节
        ptr = SKIP_AFTER_IEND

        # 4. 检查 KoiKatuClothes 标识
        if extra[ptr:ptr + len(CHECK_KEY)] != CHECK_KEY:
            return mod_ids

        # 5. 跳过 10 字节
        ptr += len(CHECK_KEY) + SKIP_AFTER_CHECK

        # 6. 跳过卡片名（定位到 NAME_END_BYTES）
        end_pos = extra.find(NAME_END_BYTES, ptr)
        if end_pos == -1:
            return mod_ids
        ptr = end_pos

        # 7. 查找 KKEx → info
        kkex_pos = extra.find(b"KKEx", ptr)
        if kkex_pos == -1:
            return mod_ids

        info_pos = extra.find(b"info", kkex_pos)
        if info_pos == -1:
            return mod_ids

        # 8. 提取 ModID.{xxx}.Slot（到 STOP_TAG 为止）
        stop_pos = extra.find(STOP_TAG, info_pos)
        if stop_pos == -1:
            data_block = extra[info_pos:]
        else:
            data_block = extra[info_pos:stop_pos]

        pattern = re.compile(b"ModID.(.*?).Slot")
        matches = pattern.findall(data_block)
        mod_ids = {m.decode("utf-8", errors="ignore") for m in matches}

        return mod_ids
