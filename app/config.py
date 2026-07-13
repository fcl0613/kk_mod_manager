"""应用配置管理

配置存储在 exe 所在目录的 data/config.json（需求文档 8.3）。

需求文档 v1.3 配置项：
  - game_mod_path: 游戏 Mod 路径
  - repo_path: 本地 Mod 仓库路径
  - remote_repo_url: 远程仓库 URL（可选）
  - UI 主题: 暗色/亮色
  - 行为配置: 卸载前确认、清理空目录
"""

from pathlib import Path
import json
from pydantic import BaseModel, Field


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
