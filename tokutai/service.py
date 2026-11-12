"""取り込み・警告判定・管理職通知のロジック。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import db
from .bizdays import business_days_between
from .corporate import CompanyNormalizer
from .db import Case, NotifyLog, ProcessedMail
from .mailparse import KIND_CREATE, KIND_DONE, classify, html_to_text, parse_body
from .settings import Settings

log = logging.getLogger(__name__)

ALERT_DOCS = "書類未着"
ALERT_PROCESS = "未処理"
ALERT_DEFECT = "補正未着"
NOTE_UNSEEN = "受付メール未確認"
RETENTION_DAYS = 365  # 処理済の案件を残す期間


# ---------- 警告判定 ----------
@dataclass(frozen=True)
class Alert:
    case_no: str
    company: str
    kind: str
    base_date: date
    days: int


def alert_kind(c: Case, today: date, s: Settings) -> str | None:
    if c.done_date:
        return None
    if c.docs_date:
        if business_days_between(c.docs_date, today) >= s.warn_process_days:
            return ALERT_PROCESS
        return None
    if c.defect_date:
        if business_days_between(c.defect_date, today) >= s.warn_defect_days:
            return ALERT_DEFECT
        return None
    if c.received_date and business_days_between(c.received_date, today) >= s.warn_docs_days:
        return ALERT_DOCS
    return None


def elapsed_days(c: Case, today: date) -> int | None:
    """状態ごとの経過営業日。受付→受付日から、書類到着→到着日から。処理済は空。"""
    if c.done_date:
        return None
    base = c.docs_date or c.defect_date or c.received_date
    return business_days_between(base, today) if base else None


def collect_alerts(cases: list[Case], today: date, s: Settings) -> list[Alert]:
    out: list[Alert] = []
    for c in cases:
        k = alert_kind(c, today, s)
        if k:
            base = {ALERT_PROCESS: c.docs_date, ALERT_DEFECT: c.defect_date}.get(k, c.received_date)
            out.append(Alert(c.case_no, c.company, k, base, business_days_between(base, today)))  # type: ignore[arg-type]
    return out


# ---------- 一覧の絞り込みと並び替え ----------
FILTER_OPEN = "未完了のみ"
FILTER_ALL = "すべて"
FILTER_ALERT = "警告のみ"
FILTER_DEFECT = "不備のみ"
FILTERS = [FILTER_OPEN, FILTER_ALL, FILTER_ALERT, FILTER_DEFECT]

_STATUS_ORDER = {db.STATUS_RECEIVED: 0, db.STATUS_DEFECT: 1, db.STATUS_ARRIVED: 2, db.STATUS_DONE: 3}


def _sort_value(col: str, c: Case, alert: Alert | None, today: date):
    """列ごとの並び替え値。空（None）は、昇順でも降順でも最後に回す。"""
    if col == "状態":
        return _STATUS_ORDER[c.status]
    if col == "警告":
        return alert.kind if alert else None
    if col == "案件番号":
        return c.case_no
    if col == "会社名":
        return c.company or None
    if col == "受付日":
        return c.received_date
    if col == "不備連絡日":
        return c.defect_date
    if col == "書類到着日":
        return c.docs_date
    if col == "処理済日":
        return c.done_date
    if col == "経過日数":
        return elapsed_days(c, today)
    if col == "備考":
        return c.note or None
    return None


def select_rows(cases: list[Case], alerts: dict[str, Alert], mode: str, query: str,
                sort_col: str, ascending: bool, today: date) -> list[Case]:
    """絞り込み・検索・並び替えをした案件の一覧を返す。

    検索語があるときは「未完了のみ」を無視して、処理済も探す（探し漏れ防止）。
    検索は案件番号・会社名・備考が対象。
    """
    q = (query or "").strip().lower()
    rows = []
    for c in cases:
        if mode == FILTER_OPEN and not q and c.done_date:
            continue
        if mode == FILTER_ALERT and c.case_no not in alerts:
            continue
        if mode == FILTER_DEFECT and c.status != db.STATUS_DEFECT:
            continue
        if q and not any(q in (v or "").lower() for v in (c.case_no, c.company, c.note)):
            continue
        rows.append(c)
    if sort_col:
        filled = [(c, _sort_value(sort_col, c, alerts.get(c.case_no), today)) for c in rows]
        have = sorted((x for x in filled if x[1] is not None), key=lambda x: (x[1], x[0].case_no),
                      reverse=not ascending)
        rows = [c for c, _ in have] + [c for c, v in filled if v is None]
    else:  # 標準: 警告 → 未完了 → 処理済、受付日の古い順
        rows.sort(key=lambda c: (c.case_no not in alerts, c.done_date is not None, c.received_date or date.min))
    return rows


# ---------- CSV出力 ----------
CSV_HEADER = ["状態", "案件番号", "会社名", "受付日", "不備連絡日", "書類到着日", "処理済日", "備考"]


def export_csv(cases: list[Case], path) -> int:
    """案件をCSVに書き出す（Excelで開けるようBOM付きUTF-8）。書き出した件数を返す。"""
    import csv

    def f(d: date | None) -> str:
        return d.strftime("%Y-%m-%d") if d else ""

    rows = sorted(cases, key=lambda c: (c.received_date or date.min, c.case_no))
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(CSV_HEADER)
        for c in rows:
            w.writerow([c.status, c.case_no, c.company, f(c.received_date), f(c.defect_date),
                        f(c.docs_date), f(c.done_date), c.note])
    return len(rows)


# ---------- 取り込み ----------
class MailSource(Protocol):
    def fetch_messages(self, folder_id: str, since: datetime, interactive: bool = ...) -> list: ...
    def find_folder_id(self, name: str, interactive: bool = ...) -> str: ...


@dataclass
class SyncResult:
    new_cases: int = 0
    done_cases: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)


def _append_note(c: Case, note: str) -> None:
    if note and note not in (c.note or ""):
        c.note = f"{c.note}　{note}".strip() if c.note else note


def record_defect(c: Case, defect_date: date) -> None:
    """不備を連絡した日を記録する。元の書類到着日は備考に残す。"""
    if c.docs_date:
        _append_note(c, f"元の書類到着日 {c.docs_date:%Y-%m-%d}")
    c.defect_date = defect_date
    c.docs_date = None  # 補正書類が届くまで、書類は揃っていない扱い


def apply_mail(s: Session, norm: CompanyNormalizer, kind: str, case_no: str, company_raw: str,
               recv: date, res: SyncResult) -> None:
    c = s.get(Case, case_no)
    if kind == KIND_CREATE:
        cr = norm.normalize(company_raw) if company_raw else None
        if c is None:
            c = Case(case_no=case_no, company=cr.name if cr else "", received_date=recv, note="")
            for n in (cr.notes if cr else []):
                _append_note(c, n)
            s.add(c)
            res.new_cases += 1
        else:
            # 処理完了メールが先に来て仮登録されていた場合の補完
            if c.received_date is None:
                c.received_date = recv
            if not c.company and cr:
                c.company = cr.name
                for n in cr.notes:
                    _append_note(c, n)
            if c.note and NOTE_UNSEEN in c.note:
                c.note = c.note.replace(NOTE_UNSEEN, "").strip("　 ")
    elif kind == KIND_DONE:
        if c is None:
            c = Case(case_no=case_no, company="", received_date=None, done_date=recv, note=NOTE_UNSEEN)
            s.add(c)
            res.done_cases += 1
        elif c.done_date is None:
            c.done_date = recv
            res.done_cases += 1
    s.flush()


def import_mails(s: Session, st: Settings, source: MailSource, norm: CompanyNormalizer,
                 res: SyncResult, interactive: bool = False) -> None:
    folder_id = source.find_folder_id(st.folder_name, interactive)
    last = db.get_meta(s, "last_sync")
    if last:
        since = datetime.fromisoformat(last).astimezone() - timedelta(days=1)
    else:
        since = datetime.now().astimezone() - timedelta(days=max(1, st.initial_days))
    started = datetime.now().astimezone()
    messages = source.fetch_messages(folder_id, since, interactive)
    messages.sort(key=lambda m: m.received)
    for m in messages:
        if s.get(ProcessedMail, m.mail_id):
            continue
        kind = classify(m.subject, st.kw_create, st.kw_done)
        if kind is None:
            continue
        body = html_to_text(m.body) if m.body_is_html else m.body
        parsed = parse_body(kind, body)
        if parsed is None:
            res.errors.append(f"案件番号を読み取れないメール: {m.subject[:40]}")
            log.warning("案件番号なし: %s", m.subject)
            s.add(ProcessedMail(mail_id=m.mail_id, kind=kind, case_no=""))
            continue
        apply_mail(s, norm, kind, parsed.case_no, parsed.company_raw, m.received_date, res)
        s.add(ProcessedMail(mail_id=m.mail_id, kind=kind, case_no=parsed.case_no))
        log.info("取り込み: %s %s", kind, parsed.case_no)
    db.set_meta(s, "last_sync", started.isoformat())
    s.commit()


# ---------- 保存期間 ----------
def purge_old_done(s: Session, today: date) -> int:
    """処理済日から1年（365日）をすぎた案件を削除する。未完了の案件は消さない。"""
    cutoff = today - timedelta(days=RETENTION_DAYS)
    nos = list(s.scalars(select(Case.case_no).where(Case.done_date.is_not(None), Case.done_date < cutoff)))
    if nos:
        s.execute(delete(NotifyLog).where(NotifyLog.case_no.in_(nos)))
        s.execute(delete(Case).where(Case.case_no.in_(nos)))
        s.commit()
        log.info("保存期間（%d日）をすぎた処理済の案件を削除: %d件", RETENTION_DAYS, len(nos))
    return len(nos)


# ---------- 通知 ----------
def build_notice(alerts: list[Alert], st: Settings) -> tuple[str, str]:
    lines = [f"特退金脱退案件で、期限を過ぎた案件が{len(alerts)}件あります。", ""]
    for kind, title, days in (
        (ALERT_DOCS, "【書類未着】受付から", st.warn_docs_days),
        (ALERT_DEFECT, "【補正書類未着】不備連絡から", st.warn_defect_days),
        (ALERT_PROCESS, "【WEB処理未完了】書類到着から", st.warn_process_days),
    ):
        group = [a for a in alerts if a.kind == kind]
        if not group:
            continue
        lines.append(f"{title}{days}営業日以上")
        for a in group:
            lines.append(f"・{a.case_no}　{a.company or '（会社名なし）'}　基準日 {a.base_date:%Y-%m-%d}　経過 {a.days}営業日")
        lines.append("")
    lines.append("このメールは特退金脱退 処理漏れ防止アプリから自動送信されています。")
    return f"【特退金脱退】処理漏れ警告 {len(alerts)}件", "\n".join(lines)


def notify_managers(s: Session, st: Settings, sender, today: date, res: SyncResult) -> None:
    if not st.notify_enabled:
        return
    to = st.managers()
    if not to:
        return
    cases = list(s.scalars(select(Case)))
    alerts = collect_alerts(cases, today, st)
    pending = []
    for a in alerts:
        done = s.scalar(select(NotifyLog).where(
            NotifyLog.case_no == a.case_no, NotifyLog.kind == a.kind, NotifyLog.notified_on == today))
        if done is None:
            pending.append(a)
    if not pending:
        return
    subject, body = build_notice(pending, st)
    sender.send_mail(to, subject, body, False)
    for a in pending:
        s.add(NotifyLog(case_no=a.case_no, kind=a.kind, notified_on=today))
    s.commit()
    res.notified += len(pending)
    log.info("管理職へ通知: %d件 -> %s", len(pending), ",".join(to))


def run_sync(st: Settings, client, norm: CompanyNormalizer, interactive: bool = False,
             today: date | None = None) -> SyncResult:
    """メール取り込み → 警告判定 → 管理職への通知。失敗は errors に集めて続行する。"""
    res = SyncResult()
    today = today or date.today()
    with db.session() as s:
        try:
            import_mails(s, st, client, norm, res, interactive)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            res.errors.append(f"メール取り込みに失敗: {e}")
            log.exception("メール取り込み失敗")
        try:
            purge_old_done(s, today)
        except Exception:  # noqa: BLE001
            s.rollback()
            log.exception("古い処理済案件の削除に失敗")
        try:
            notify_managers(s, st, client, today, res)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            res.errors.append(f"管理職への通知に失敗: {e}")
            log.exception("通知失敗")
        db.set_meta(s, "last_run", datetime.now().isoformat(timespec="seconds"))
        s.commit()
    return res
