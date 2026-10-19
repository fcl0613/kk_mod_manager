"""数据库初始化与 Session 管理

数据文件存储在 exe 所在目录的 data/ 目录下（需求文档 8.3）。
"""

from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session


class Base(DeclarativeBase):
    """SQLAlchemy ORM 基类"""
    pass


def get_data_dir() -> Path:
    """获取数据目录路径（exe 所在目录/data/）

    优先使用 exe 所在目录（打包后），回退到当前工作目录。
    """
    import sys
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后
        base = Path(sys.executable).parent
    else:
        # 开发模式：使用项目根目录
        base = Path(__file__).parent.parent
    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_db_path() -> Path:
    """获取数据库文件路径"""
    return get_data_dir() / "mods.db"


# 全局引擎和 Session 工厂
_engine = None
_SessionLocal = None


def get_engine():
    """获取（懒创建）数据库引擎"""
    global _engine
    if _engine is None:
        db_path = get_db_path()
        _engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},  # SQLite 多线程支持
        )
    return _engine


def get_session_factory():
    """获取 Session 工厂"""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal


def _run_schema_migration(engine) -> None:
    """幂等补齐 mods 表的历史欠账列（F19-8）。

    create_all 不会给已存在的表加列，存量库需用
    PRAGMA table_info 检查后 ALTER TABLE ADD COLUMN：
      - repo_relative_path: 历史欠账
      - game_relative_path: F19 新增
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.execute(text("PRAGMA table_info(mods)"))
        }
        # 表不存在（首次运行由 create_all 建表）时无需迁移
        if not existing:
            return
        for col in ("repo_relative_path", "game_relative_path"):
            if col not in existing:
                conn.execute(
                    text(f"ALTER TABLE mods ADD COLUMN {col} VARCHAR(1000)")
                )
        conn.commit()


def init_db():
    """创建所有表（首次运行或模型变更时调用），并补齐存量库缺失列"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _run_schema_migration(engine)


def get_session() -> Session:
    """获取一个新的数据库 Session"""
    return get_session_factory()()
