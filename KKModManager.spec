# -*- mode: python ; coding: utf-8 -*-
"""KK Mod Manager — PyInstaller 打包配置

用法:
    pyinstaller KKModManager.spec

输出:
    dist/KKModManager.exe
"""

import sys
from pathlib import Path

# ── 项目根目录 ──
PROJECT_ROOT = Path(SPECPATH).resolve()

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        # 暗色主题 QSS
        (str(PROJECT_ROOT / "assets"), "assets"),
    ],
    hiddenimports=[
        # PyQt6 平台插件
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.QtNetwork",  # 更新检查服务用
        "app",  # 确保 app/__init__.py 中的 __version__ 被识别
        # 数据库
        "sqlalchemy",
        "sqlalchemy.ext.declarative",
        # 配置 & 日志
        "pydantic",
        "loguru",
        # 版本比较
        "packaging",
        "packaging.version",
        "packaging.specifiers",
        "packaging.requirements",
        # 图片处理
        "PIL",
        "PIL.Image",
        # 角色卡解析
        "kkloader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="KKModManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI 应用，不显示终端窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # 如有 ico 文件可填: str(PROJECT_ROOT / "icon.ico")
)
