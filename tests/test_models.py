"""Mod / OperationLog ORM 模型测试（属性、默认值、唯一索引）。"""

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.mod import Mod, ModLocation, ModStatus, OperationLog


def _mod(**kwargs) -> Mod:
    """构造一行最小合法 Mod（字段可覆盖）"""
    base = dict(
        guid="guid.x",
        name="ModX",
        version="1.0.0",
        file_name="ModX.zipmod",
        location=ModLocation.GAME,
    )
    base.update(kwargs)
    return Mod(**base)


# ── 文件名属性 ──────────────────────────────────────────────

def test_is_disabled_file_adds_suffix():
    mod = _mod(file_name="ModX.zipmod")
    assert mod.is_disabled_file == "ModX.zipmod_disabled"


def test_is_disabled_file_already_disabled_unchanged():
    mod = _mod(file_name="ModX.zipmod_disabled")
    assert mod.is_disabled_file == "ModX.zipmod_disabled"


def test_current_file_name_follows_status():
    enabled = _mod(status=ModStatus.ENABLED)
    assert enabled.current_file_name == "ModX.zipmod"
    disabled = _mod(status=ModStatus.DISABLED)
    assert disabled.current_file_name == "ModX.zipmod_disabled"


# ── to_dict ─────────────────────────────────────────────────

def test_to_dict_fields(db_session):
    ts = datetime(2024, 5, 1, 12, 30, 0)
    mod = _mod(
        author="Alice", notes="备注", repo_relative_path="Alice/ModX.zipmod",
        installed_at=ts,
    )
    db_session.add(mod)
    db_session.commit()

    d = mod.to_dict()
    assert set(d.keys()) == {
        "id", "guid", "name", "version", "author", "description", "website",
        "file_name", "location", "status", "installed_at", "notes",
        "repo_relative_path",
    }
    assert d["location"] == "game"
    assert d["status"] == "enabled"
    assert d["installed_at"] == ts.isoformat()
    assert d["notes"] == "备注"


def test_to_dict_installed_at_none_gets_default(db_session):
    """显式 None 在 flush 时仍触发 Python 端默认值（非 NULL），
    to_dict 因此总是输出 isoformat 字符串（非空列的防御分支）"""
    mod = _mod(installed_at=None)
    db_session.add(mod)
    db_session.commit()
    assert isinstance(mod.installed_at, datetime)
    assert mod.to_dict()["installed_at"] == mod.installed_at.isoformat()


def test_mod_repr_contains_guid_and_name():
    # 列默认值在 flush 前不生效，repr 需要 status/location 就绪 → 显式传
    r = repr(_mod(name="测试Mod", status=ModStatus.ENABLED))
    assert "guid.x" in r and "测试Mod" in r


# ── 唯一索引 / 双位置共存 ───────────────────────────────────

def test_same_guid_two_locations_coexist(db_session):
    db_session.add(_mod(location=ModLocation.GAME))
    db_session.add(_mod(location=ModLocation.REPO))
    db_session.commit()

    rows = db_session.query(Mod).filter(Mod.guid == "guid.x").all()
    assert len(rows) == 2
    assert {r.location for r in rows} == {ModLocation.GAME, ModLocation.REPO}


def test_unique_index_same_location_rejects(db_session):
    db_session.add(_mod(location=ModLocation.GAME, guid="guid.dup"))
    db_session.commit()

    db_session.add(_mod(location=ModLocation.GAME, guid="guid.dup"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert db_session.query(Mod).filter(Mod.guid == "guid.dup").count() == 1


# ── 列默认值 ────────────────────────────────────────────────

def test_mod_column_defaults(db_session):
    mod = _mod()  # 未显式给 status / author / notes / repo_relative_path
    db_session.add(mod)
    db_session.commit()

    assert mod.status == ModStatus.ENABLED
    assert mod.author == ""
    assert mod.author_normalized == ""
    assert mod.description == ""
    assert mod.website == ""
    assert mod.notes == ""
    assert mod.repo_relative_path == ""
    assert isinstance(mod.installed_at, datetime)
    assert mod.id is not None


def test_operation_log_defaults(db_session):
    log = OperationLog(
        operation="install", mod_name="ModX", mod_guid="guid.x", location="game",
    )
    db_session.add(log)
    db_session.commit()

    assert log.result == "success"
    assert log.detail == ""
    assert isinstance(log.created_at, datetime)
