import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokutai import db, paths, service  # noqa: E402
from tokutai.bizdays import business_days_between  # noqa: E402
from tokutai.corporate import CompanyNormalizer, NOTE_AMBIGUOUS, NOTE_UNKNOWN, load_table  # noqa: E402
from tokutai.graph import MailItem  # noqa: E402
from tokutai.mailparse import KIND_CREATE, KIND_DONE, classify, parse_body  # noqa: E402
from tokutai.settings import Settings  # noqa: E402


@pytest.fixture(scope="module")
def norm():
    return CompanyNormalizer(load_table(paths.bundled_abbr_path()))


@pytest.mark.parametrize("raw,expected", [
    ("ｽｽﾞｶﾝ (ｶ", "スズカン株式会社"),
    ("ｶ)ｽｽﾞｶﾝ", "株式会社スズカン"),
    ("ﾏﾙﾏﾙ(ｶ)ｴｲｷﾞﾖｳ", "マルマル株式会社エイギヨウ"),
    ("ｱｲｳ(ﾕ", "アイウ有限会社"),
    ("ｼﾞｮｳﾎﾞｳ(ｼﾎｳ", "ジョウボウ司法書士法人"),
    ("ﾃｽﾄ(ｼ", "テスト合資会社"),
])
def test_company(norm, raw, expected):
    assert norm.normalize(raw).name == expected


def test_company_notes(norm):
    assert NOTE_AMBIGUOUS in norm.normalize("ｱｲｳ(ｲ").notes
    assert NOTE_UNKNOWN in norm.normalize("ｱｲｳ(ﾎﾎﾎ").notes
    assert norm.normalize("").name == ""


def test_classify_and_parse():
    assert classify("特退金 脱退案件作成のお知らせ", "特退金脱退案件作成", "特退金制度申し込み") == KIND_CREATE
    assert classify("アクサ生命：特退金制度申し込み", "特退金脱退案件作成", "特退金制度申し込み") == KIND_DONE
    assert classify("関係ないメール", "特退金脱退案件作成", "特退金制度申し込み") is None
    p = parse_body(KIND_CREATE, "案件番号：T261110500002789\n上記書類を ｽｽﾞｶﾝ (ｶ 様へ送付")
    assert p.case_no == "T261110500002789" and p.company_raw == "ｽｽﾞｶﾝ (ｶ"
    assert parse_body(KIND_DONE, "本文に番号なし") is None


def test_bizdays():
    # 2026-11-13(金) -> 11-16(月): 1営業日。11-23(月,勤労感謝の日)は除外
    assert business_days_between(date(2026, 11, 13), date(2026, 11, 16)) == 1
    assert business_days_between(date(2026, 11, 20), date(2026, 11, 24)) == 1
    assert business_days_between(date(2026, 11, 20), date(2026, 11, 20)) == 0


class FakeClient:
    def __init__(self, msgs):
        self.msgs, self.sent = msgs, []

    def find_folder_id(self, name, interactive=False):
        return "f1"

    def fetch_messages(self, folder_id, since, interactive=False):
        return list(self.msgs)

    def send_mail(self, to, subject, body, interactive=False):
        self.sent.append((to, subject, body))


def mail(i, subj, body, d):
    return MailItem(i, subj, datetime(d.year, d.month, d.day, 10).astimezone(), body, False)


def test_sync_flow(tmp_path, monkeypatch, norm):
    monkeypatch.setenv("TOKUTAI_HOME", str(tmp_path))
    db.init(f"sqlite:///{tmp_path / 't.db'}")
    st = Settings(folder_name="x", manager_emails=["a@example.com", "b@example.com"])
    msgs = [
        mail("m1", "特退金脱退案件作成", "案件番号：T1001\n上記書類を ｽｽﾞｶﾝ (ｶ 様へ", date(2026, 11, 2)),
        mail("m2", "特退金脱退案件作成", "案件番号：T1002\n上記書類を ﾃｽﾄ(ﾕ 様へ", date(2026, 11, 4)),
        mail("m3", "アクサ生命：特退金制度申し込み", "案件番号：T1002", date(2026, 11, 6)),
        mail("m4", "アクサ生命：特退金制度申し込み", "案件番号：T9999", date(2026, 11, 6)),
    ]
    client = FakeClient(msgs)
    r = service.run_sync(st, client, norm, today=date(2026, 11, 12))
    assert (r.new_cases, r.done_cases, r.errors) == (2, 2, [])
    with db.session() as s:
        c1, c2, c9 = s.get(db.Case, "T1001"), s.get(db.Case, "T1002"), s.get(db.Case, "T9999")
        assert c1.company == "スズカン株式会社" and c1.status == "受付"
        assert c2.status == "処理済" and c9.note == service.NOTE_UNSEEN
        assert service.alert_kind(c1, date(2026, 11, 12), st) == service.ALERT_DOCS  # 7営業日
        assert service.alert_kind(c1, date(2026, 11, 10), st) is None
    assert len(client.sent) == 1 and client.sent[0][0] == ["a@example.com", "b@example.com"]
    # 同日の再実行では二重送信しない・メールも二重取り込みしない
    r2 = service.run_sync(st, client, norm, today=date(2026, 11, 12))
    assert r2.new_cases == 0 and r2.notified == 0 and len(client.sent) == 1
    # 翌日は再通知
    service.run_sync(st, client, norm, today=date(2026, 11, 13))
    assert len(client.sent) == 2


def test_process_alert():
    st = Settings()
    c = db.Case(case_no="T1", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 9))
    assert service.alert_kind(c, date(2026, 11, 11), st) is None
    assert service.alert_kind(c, date(2026, 11, 12), st) == service.ALERT_PROCESS


def test_defect_alert():
    st = Settings()
    c = db.Case(case_no="T2", received_date=date(2026, 11, 2), defect_date=date(2026, 11, 2))
    assert c.status == db.STATUS_DEFECT
    assert service.alert_kind(c, date(2026, 11, 11), st) is None  # 6営業日（11/3は祝日）
    assert service.alert_kind(c, date(2026, 11, 12), st) == service.ALERT_DEFECT  # 7営業日
    st.warn_defect_days = 3
    assert service.alert_kind(c, date(2026, 11, 6), st) == service.ALERT_DEFECT
    # 補正書類が届いたら、WEB処理の警告に切り替わる
    c.defect_date, c.docs_date = None, date(2026, 11, 9)
    assert c.status == db.STATUS_ARRIVED
    assert service.alert_kind(c, date(2026, 11, 12), st) == service.ALERT_PROCESS


def test_migrate_old_db(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE cases (case_no VARCHAR(40) PRIMARY KEY, company VARCHAR(200), "
                "received_date DATE, docs_date DATE, done_date DATE, note TEXT)")
    con.execute("INSERT INTO cases (case_no, company, note) VALUES ('X1','a','')")
    con.commit(); con.close()
    db.init(f"sqlite:///{p}")
    with db.session() as s:
        assert s.get(db.Case, "X1").defect_date is None


def test_record_defect_keeps_original_date():
    c = db.Case(case_no="T3", received_date=date(2026, 11, 2), docs_date=date(2026, 11, 5), note="メモ")
    service.record_defect(c, date(2026, 11, 9))
    assert c.docs_date is None and c.defect_date == date(2026, 11, 9)
    assert "元の書類到着日 2026-11-05" in c.note and "メモ" in c.note
