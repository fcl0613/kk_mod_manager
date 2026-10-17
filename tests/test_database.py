"""数据库 schema 迁移测试（F19-8：幂等补列）。"""

from sqlalchemy import create_engine, inspect, text

from app.database import _run_schema_migration


# v1.5 之前的老 mods 表（缺 repo_relative_path 历史欠账与 game_relative_path）
_LEGACY_SCHEMA = """
CREATE TABLE mods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guid VARCHAR(200) NOT NULL,
    name VARCHAR(200) NOT NULL,
    version VARCHAR(50) NOT NULL,
    author VARCHAR(100),
    file_name VARCHAR(300) NOT NULL,
    location VARCHAR(10) NOT NULL,
    status VARCHAR(10),
    installed_at DATETIME,
    notes VARCHAR(1000)
)
"""


def _columns(engine, table: str = "mods") -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migration_adds_missing_columns():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text(_LEGACY_SCHEMA))
        conn.commit()

    _run_schema_migration(engine)
    cols = _columns(engine)
    assert "repo_relative_path" in cols   # 历史欠账
    assert "game_relative_path" in cols   # F19 新增


def test_migration_idempotent():
    """二次运行不重复加列、不抛错"""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text(_LEGACY_SCHEMA))
        conn.commit()

    _run_schema_migration(engine)
    _run_schema_migration(engine)
    legacy_cols = {
        "id", "guid", "name", "version", "author", "file_name",
        "location", "status", "installed_at", "notes",
    }
    assert _columns(engine) == legacy_cols | {"repo_relative_path", "game_relative_path"}


def test_migration_skips_when_table_absent():
    """表不存在（首次运行由 create_all 建表）时安全跳过"""
    engine = create_engine("sqlite://")
    _run_schema_migration(engine)  # 不应抛错
    assert "mods" not in inspect(engine).get_table_names()


def test_migration_noop_on_current_schema():
    """新 schema（列已齐全）时为空操作"""
    from app.database import Base
    import app.models.mod  # noqa: F401 — 确保 Mod 模型注册到 Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    before = _columns(engine)
    _run_schema_migration(engine)
    assert _columns(engine) == before
