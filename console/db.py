"""控制面数据库(SQLite, 独立于 CreatorHub 的库)。"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

_engine = None


def init_db(db_path: str):
    global _engine
    _engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(_engine)
    # 轻量迁移: 为已有表补缺失列
    insp = inspect(_engine)
    for table in SQLModel.metadata.tables.values():
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            with _engine.begin() as conn:
                conn.execute(text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                    f'{col.type.compile(_engine.dialect)}'))
    return _engine


def get_session() -> Session:
    assert _engine is not None, "init_db() 未调用"
    return Session(_engine)