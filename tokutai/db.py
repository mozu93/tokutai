"""SQLite（SQLAlchemy）のモデルとセッション。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from sqlalchemy import Date, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import paths

STATUS_RECEIVED = "書類待ち"
STATUS_ARRIVED = "書類到着"
STATUS_DONE = "処理済"
STATUS_DEFECT = "不備（補正待ち）"


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"
    case_no: Mapped[str] = mapped_column(String(40), primary_key=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    docs_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    done_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    defect_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    @property
    def status(self) -> str:
        if self.done_date:
            return STATUS_DONE
        if self.docs_date:
            return STATUS_ARRIVED
        if self.defect_date:
            return STATUS_DEFECT
        return STATUS_RECEIVED


class ProcessedMail(Base):
    __tablename__ = "processed_mails"
    mail_id: Mapped[str] = mapped_column(String(400), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    case_no: Mapped[str] = mapped_column(String(40), default="")


class NotifyLog(Base):
    __tablename__ = "notify_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))
    notified_on: Mapped[date] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("case_no", "kind", "notified_on"),)


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


_engine = None
_factory: sessionmaker | None = None


def _migrate(engine) -> None:
    """既存のDBに、後から増えた列を足す。"""
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(cases)")}
        if "defect_date" not in cols:
            conn.exec_driver_sql("ALTER TABLE cases ADD COLUMN defect_date DATE")


def init(url: str | None = None) -> None:
    global _engine, _factory
    _engine = create_engine(url or f"sqlite:///{paths.db_path()}", future=True)
    Base.metadata.create_all(_engine)
    _migrate(_engine)
    _factory = sessionmaker(_engine, expire_on_commit=False)


def session() -> Session:
    if _factory is None:
        init()
    return _factory()  # type: ignore[misc]


def get_meta(s: Session, key: str, default: str = "") -> str:
    m = s.get(Meta, key)
    return m.value if m else default


def set_meta(s: Session, key: str, value: str) -> None:
    m = s.get(Meta, key)
    if m:
        m.value = value
    else:
        s.add(Meta(key=key, value=value))


def daily_backup(keep: int = 30, today: date | None = None) -> bool:
    """1日1回、データベースのコピーを保存する。新しく作ったときだけ True を返す。

    使用中でも整合したコピーが取れるよう、SQLite のバックアップ機能を使う。
    起動時と、同期のたびに呼ぶ（トレイに常駐して再起動しなくても、毎日取れるようにするため）。
    """
    src = _engine.url.database if _engine is not None else str(paths.db_path())
    if not src or not Path(src).exists():
        return False
    dst = paths.backup_dir() / f"tokutai_{(today or date.today()):%Y%m%d}.db"
    made = False
    if not dst.exists():
        tmp = dst.with_suffix(".tmp")
        with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(tmp)) as b:
            a.backup(b)
        tmp.replace(dst)
        made = True
    for old in sorted(paths.backup_dir().glob("tokutai_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return made
