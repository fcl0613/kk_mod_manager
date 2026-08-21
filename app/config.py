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

    # ── 更新检查 ──
    update_check_enabled: bool = Field(
        default=True, description="启动时自动检查更新"
    )
    update_last_check_at: str = Field(
        default="", description="最近一次检查更新的 ISO 时间戳"
    )
    update_skipped_version: str = Field(
        default="", description="用户选择跳过的版本"
    )
    update_notified_version: str = Field(
        default="", description="已提示过用户的版本"
    )
    update_check_interval_days: int = Field(
        default=1, description="自动检查间隔天数"
    )
    update_latest_version: str = Field(
        default="", description="最近一次检查到的最新版本号（含已是最新的情况）"
    )
    update_latest_url: str = Field(
        default="", description="最新版本的 Release 页面 URL"
    )
    update_latest_is_prerelease: bool = Field(
        default=False, description="最近记录的最新版本是否为预览版"
    )

    # ── 版本 ──
    config_version: str = Field(default="2", description="配置文件版本号")


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
    """加载配置，不存在时返回默认配置

    旧版本 config.json 自动升级：
      - Pydantic 用默认值填充缺失字段（无需显式迁移）
      - 若发现 config_version < "2"，主动写回新 schema 以便新字段落盘
    """
    config_path = _get_config_path()
    cfg = AppConfig()
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            cfg = AppConfig(**data)
        except Exception:
            pass

    # 配置版本升级：触发重写以落盘新字段
    if cfg.config_version != "2":
        cfg.config_version = "2"
        try:
            save_config(cfg)
        except Exception:
            pass

    return cfg


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
