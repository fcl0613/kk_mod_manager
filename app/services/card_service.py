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

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import io
import re
import struct
import zipfile

from sqlalchemy.orm import Session

from app.models.mod import Mod, ModLocation
from kkloader import KoikatuCharaData

SIGNATURE = b'\x89PNG\r\n\x1a\n'
IEND_TYPE = b'IEND'
CHECK_KEY = b"KoiKatuClothes"
SKIP_AFTER_IEND = 8  # 固定跳过8字节
SKIP_AFTER_CHECK = 10  # 检测成功后再跳过10字节
NAME_END_BYTES = b"\x28\x00\x00\xDF\x12\x00\x00"  # 卡片名结束标志
STOP_TAG = b"<additionalAccessories"


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


@dataclass
class CardParseResult:
    """卡片解析结果"""
    card_path: Path  # 卡片文件路径
    card_name: str  # 卡片文件名
    card_type: str  # "character" / "fashion" / "unknown"
    is_valid: bool  # 是否有效卡片
    error_message: str = ""  # 错误信息
    mods: list[CardModItem] = field(default_factory=list)

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

    # Koikatsu 卡片 PNG 内嵌数据中 GUID 常见格式
    # 如 "guid":"xxx" 或 modGUID 等字段
    GUID_PATTERN = re.compile(
        r'(?:["\'](?:guid|mod_guid|modGUID|ModGUID)["\']\s*[:=]\s*["\'])([^"\']+)(?:["\'])',
        re.IGNORECASE,
    )

    # 也可以匹配裸的方括号式 GUID（如 [Author]ModName）
    BRACKET_GUID_PATTERN = re.compile(
        r'\[([^\]]+)\]([^",\s\[\]]+)'
    )

    def parse_card(self, card_path: Path) -> CardParseResult:
        """
        解析角色卡/服装卡 PNG 文件，提取依赖的 Mod GUID 列表。

        Args:
            card_path: PNG 卡片文件路径

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

            # 查找 IEND chunk 末尾
            iend_end = self._find_png_end(data)
            if iend_end is None:
                return CardParseResult(
                    card_path=path,
                    card_name=path.name,
                    card_type="unknown",
                    is_valid=False,
                    error_message="无法解析 PNG 文件结构",
                )

            # 读取 IEND 之后的数据
            appended = data[iend_end:]

            guids: set[str] = set()
            cloth_guids: set[str] = set()
            chara_guids: set[str] = set()

            if len(appended) > 4:
                # 尝试作为 ZIP 解压
                zip_guids = self._extract_guids_from_zip(appended)
                if zip_guids:
                    guids.update(zip_guids)

                # 如果 ZIP 方式没提取到，尝试正则匹配
                if not guids:
                    text_guids = self._extract_guids_from_text(appended)
                    guids.update(text_guids)

                # 服装卡二进制格式：ModID.{xxx}.Slot
                cloth_guids = self.parse_cloth_card(path)
                guids.update(cloth_guids)

                # 角色卡 kkloader 格式：universalautoresolver info
                chara_guids = self.parse_character_card(path)
                guids.update(chara_guids)

            # 判断卡片类型
            card_type = self._detect_card_type(appended)
            if cloth_guids:
                card_type = "fashion"
            elif chara_guids:
                card_type = "character"

            if not guids:
                return CardParseResult(
                    card_path=path,
                    card_name=path.name,
                    card_type=card_type,
                    is_valid=True,
                    error_message="",
                    mods=[],
                )

            return CardParseResult(
                card_path=path,
                card_name=path.name,
                card_type=card_type,
                is_valid=True,
                error_message="",
                mods=[CardModItem(guid=g, status=CardModStatus.MISSING) for g in sorted(guids)],
            )

        except Exception as e:
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
            game_mod_path: str,
            repo_path: str,
            db_session: Session,
    ) -> CardParseResult:
        """
        检查卡片依赖的每个 Mod 在游戏和仓库中的可用性。

        顺序（BR-C03）：
          ① 先查游戏 Mod 路径
          ② 再查仓库路径
          ③ 都没有 → 标记为缺失

        Returns:
            更新了 mods 状态的 CardParseResult
        """
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
                continue

            # ③ 都没有 → 缺失
            item.status = CardModStatus.MISSING

        return result

    # ── 内部方法 ──

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

    def _extract_guids_from_zip(self, data: bytes) -> set[str]:
        """尝试从 ZIP 数据中提取 GUID"""
        guids: set[str] = set()
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    try:
                        content = zf.read(name)
                        # 尝试 JSON 解析
                        text = content.decode("utf-8", errors="ignore")
                        guids.update(self._extract_guids_from_text(text.encode("utf-8")))
                    except Exception:
                        # 尝试从二进制中匹配 GUID
                        guids.update(self._extract_guids_from_text(content))
        except (zipfile.BadZipFile, Exception):
            pass
        return guids

    def _extract_guids_from_text(self, data: bytes) -> set[str]:
        """从文本数据中提取 GUID 引用"""
        guids: set[str] = set()
        try:
            text = data.decode("utf-8", errors="ignore")

            # 方法 1: JSON 风格 GUID 匹配
            for m in self.GUID_PATTERN.finditer(text):
                g = m.group(1).strip()
                if g and len(g) > 2:
                    guids.add(g)

            # 方法 2: 方括号式 GUID 如 [Author]ModName
            for m in self.BRACKET_GUID_PATTERN.finditer(text):
                g = f"[{m.group(1)}]{m.group(2)}"
                # 过滤明显不是 GUID 的内容
                if 3 < len(g) < 200:
                    guids.add(g)

        except Exception:
            pass
        return guids

    @staticmethod
    def parse_character_card(file_path: Path) -> set[str]:
        """
        解析角色卡 PNG 文件，通过 kkloader 提取 sideloader 依赖的 Mod GUID。

        角色卡是 KoiKatuChara 格式，PNG IEND 后嵌入了 msgpack 序列化的
        角色数据。其中 KKEx 块下的 universalautoresolver info 列表记录了
        角色所依赖的 Mod GUID。

        Args:
            file_path: PNG 角色卡文件路径

        Returns:
            GUID 集合（可能为空）
        """
        mod_ids: set[str] = set()
        try:
            kc = KoikatuCharaData.load(str(file_path))
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

    @staticmethod
    def _detect_card_type(data: bytes) -> str:
        """尝试判断卡片类型"""
        try:
            text = data.decode("utf-8", errors="ignore").lower()
            if "coordinate" in text or "clothes" in text or "outfit" in text:
                return "fashion"
            if "chara" in text or "character" in text:
                return "character"
        except Exception:
            pass
        return "unknown"
