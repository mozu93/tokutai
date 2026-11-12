"""SQLite（SQLAlchemy）のモデルとセッション。"""
from __future__ import annotations

import shutil
from datetime import date

from sqlalchemy import Date, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import paths

STATUS_RECEIVED = "受付"
STATUS_ARRIVED = "書類到着"
STATUS_DONE = "処理済"


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"
    case_no: Mapped[str] = mapped_column(String(40), primary_key=True)
    company: Mapped[str] = mapped_column(String(200), default="")
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    docs_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    done_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    @property
    def status(self) -> str:
        if self.done_date:
            return STATUS_DONE
        if self.docs_date:
            return STATUS_ARRIVED
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


def init(url: str | None = None) -> None:
    global _engine, _factory
    _engine = create_engine(url or f"sqlite:///{paths.db_path()}", future=True)
    Base.metadata.create_all(_engine)
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


def daily_backup(keep: int = 30) -> None:
    """1日1回、データベースのコピーを保存する。"""
    src = paths.db_path()
    if not src.exists():
        return
    dst = paths.backup_dir() / f"tokutai_{date.today():%Y%m%d}.db"
    if not dst.exists():
        shutil.copyfile(src, dst)
    files = sorted(paths.backup_dir().glob("tokutai_*.db"))
    for old in files[:-keep]:
        old.unlink(missing_ok=True)
