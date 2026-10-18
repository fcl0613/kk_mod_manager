"""pytest 共享 fixture 与测试数据构造器。

设计原则（与 tests/sync_service_test.py 的手工模式一致）：
  - 内存 SQLite（create_all），绝不触碰真实 data/mods.db
  - 临时目录模拟游戏目录 / 仓库目录
  - autouse 替换 app.config._config_instance 单例，teardown 无条件恢复
  - .gitignore 排除 *.zipmod 与 data/，zipmod / 卡片 fixture 全部运行时生成
"""

import struct
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import msgpack
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 与 tests/sync_service_test.py 一致：保证从任意 cwd 运行都能 import app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

logger.remove()  # 静音服务层日志（pytest 会捕获输出，失败时再看）

import app.config as cfg
from app.config import AppConfig
from app.database import Base
from app.services.card_service import (
    CHECK_KEY,
    NAME_END_BYTES,
    SIGNATURE,
    STOP_TAG,
    CardService,
)
from app.services.mod_service import ModService

# 角色卡 UAR（universalautoresolver）在 KKEx 下的键名
UAR_KEY = "com.bepis.sideloader.universalautoresolver"


# ═══════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════

@pytest.fixture()
def db_session():
    """每个测试独立的内存 SQLite 会话（不触碰真实 data/mods.db）"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def dirs(tmp_path):
    """临时游戏目录 / 仓库目录"""
    game = tmp_path / "game"
    repo = tmp_path / "repo"
    game.mkdir()
    repo.mkdir()
    return SimpleNamespace(game=game, repo=repo, root=tmp_path)


@pytest.fixture(autouse=True)
def app_config(dirs):
    """autouse 安全网：用临时路径替换配置单例。

    即使测试忘记声明，也拿不到真实 data/config.json 里的路径，
    不会向真实磁盘复制/删除文件；测试中途改写单例
    （如 clean_empty_dirs=False、空 game_mod_path）由 teardown 兜底恢复。
    """
    original = cfg._config_instance
    cfg._config_instance = AppConfig(
        game_mod_path=str(dirs.game), repo_path=str(dirs.repo)
    )
    yield cfg._config_instance
    cfg._config_instance = original


@pytest.fixture(autouse=True)
def _trash_root_isolation(tmp_path, monkeypatch):
    """autouse 安全网（F25）：回收站根指到临时目录，测试绝不写真实 data/.trash/。

    move_to_trash 经 app.database.get_data_dir 解析回收站位置，
    这里替换该函数即可全局生效（含 mod_service 内的调用）。
    """
    monkeypatch.setattr("app.database.get_data_dir", lambda: tmp_path / "appdata")


@pytest.fixture()
def trash_root(tmp_path) -> Path:
    """断言用：本次测试对应的回收站根目录（与 _trash_root_isolation 同源）"""
    return tmp_path / "appdata" / ".trash"


@pytest.fixture()
def svc(db_session):
    """配置好临时路径的 ModService"""
    return ModService(db_session)


@pytest.fixture()
def card_svc():
    """全新 CardService（避免 self.kc 跨测试泄漏，该类无 __init__）"""
    return CardService()


@pytest.fixture()
def fake_kc(monkeypatch):
    """monkeypatch card_service.KoikatuCharaData 为 FakeKCLoader。

    用法：payload = fake_kc(StubKC({...})) —— 返回赋给 load 的桩对象，
    测试结束后自动恢复 KoikatuCharaData 原引用并清空 payload。
    """
    from app.services import card_service as cs

    def _install(payload):
        FakeKCLoader.payload = payload
        monkeypatch.setattr(cs, "KoikatuCharaData", FakeKCLoader)
        return payload

    yield _install
    FakeKCLoader.payload = None


# ═══════════════════════════════════════════════════════════
# zipmod 构造器（测试文件直接 from tests.conftest import 使用）
# ═══════════════════════════════════════════════════════════

def make_zipmod(
    dir_path: Path,
    file_stem: str,
    guid: str,
    version: str = "1.0.0",
    author: str | None = "TestAuthor",
    disabled: bool = False,
    description: str = "",
    website: str = "",
) -> Path:
    """造一个含 manifest.xml 的 zipmod 文件（author 为 None 时不写 <author> 标签）"""
    parts = [
        f"<guid>{guid}</guid>",
        f"<name>{file_stem}</name>",
        f"<version>{version}</version>",
    ]
    if author:
        parts.append(f"<author>{author}</author>")
    if description:
        parts.append(f"<description>{description}</description>")
    if website:
        parts.append(f"<website>{website}</website>")
    body = "<manifest>" + "".join(parts) + "</manifest>"
    fn = file_stem + (".zipmod_disabled" if disabled else ".zipmod")
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    p = Path(dir_path) / fn
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.xml", body)
    return p


def make_bad_file(dir_path: Path, name: str) -> Path:
    """造一个非 zip 的伪 zipmod 文件"""
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    p = Path(dir_path) / name
    p.write_text("这不是一个zip文件", encoding="utf-8")
    return p


def make_raw_zipmod(dir_path: Path, name: str, manifest_bytes: bytes) -> Path:
    """造 manifest.xml 为自定义原始字节的 zipmod（编码 / 格式测试用）"""
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    p = Path(dir_path) / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.xml", manifest_bytes)
    return p


def make_zip_without_manifest(dir_path: Path, name: str) -> Path:
    """造一个不含 manifest.xml 的合法 zip 文件"""
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    p = Path(dir_path) / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "no manifest here")
    return p


# ═══════════════════════════════════════════════════════════
# 卡片字节级构造器（与 card_service 的解析契约逐字节对应）
# ═══════════════════════════════════════════════════════════

def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """打包 PNG chunk：4 字节大端长度 + 类型 + 数据 + 4 字节 CRC 占位"""
    return struct.pack(">I", len(data)) + chunk_type + data + b"\x00\x00\x00\x00"


def make_png_bytes(extra: bytes = b"", with_iend: bool = True) -> bytes:
    """最小合法 PNG：签名 + IHDR(13B) + IDAT + [IEND] + IEND 后附加数据。

    _find_png_end 不校验 CRC，4 个零字节即可。
    """
    out = SIGNATURE + _png_chunk(b"IHDR", b"\x00" * 13) + _png_chunk(b"IDAT", b"\x01\x02\x03\x04")
    if with_iend:
        out += _png_chunk(b"IEND", b"")
    return out + extra


def make_cloth_tail(
    guids: list[bytes],
    stop_tag: bool = True,
    name: bytes = b"TestClothCard",
) -> bytes:
    """构造服装卡 IEND 后的尾部字节（与 parse_cloth_card 的布局对应）：

    8 字节跳过 | KoiKatuClothes | 10 字节跳过 | 卡片名 |
    NAME_END_BYTES | KKEx…info | ModID.{guid}.Slot × N | [STOP_TAG + 诱饵 GUID]
    """
    body = b"".join(b"ModID." + g + b".Slot" for g in guids)
    tail = (
        b"\x00" * 8 + CHECK_KEY + b"\x00" * 10 + name + NAME_END_BYTES
        + b"KKEx\x00\x01info" + body
    )
    if stop_tag:
        tail += STOP_TAG + b"ModID.after_stop_tag.Slot"
    return tail


def make_info_blob(guid: str, with_slot: bool = True) -> bytes:
    """构造角色卡 UAR info 块：8 字节填充 + GUID + \\xa4Slot 标记。

    满足 parse_character_card 的 info[8:find(b'\\xa4Slot', 8)] 契约。
    """
    blob = b"\x00" * 8 + guid.encode("utf-8")
    if with_slot:
        blob += b"\xa4Slot"
    return blob


class StubKC:
    """模拟 KoikatuCharaData：__getitem__/__setitem__ 代理内部 dict。

    - get_game_version / parse_character_card 只需字典式读取
      （缺键抛 KeyError，走源码容错分支）
    - kks_to_kk 需要 Parameter 属性（SimpleNamespace）、header 属性、
      save 可调用（默认 MagicMock，测试可改写 side_effect）
    """

    def __init__(self, data: dict | None = None):
        self._data = data if data is not None else {}
        self.Parameter = SimpleNamespace(version=None)
        self.header = b""
        self.save = MagicMock()

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value


class FakeKCLoader:
    """parse_card 角色卡分支桩：monkeypatch 到 card_service.KoikatuCharaData，
    .load(str(path)) 返回类属性 payload（用 fake_kc fixture 安装与清理）"""

    payload = None

    @staticmethod
    def load(path):
        return FakeKCLoader.payload


# ═══════════════════════════════════════════════════════════
# 场景卡构造器（与 scene_service 的解析契约对应）
# ═══════════════════════════════════════════════════════════

def make_scene_item_info(
    guid: str, name: str = "", author: str = "", website: str = "",
) -> bytes:
    """构造场景级 UAR 物品/图案条目（msgpack dict：GUID/Name/Author/Website）"""
    return msgpack.packb(
        {"GUID": guid, "Name": name, "Author": author, "Website": website,
         "Slot": 1, "LocalSlot": 100000001},
        use_bin_type=True,
    )


def make_chara_uar_blob(
    mod_id: str, name: str = "", author: str = "", website: str = "",
) -> bytes:
    """构造角色卡 UAR info 条目（msgpack dict：ModID 打头，Slot 次之）。

    必须满足 parse_character_card 的字节级契约 info[8:find(b'\\xa4Slot')]：
    ModID 键打头（0x8N 地图头 + 0xa5'ModID' 共 7 字节）且 GUID ≤ 31 字节
    （fixstr 单字节头，数据从第 8 字节起），否则该字节 hack 会截错。
    """
    assert len(mod_id.encode("utf-8")) <= 31, "GUID 超过 31 字节会破坏 info[8:] 契约"
    return msgpack.packb(
        {"ModID": mod_id, "Slot": 100, "LocalSlot": 100000002,
         "Property": "ChaFileFace.hlUpId", "CategoryNo": 410,
         "Author": author, "Website": website, "Name": name},
        use_bin_type=True,
    )


class StubScene:
    """模拟 KoikatuSceneData：version/map/mod_data 属性 + walk/count_object_types。

    walk 产出 (序号, {"type": 0, "data": {"character": StubKC}})，与真实
    CHARACTER 对象嵌套结构一致（obj["data"]["character"] 为内嵌角色数据）。
    """

    CHARACTER = 0

    def __init__(self, version="1.0.4.2", map_id=-1, mod_data=None,
                 characters=None, counts=None):
        self.version = version
        self.map = map_id
        self.mod_data = mod_data
        self._characters = characters or []
        self._counts = counts or {}

    def walk(self, include_depth=False, object_type=None):
        for i, kc in enumerate(self._characters):
            yield i, {"type": 0, "data": {"character": kc}}

    def count_object_types(self):
        return dict(self._counts)


class FakeSceneLoader:
    """SceneService 加载桩：monkeypatch 到 scene_service.KoikatuSceneData，
    .load(str(path)) 返回类属性 payload（用 fake_scene fixture 安装与清理）"""

    payload = None
    CHARACTER = 0

    @staticmethod
    def load(path):
        return FakeSceneLoader.payload


@pytest.fixture()
def fake_scene(monkeypatch):
    """monkeypatch scene_service.KoikatuSceneData 为 FakeSceneLoader。

    用法：sd = fake_scene(StubScene(...)) —— 返回赋给 load 的桩对象，
    测试结束后自动恢复原引用并清空 payload（仿 fake_kc 模式）。
    """
    from app.services import scene_service as ss

    def _install(payload):
        FakeSceneLoader.payload = payload
        monkeypatch.setattr(ss, "KoikatuSceneData", FakeSceneLoader)
        return payload

    yield _install
    FakeSceneLoader.payload = None
