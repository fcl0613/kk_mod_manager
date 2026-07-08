# Mod 模型新增 `repo_relative_path` 字段 —— 影响范围与解决方案

> 目标：在 `Mod` 模型中新增 `repo_relative_path` 字段，专门用于**锁定仓库（REPO）中 Mod 的位置**。游戏内（GAME）记录继续按《游戏 Mod 路径 / 作者名 / mod 文件》的约定组织，不引入新字段、不改 GAME 端路径逻辑。

---

## 1. 背景：现状与痛点

### 1.1 字段定义（已落地）

[app/models/mod.py:77-78](app/models/mod.py#L77-L78)：

```python
# —— 仓库mod相对路径 ——
repo_relative_path: Mapped[str] = mapped_column(String(1000), default="", comment="仓库mod的相对路径")
```

- 类型：`VARCHAR(1000)`（刻意放宽深度上限，留出后续多层目录空间）
- 默认值：`""`（老数据兼容）
- 写入规则：见 §3.2
- 读取规则：见 §3.3

### 1.2 当前 REPO 路径是怎么算出来的

仓库 Mod 的磁盘路径**目前完全运行时拼接**，没有持久化：

```
repo_full_path = config.repo_path / {mod.author or "Unknown"} / {mod.current_file_name}
```

而 `get_author_dir()`（[app/utils/file_utils.py:150](app/utils/file_utils.py#L150)）的策略是：

1. 在 `repo_root` 下找**已存在的**同名目录（忽略大小写）→ 用找到的那个；
2. 找不到 → 用规范化作者名**创建/假设**目录。

这条链条有两个明确痛点：

- **作者目录一旦改名/合并，DB 里按 `author` 重建路径就会失联**。
- **`refresh_from_disk` 重扫后**只写 `file_name`，没保留「在仓库里的真实相对位置」，下次重装同 GUID 时无法反查原始位置。

### 1.3 GAME 端为什么不引入新字段

按你的明确决定：**游戏内 Mod 路径继续按《游戏 Mod 路径 / 作者名 / mod》组织**，原因是：

- GAME 端启用/禁用只切换 `.zipmod ↔ .zipmod_disabled`，单一作者目录下文件命名稳定；
- GAME 端目录结构由游戏本身（Sideloader）读取并按作者分类，外部工具不应对其目录布局做假设；
- 引入字段反而会增加误改游戏目录结构的风险。

因此本次方案**只在 REPO 端**维护 `repo_relative_path`，GAME 端所有写盘逻辑保持原状。

---

## 2. 影响范围（按文件）

### 2.1 数据层

| 文件 | 行号 | 当前状态 | 改动 |
|---|---|---|---|
| [app/models/mod.py](app/models/mod.py) | 77-78 | ✅ 字段已添加 | **已完成**；同步更新 `to_dict()`（106-120 行）导出新字段 |
| [app/models/__init__.py](app/models/__init__.py) | 全文 | 仅 re-export 现有符号 | 不变 |

### 2.2 服务层 —— 关键改动集中在 `mod_service.py`

[app/services/mod_service.py](app/services/mod_service.py)（1177 行）按「只动 REPO 端」原则重新划分：

| 方法 | 行号 | 是否改 | 改什么 |
|---|---|---|---|
| `install_to_game` | 107-250 | ❌ 不改 | GAME 端不引入字段 |
| `add_to_repo` | 256-382 | ✅ **必改** | REPO 写入时计算 `repo_relative_path = dest_file.relative_to(repo_root).as_posix()` |
| `uninstall` | 426-476 | ✅ **必改** | REPO 分支优先用 `repo_relative_path`；GAME 分支保持原样 |
| `set_enabled` | 482-550 | ❌ 不改 | 仅作用于 GAME 端，不涉及仓库 |
| `install_from_repo` | 556-662 | ✅ **必改** | 复制成功后**新建的 GAME 记录**不写 `repo_relative_path`（GAME 不需要）；但**源 REPO 记录的 `repo_relative_path` 必须透传校验**：复制前断言源文件存在 |
| `add_game_mod_to_repo` | 668-774 | ✅ **必改** | 复制成功后**新建的 REPO 记录**写入 `repo_relative_path` |
| `refresh_from_disk` | 857-918 | ✅ **必改** | REPO 端扫描时计算 `repo_relative_path = zipmod_file.relative_to(repo_root).as_posix()`；GAME 端扫描行为不变 |
| `batch_install` / `_batch_install_single` | 955-1144 | ✅ **条件改** | 仅当 `location == REPO` 时写入；GAME 分支保持原状 |
| `_remove_mod_files` | 1150-1156 | ✅ **必改** | 增加 `mod.location == REPO` 时优先用 `repo_relative_path` 的逻辑 |

[app/services/manifest_service.py](app/services/manifest_service.py) —— 仅按 `guid` + `location` 做重复检测，**不动**。

[app/services/card_service.py](app/services/card_service.py) —— 同上，**不动**。

### 2.3 工具层

[app/utils/file_utils.py](app/utils/file_utils.py) 新增两个辅助函数（不替代 `get_author_dir`，仅作配套）：

- `compute_repo_relative_path(repo_root: Path, file_path: Path) -> str` —— 计算 `file_path.relative_to(repo_root).as_posix()`
- `resolve_repo_mod_path(mod: Mod, repo_root: Path) -> Path` —— 优先用 `mod.repo_relative_path`，为空时回退到 `get_author_dir(...) / mod.current_file_name`

### 2.4 UI 层

| 文件 | 行号 | 改动 |
|---|---|---|
| [app/ui/mod_detail_panel.py](app/ui/mod_detail_panel.py) | 64-72（字段列表）、156-162（full_path 拼装） | 字段列表里**仅 REPO 记录**额外渲染「仓库相对路径」；GAME 记录完全维持现状。`full_path` 在 REPO 分支改用 `repo_relative_path` 拼接，GAME 分支维持原公式 |
| [app/ui/main_window.py](app/ui/main_window.py) | 713-740 | **不改**（列表项不展示路径） |
| [app/ui/batch_install_dialog.py](app/ui/batch_install_dialog.py) | 全文 | **不改** |

### 2.5 数据库初始化 / 迁移

| 文件 | 改动 |
|---|---|
| [app/database.py](app/database.py) | `init_db()` 不变；`create_all` 会自动加列 |
| [scripts/migrate_repo_relative_path.py](scripts/migrate_repo_relative_path.py) | **新增**：幂等迁移脚本，给存量库 `ALTER TABLE mods ADD COLUMN repo_relative_path VARCHAR(1000) DEFAULT ''`，并按磁盘路径回填 REPO 记录 |

[app/ui/settings_dialog.py](app/ui/settings_dialog.py) —— 重置路径下会重建表，**不强制改**。

### 2.6 文档

| 文件 | 改动 |
|---|---|
| [需求文档.md](需求文档.md) §8.1 数据需求 | 在字段表里加 `repo_relative_path`，明确「仅 REPO 端使用，GAME 端为空」 |
| [开发文档.md](开发文档.md) §7.1 数据结构 | 同步更新 `Mod` ORM 定义 |

### 2.7 测试

[tests/card_service_test.py](tests/card_service_test.py) —— 不动。

**新增** [tests/mod_service_test.py](tests/mod_service_test.py)：
- REPO Mod 写入后 `repo_relative_path` 等于 `dest_file.relative_to(repo_root).as_posix()`
- GAME Mod 写入后 `repo_relative_path == ""`（明确不写入）
- `uninstall` 仓库 Mod 优先用 `repo_relative_path` 定位
- `add_game_mod_to_repo` 复制成功后 REPO 记录正确携带 `repo_relative_path`
- 迁移脚本幂等（再次执行不抛错）

---

## 3. 解决方案

### 3.1 字段语义（契约）

| 项 | 规则 |
|---|---|
| 字段名 | `repo_relative_path` |
| 类型 | `VARCHAR(1000)` |
| 默认值 | `""` |
| 适用记录 | **仅 `location == REPO`** |
| GAME 记录 | 一律为空字符串，写入时**不填充** |
| 格式 | POSIX 风格相对路径，如 `Makise/[Makise]GS-Alice.zipmod`、`subdir/Makise/[Makise]GS-Alice.zipmod` |
| 含义 | 相对于 `config.repo_path` 的路径；启用/禁用切换时**不动**（仓库没有启用/禁用概念，详见需求文档 §F08-5） |
| 解析回退 | `""` 时用 `get_author_dir(repo_root, mod.author) / mod.current_file_name` 兜底 |

### 3.2 写入策略

**REPO 端必填**：

| 入口 | 写入逻辑 |
|---|---|
| `add_to_repo` | `repo_relative_path = compute_repo_relative_path(repo_root, dest_file)` |
| `add_game_mod_to_repo`（新建 REPO 记录时） | 同上：`compute_repo_relative_path(repo_root, dest_file)` |
| `refresh_from_disk`（REPO 端） | `repo_relative_path = zipmod_file.relative_to(repo_root).as_posix()` |
| `_batch_install_single`（`location == REPO`） | 同 `add_to_repo` |

**GAME 端一律不写**：所有 `Mod(location=GAME, ...)` 构造时**不传** `repo_relative_path`，让它保持默认 `""`。

**特别说明 —— `set_enabled` 不需要处理本字段**：
- `set_enabled` 仅对 `mod.location == ModLocation.GAME` 起作用（见 [mod_service.py:493-497](app/services/mod_service.py#L493)）；
- REPO 记录没有启用/禁用切换，不会改 `file_name`，因此 `repo_relative_path` 在 REPO 端是稳定的。

### 3.3 读取策略 —— `resolve_repo_mod_path`

在 [app/utils/file_utils.py](app/utils/file_utils.py) 新增：

```python
def compute_repo_relative_path(repo_root: Path, file_path: Path) -> str:
    """计算仓库 Mod 文件相对于仓库根的相对路径（POSIX 风格）。"""
    return Path(file_path).resolve().relative_to(Path(repo_root).resolve()).as_posix()


def resolve_repo_mod_path(mod: Mod, repo_root: Path) -> Path:
    """解析仓库 Mod 的磁盘绝对路径。

    优先使用 mod.repo_relative_path；为空时回退到旧的"作者目录/文件名"公式。
    """
    repo_root = Path(repo_root)
    if mod.repo_relative_path:
        return repo_root / mod.repo_relative_path
    # 回退：兼容老数据（迁移前 + 找不到文件的孤儿记录）
    author_dir = get_author_dir(repo_root, mod.author)
    return author_dir / mod.current_file_name
```

**GAME 端不要复用这个函数**，继续用：

```python
author_dir = get_author_dir(Path(config.game_mod_path), mod.author)
file_path = author_dir / mod.current_file_name
```

### 3.4 各方法的具体改动

#### `add_to_repo`（[mod_service.py:256](app/services/mod_service.py#L256)）

```python
# 第 360 行附近，Mod 构造时新增一行
mod = Mod(
    guid=manifest.guid,
    ...
    file_name=dest_file.name,
    location=ModLocation.REPO,
    status=ModStatus.ENABLED,
    installed_at=datetime.now(),
    repo_relative_path=compute_repo_relative_path(repo_root, dest_file),  # ← 新增
)
```

#### `uninstall`（[mod_service.py:426](app/services/mod_service.py#L426)）

```python
# 第 434-441 行改写
config = get_config()

if mod.location == ModLocation.GAME:
    # GAME 端保持原状，不动
    base_path = config.game_mod_path
    author_dir = get_author_dir(Path(base_path), mod.author)
    file_path = author_dir / mod.current_file_name
else:
    # REPO 端优先用 repo_relative_path
    base_path = config.repo_path
    file_path = resolve_repo_mod_path(mod, Path(base_path))
```

#### `add_game_mod_to_repo`（[mod_service.py:668](app/services/mod_service.py#L668)）

```python
# 第 745-757 行附近，新建 REPO Mod 时写入 repo_relative_path
repo_mod = Mod(
    guid=game_mod.guid,
    ...
    file_name=dest_file.name,
    location=ModLocation.REPO,
    status=ModStatus.ENABLED,
    installed_at=datetime.now(),
    repo_relative_path=compute_repo_relative_path(repo_path, dest_file),  # ← 新增
)
```

#### `install_from_repo`（[mod_service.py:556](app/services/mod_service.py#L556)）

```python
# 第 605-606 行：复制源文件时优先用 repo_relative_path 定位
src_file = resolve_repo_mod_path(repo_mod, repo_path)

# 第 633-645 行：新建 GAME Mod 时不传 repo_relative_path（保持默认 ""）
game_mod = Mod(
    ...
    location=ModLocation.GAME,
    # repo_relative_path 不传，保持 ""
)
```

#### `refresh_from_disk`（[mod_service.py:857](app/services/mod_service.py#L857)）

```python
# 第 899-913 行附近
mod = Mod(
    guid=manifest.guid,
    ...
    file_name=zipmod_file.name,
    location=location,
    status=ModStatus.ENABLED,
    installed_at=datetime.fromtimestamp(zipmod_file.stat().st_mtime),
    repo_relative_path=(
        zipmod_file.relative_to(base_path).as_posix()
        if location == ModLocation.REPO else ""   # ← GAME 端不写
    ),
)
```

#### `_batch_install_single`（[mod_service.py:1035](app/services/mod_service.py#L1035)）

```python
# 第 1112-1124 行附近
mod = Mod(
    ...
    file_name=dest_file.name,
    location=location,
    status=ModStatus.ENABLED,
    installed_at=datetime.now(),
    repo_relative_path=(
        compute_repo_relative_path(base_path, dest_file)
        if location == ModLocation.REPO else ""   # ← GAME 端不写
    ),
)
```

#### `_remove_mod_files`（[mod_service.py:1150](app/services/mod_service.py#L1150)）

```python
@staticmethod
def _remove_mod_files(mod: Mod, base_path: str) -> None:
    """删除 Mod 对应的磁盘文件"""
    base = Path(base_path)
    if mod.location == ModLocation.REPO and mod.repo_relative_path:
        # REPO 端优先用精确路径
        file_path = base / mod.repo_relative_path
    else:
        # GAME 端 或 老数据回退
        author_dir = get_author_dir(base, mod.author)
        file_path = author_dir / mod.current_file_name
    if file_path.exists():
        delete_file(file_path)
```

#### `_log_operation`（[mod_service.py:1158](app/services/mod_service.py#L1158)）

**不改**，日志用现有 `detail` 字段即可；如有需要可在 `repo_add` 的 detail 末尾追加 `→ {repo_relative_path}`，但非必需。

### 3.5 兼容性 & 回填

**老数据怎么办**：已有 REPO 记录的 `repo_relative_path` 都是 `""`，需要回填：

1. **迁移脚本主动回填**（[scripts/migrate_repo_relative_path.py](scripts/migrate_repo_relative_path.py)）：
   - `ALTER TABLE mods ADD COLUMN repo_relative_path VARCHAR(1000) DEFAULT ''`（列已存在则跳过，幂等）
   - 遍历所有 `location == REPO` 且 `repo_relative_path == ""` 的记录
   - 对每条记录尝试 `resolve_repo_mod_path()` 找到磁盘文件，命中则写回 `repo_relative_path`
   - **不创建任何目录**，找不到的保留 `""`，运行时仍走 `get_author_dir` 公式
2. **懒回填兜底**（在 `uninstall` / `_remove_mod_files` / `refresh_from_disk` 成功后）：
   - 旧记录首次成功删除/重扫时，**顺手**把 `repo_relative_path` 写回 DB

### 3.6 数据库迁移脚本

[scripts/migrate_repo_relative_path.py](scripts/migrate_repo_relative_path.py)：

```python
"""为 mods 表新增 repo_relative_path 列，并按磁盘路径回填 REPO 记录。

运行：python -m scripts.migrate_repo_relative_path
幂等：列已存在则跳过；记录已有 repo_relative_path 则跳过。
GAME 记录的 repo_relative_path 保持空字符串。
"""
from pathlib import Path

from sqlalchemy import text

from app.database import get_engine, get_session
from app.models.mod import Mod, ModLocation
from app.config import get_config


def migrate_schema() -> bool:
    """为 mods 表新增 repo_relative_path 列（幂等）。"""
    engine = get_engine()
    with engine.begin() as conn:
        cols = conn.execute(text("PRAGMA table_info(mods)")).fetchall()
        if any(c[1] == "repo_relative_path" for c in cols):
            print("[跳过] repo_relative_path 列已存在")
            return False
        conn.execute(text(
            "ALTER TABLE mods ADD COLUMN repo_relative_path VARCHAR(1000) DEFAULT ''"
        ))
        print("[完成] 新增 repo_relative_path 列")
        return True


def backfill_repo_records() -> int:
    """回填 REPO 记录的 repo_relative_path。"""
    config = get_config()
    repo_root = Path(config.repo_path) if config.repo_path else None
    if not repo_root or not repo_root.exists():
        print(f"[跳过] 仓库路径未配置或不存在：{config.repo_path}")
        return 0

    session = get_session()
    filled = 0
    try:
        # 仅回填 REPO 记录
        for mod in session.query(Mod).filter(
            Mod.location == ModLocation.REPO,
            Mod.repo_relative_path == "",
        ).all():
            author_dir = repo_root / (mod.author or "Unknown")
            for candidate in [
                author_dir / mod.current_file_name,
                author_dir / mod.file_name,
            ]:
                if candidate.exists():
                    mod.repo_relative_path = candidate.relative_to(repo_root).as_posix()
                    filled += 1
                    break
        session.commit()
        print(f"[完成] 回填 {filled} 条 REPO 记录")
        return filled
    finally:
        session.close()


if __name__ == "__main__":
    migrate_schema()
    backfill_repo_records()
    print("迁移完成")
```

### 3.7 UI 调整

[app/ui/mod_detail_panel.py](app/ui/mod_detail_panel.py) 字段列表（64-72 行）改为：

```python
for key in ["name", "guid", "version", "author", "description",
             "website", "file_name", "location", "status",
             "installed_at", "full_path", "repo_relative_path"]:
    ...
```

渲染逻辑（156-162 行）：

```python
# 仓库相对路径（仅 REPO 记录有意义）
if mod.location == ModLocation.REPO:
    if mod.repo_relative_path:
        self.fields["repo_relative_path"].setText(
            f"<b>仓库相对路径：</b>{mod.repo_relative_path}"
        )
    else:
        self.fields["repo_relative_path"].setText(
            "<b>仓库相对路径：</b><span style='color:#888'>(未记录，按作者目录定位)</span>"
        )
    self.fields["repo_relative_path"].setVisible(True)
else:
    # GAME 端隐藏该字段
    self.fields["repo_relative_path"].setVisible(False)

# 完整路径
if mod.location == ModLocation.REPO and mod.repo_relative_path:
    full_path = f"{config.repo_path}/{mod.repo_relative_path}"
else:
    base_path = (
        config.game_mod_path if mod.location == ModLocation.GAME
        else config.repo_path
    )
    full_path = f"{base_path}/{mod.author or 'Unknown'}/{mod.current_file_name}"
self.fields["full_path"].setText(f"<b>完整路径：</b>{full_path}")
```

### 3.8 文档同步

- [需求文档.md](需求文档.md) §8.1 数据需求表新增一行：
  ```
  | repo_relative_path | VARCHAR(1000) | 仓库 Mod 相对路径（仅 REPO 端使用，GAME 端为空） | 否（默认 ""） |
  ```
- [开发文档.md](开发文档.md) §7.1 ORM 模型定义同步；说明 REPO 端通过 `resolve_repo_mod_path()` 定位文件，GAME 端维持 `get_author_dir() + file_name` 不变。

---

## 4. 实施步骤（建议顺序）

1. **字段已落地** —— [app/models/mod.py:77-78](app/models/mod.py#L77-L78) ✅
2. **同步 `to_dict()`** —— [app/models/mod.py:106-120](app/models/mod.py#L106-L120) 把 `repo_relative_path` 加入导出
3. **工具函数** —— [app/utils/file_utils.py](app/utils/file_utils.py) 新增 `compute_repo_relative_path` 与 `resolve_repo_mod_path`
4. **服务层 6 处改造** —— 按 §3.4 表格改 `add_to_repo` / `uninstall` / `add_game_mod_to_repo` / `install_from_repo` / `refresh_from_disk` / `_batch_install_single` / `_remove_mod_files`
5. **UI 渲染** —— [app/ui/mod_detail_panel.py](app/ui/mod_detail_panel.py) 字段列表 + REPO 分支展示
6. **迁移脚本** —— [scripts/migrate_repo_relative_path.py](scripts/migrate_repo_relative_path.py)
7. **懒回填** —— `uninstall` / `_remove_mod_files` / `refresh_from_disk` 成功路径上「写回 `repo_relative_path`」
8. **测试** —— [tests/mod_service_test.py](tests/mod_service_test.py) 覆盖 §2.7 列出的 5 个用例
9. **文档** —— 更新 [需求文档.md](需求文档.md) §8.1 与 [开发文档.md](开发文档.md) §7.1

---

## 5. 风险与回退

| 风险 | 缓解 |
|---|---|
| 迁移脚本误改 author 目录 | `backfill` 阶段只在 `candidate.exists()` 时才写入，**绝不创建新目录** |
| `repo_relative_path` 路径分隔符跨平台不一致 | 全部用 `Path.relative_to(...).as_posix()` 强制 POSIX 风格；解析时也按 POSIX 拼回 |
| `refresh_from_disk` 把 GAME 端 `repo_relative_path` 误填 | 显式 `if location == ModLocation.REPO else ""` 保护 |
| 老数据 `repo_relative_path == ""` 走兜底公式但作者目录也找不到了 | 兜底公式失败时 `_remove_mod_files` 不会抛错（只 log），与改前行为一致 |
| `set_enabled` 不慎写到仓库记录 | 现状已有 `if mod.location != ModLocation.GAME: return InstallResult(success=False, ...)` 守卫（[mod_service.py:493](app/services/mod_service.py#L493)），本次不动 |

**回退方案**：清空所有 REPO 记录的 `repo_relative_path` 字段，行为与改动前完全一致；GAME 端永远不依赖该字段，无任何回归。

---

## 6. 受影响文件汇总

**已完成**：
- [app/models/mod.py](app/models/mod.py) —— 字段已加（第 77-78 行）

**必须改**：
- [app/models/mod.py](app/models/mod.py) —— `to_dict()` 导出
- [app/services/mod_service.py](app/services/mod_service.py) —— 7 处方法（§3.4）
- [app/utils/file_utils.py](app/utils/file_utils.py) —— 2 个辅助函数
- [app/ui/mod_detail_panel.py](app/ui/mod_detail_panel.py)

**新增**：
- [scripts/migrate_repo_relative_path.py](scripts/migrate_repo_relative_path.py)
- [tests/mod_service_test.py](tests/mod_service_test.py)

**建议改**：
- [需求文档.md](需求文档.md) §8.1
- [开发文档.md](开发文档.md) §7.1

**不需要改**（已确认）：
- [app/database.py](app/database.py) —— `create_all` 自动加列
- [app/models/__init__.py](app/models/__init__.py)
- [app/services/manifest_service.py](app/services/manifest_service.py)
- [app/services/card_service.py](app/services/card_service.py)
- [app/ui/main_window.py](app/ui/main_window.py)
- [app/ui/batch_install_dialog.py](app/ui/batch_install_dialog.py)
- [app/ui/settings_dialog.py](app/ui/settings_dialog.py)
- [tests/card_service_test.py](tests/card_service_test.py)
- `mod_service.install_to_game`、`mod_service.set_enabled` —— 严格 GAME 端逻辑，与 REPO 字段无关