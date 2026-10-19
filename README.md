# KK Mod Manager

Illusion 游戏（Koikatsu / Koikatsu Sunshine）桌面端 Sideloader Mod 管理与卡片解析工具，基于 PyQt6。

维护一个本地 Mod 仓库，把 `.zipmod` 安装到游戏目录 / 从游戏卸载，并解析角色卡 / 服装卡 PNG，找出缺失的 Mod 依赖。

## 功能特性

- **Mod 管理**：安装 = 复制 `.zipmod` 到 `{游戏路径}/{作者名}/`（绝不解压）；卸载 = 删除文件并清理空作者目录
- **启用 / 禁用**：`.zipmod` ↔ `.zipmod_disabled` 改名实现，仅对游戏中的 Mod 生效
- **本地仓库**：游戏 ↔ 仓库双向搬运、安装并收藏一步到位
- **manifest 解析**：兼容 UTF-8 / UTF-16 BOM 编码；按 GUID + 语义化版本做重复检测，升级 / 降级需确认，同版本阻止
- **批量安装**：后台线程不卡界面，同版本自动跳过、版本冲突单独标记
- **磁盘同步**：以磁盘为准增量对齐数据库（保留备注与安装时间），解析失败的文件不误删；另提供全量重扫
- **卡片解析**：拖入角色卡 / 服装卡 PNG，提取依赖 Mod 的 GUID，比对游戏与仓库，标记 已安装 / 仓库有 / 缺失
- **KKS → KK 转换**：把二代卡转成一代卡
- 其他：关键字搜索、按作者分组、分页、统计、操作日志、深色主题

## 环境要求

- Python 3.11+
- Windows（在其他平台未测试）

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行（开发）
python main.py
```

首次启动后在设置中配置「游戏 Mod 路径」与「本地 Mod 仓库路径」。

## 打包

```bash
pyinstaller KKModManager.spec
```

## 运行测试

```bash
# 安装开发依赖（含 pytest）
pip install -r requirements-dev.txt

# 运行单元测试（服务层 / 工具层 / 模型，内存库 + 临时目录，不触碰真实数据）
python -m pytest

# 只跑某个文件 / 某个用例
python -m pytest tests/test_mod_service.py
python -m pytest tests/test_mod_service.py::TestSetEnabled
```

`tests/sync_service_test.py` 是 `sync_from_disk` 的手动测试脚本（14 个场景），单独运行：

```bash
python tests/sync_service_test.py
```

## 项目结构

```
main.py                  # 入口
app/
├── config.py            # Pydantic 配置单例（data/config.json）
├── database.py          # SQLAlchemy 引擎 / 会话
├── models/mod.py        # Mod / OperationLog ORM 模型
├── services/
│   ├── mod_service.py       # 核心业务：安装/卸载/启停/批量/磁盘同步
│   ├── manifest_service.py  # manifest.xml 解析 + 重复检测
│   └── card_service.py      # 角色/服装卡解析 + 可用性检查
├── utils/file_utils.py  # 文件操作（安全复制、改名、作者目录等）
└── ui/                  # PyQt6 界面
tests/                   # pytest 测试套件 + 手动测试脚本
docs/                    # 需求 / 开发文档
```

## 核心设计

- **`.zipmod` 永不解压**：安装 = 复制文件，卸载 = 删除文件
- **同一 GUID 允许两条记录**：游戏与仓库各一条，`(location, guid)` 唯一索引
- **数据落盘**：SQLite（`data/mods.db`）+ JSON 配置（`data/config.json`），均在 `data/` 下
- **作者目录大小写不敏感**：安装时优先复用已有目录，避免产生大小写不同的重复目录

## 致谢

- [great-majority/KoikatuCharaLoader](https://github.com/great-majority/KoikatuCharaLoader) —— 卡片解析依赖的 `kkloader` 库
