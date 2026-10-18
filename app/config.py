"""应用配置管理

配置存储在 exe 所在目录的 data/config.json（需求文档 8.3）。

需求文档 v1.5 配置项：
  - game_mod_path: 游戏 Mod 路径
  - repo_path: 本地 Mod 仓库路径
  - remote_repo_url: 远程仓库 URL（可选）
  - UI 主题: 暗色/亮色
  - 行为配置: 卸载前确认、清理空目录
  - 分类目录（F19）: 安装开关、分类清单、角色类/工作室类落点
  - Sideloader 日志路径（F23）: BepInEx LogOutput.log，自动探测失败时由用户指定
"""

from pathlib import Path
import json
from pydantic import BaseModel, Field

# F19-2 默认分类目录清单（分号分隔，社区主流布局）
DEFAULT_GAME_CATEGORIES = (
    "AdditionalPoses;BasisAndShaders;ForCharactersLoading;"
    "ForStudioScenesLoading;KK_-ExclusivesAndUncensors;KK_-ExtraMaps;MyMods"
)

# 常见 Sideloader 分类目录的中文说明（仅展示层注记：
# 目录创建/匹配/落盘一律用原始名，此表不参与任何逻辑判断）
CATEGORY_DESCRIPTIONS = {
    "AdditionalPoses": "追加姿势",
    "BasisAndShaders": "基础与着色器",
    "ForCharactersLoading": "角色加载用",
    "ForStudioScenesLoading": "工作室场景加载用",
    "KK_-ExclusivesAndUncensors": "KK 专属与去码",
    "KK_-ExtraMaps": "KK 额外地图",
    "MyMods": "我的 Mod",
}


def describe_category(name: str) -> str:
    """分类目录名 → 追加中文说明的展示文本；未知/自定义分类原样返回"""
    stripped = name.strip()
    desc = CATEGORY_DESCRIPTIONS.get(stripped)
    return f"{stripped}（{desc}）" if desc else name


class CategoryConfig(BaseModel):
    """游戏分类目录安装配置（F19，需求文档 7.3）"""

    game_category_enabled: bool = Field(
        default=True,
        description="分类目录安装开关：开启时安装到 {游戏Mod路径}/{分类}/{作者}/",
    )
    game_categories: str = Field(
        default=DEFAULT_GAME_CATEGORIES,
        description="分类目录名清单（分号分隔）",
    )
    category_chara: str = Field(
        default="ForCharactersLoading",
        description="角色类依赖的安装目标分类（人物/服装卡及场景卡【角色】）",
    )
    category_studio: str = Field(
        default="ForStudioScenesLoading",
        description="工作室类依赖的安装目标分类（场景卡【工作室】）",
    )

    @property
    def category_names(self) -> list[str]:
        """分类目录名列表（去空、保序去重）"""
        seen: set[str] = set()
        result: list[str] = []
        for name in self.game_categories.split(";"):
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        return result


class AppConfig(BaseModel):
    """应用全局配置"""

    # ── 路径配置（P0）──
    game_mod_path: str = Field(
        default="",
        description="游戏 Sideloader Mods 目录（Mod 安装目标）"
    )
    repo_path: str = Field(
        default="",
        description="本地 Mod 仓库路径（Mod 收藏目录）"
    )

    # ── Sideloader 日志（F23）──
    sideloader_log_path: str = Field(
        default="",
        description="BepInEx LogOutput.log 路径（从游戏日志读取缺失；"
                    "自动探测失败时由用户指定一次，设置页可改，留空回到自动探测）",
    )

    # ── 远程仓库（P2）──
    remote_repo_url: str = Field(
        default="",
        description="远程仓库 HTTP/HTTPS URL（可选）"
    )

    # ── UI 设置 ──
    theme: str = Field(
        default="dark",
        description="界面主题：dark / light"
    )
    window_width: int = Field(default=1350, description="窗口宽度")
    window_height: int = Field(default=800, description="窗口高度")

    # ── 行为配置 ──
    confirm_before_uninstall: bool = Field(
        default=True, description="卸载/移除前弹出确认对话框"
    )
    clean_empty_dirs: bool = Field(
        default=True, description="卸载/移除后清理空作者目录"
    )

    # ── 分类目录（F19）──
    category: CategoryConfig = Field(default_factory=CategoryConfig)

    # ── 版本 ──
    config_version: str = Field(default="1", description="配置文件版本号")


def _get_config_dir() -> Path:
    """获取配置目录"""
    import sys
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent.parent
    return base / "data"


def _get_config_path() -> Path:
    """获取配置文件路径"""
    return _get_config_dir() / "config.json"


def load_config() -> AppConfig:
    """加载配置，不存在时返回默认配置"""
    config_path = _get_config_path()
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return AppConfig(**data)
        except Exception:
            pass
    return AppConfig()


def save_config(config: AppConfig) -> None:
    """保存配置到文件"""
    config_dir = _get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(config.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# 全局单例
_config_instance: AppConfig | None = None


def get_config() -> AppConfig:
    """获取全局配置单例"""
    global _config_instance
    if _config_instance is None:
        _config_instance = load_config()
    return _config_instance


def reload_config() -> AppConfig:
    """重新加载配置（如设置修改后）"""
    global _config_instance
    _config_instance = load_config()
    return _config_instance
