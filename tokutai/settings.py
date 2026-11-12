"""設定の読み書き（settings.json）。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

from . import paths


@dataclass
class Settings:
    tenant_id: str = ""
    client_id: str = ""
    folder_name: str = ""
    kw_create: str = "特退金脱退案件作成"
    kw_done: str = "特退金制度申し込み"
    warn_docs_days: int = 7
    warn_process_days: int = 3
    warn_defect_days: int = 7
    manager_emails: list[str] = field(default_factory=lambda: ["", ""])
    notify_enabled: bool = True
    sync_minutes: int = 30
    initial_days: int = 60
    autostart: bool = True
    autostart_initialized: bool = False

    def managers(self) -> list[str]:
        return [m.strip() for m in self.manager_emails if m and m.strip()]

    def is_connectable(self) -> bool:
        return bool(self.tenant_id.strip() and self.client_id.strip() and self.folder_name.strip())


def load() -> Settings:
    p = paths.settings_path()
    s = Settings()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        names = {f.name for f in fields(Settings)}
        for k, v in raw.items():
            if k in names:
                setattr(s, k, v)
    mails = list(s.manager_emails)[:2]
    while len(mails) < 2:
        mails.append("")
    s.manager_emails = mails
    return s


def save(s: Settings) -> None:
    paths.settings_path().write_text(
        json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8"
    )
