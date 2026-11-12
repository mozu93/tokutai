"""Microsoft Graph API 連携（読み取り Mail.Read、送信 Mail.Send）。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import msal
import requests
from msal_extensions import FilePersistence, PersistedTokenCache, build_encrypted_persistence

from . import paths

log = logging.getLogger(__name__)

SCOPES = ["Mail.Read", "Mail.Send"]
BASE = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    pass


@dataclass
class MailItem:
    mail_id: str
    subject: str
    received: datetime  # ローカルタイムゾーン
    body: str
    body_is_html: bool

    @property
    def received_date(self) -> date:
        return self.received.date()


def _cache() -> PersistedTokenCache:
    path = str(paths.token_cache_path())
    try:
        persistence = build_encrypted_persistence(path)  # Windows では DPAPI で暗号化
    except Exception:  # noqa: BLE001
        log.warning("暗号化ストレージが使えないため、平文キャッシュにフォールバックします")
        persistence = FilePersistence(path)
    return PersistedTokenCache(persistence)


class GraphClient:
    def __init__(self, tenant_id: str, client_id: str):
        self._app = msal.PublicClientApplication(
            client_id.strip(),
            authority=f"https://login.microsoftonline.com/{tenant_id.strip()}",
            token_cache=_cache(),
        )
        self._folder_cache: dict[str, str] = {}

    # ---- 認証 ----
    def token(self, interactive: bool = True) -> str:
        accounts = self._app.get_accounts()
        if accounts:
            r = self._app.acquire_token_silent(SCOPES, account=accounts[0])
            if r and "access_token" in r:
                return r["access_token"]
        if not interactive:
            raise GraphError("サインインが必要です。設定画面の「接続テスト」でサインインしてください。")
        r = self._app.acquire_token_interactive(SCOPES)
        if "access_token" not in r:
            raise GraphError(f"サインインに失敗しました: {r.get('error_description', r.get('error'))}")
        return r["access_token"]

    def _headers(self, interactive: bool, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self.token(interactive)}"}
        if extra:
            h.update(extra)
        return h

    def _get(self, url: str, interactive: bool, params: dict | None = None, extra: dict | None = None) -> dict:
        try:
            r = requests.get(url, headers=self._headers(interactive, extra), params=params, timeout=60)
        except requests.RequestException as e:
            raise GraphError(f"通信に失敗しました: {e}") from e
        if r.status_code >= 400:
            raise GraphError(f"Graph APIエラー {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---- フォルダ検索（サブフォルダも名前から探す）----
    def find_folder_id(self, name: str, interactive: bool = True) -> str:
        key = name.strip()
        if key in self._folder_cache:
            return self._folder_cache[key]
        queue = [f"{BASE}/me/mailFolders?$top=100"]
        seen = 0
        while queue:
            url = queue.pop(0)
            while url:
                data = self._get(url, interactive)
                for f in data.get("value", []):
                    seen += 1
                    if f.get("displayName", "").strip() == key:
                        self._folder_cache[key] = f["id"]
                        return f["id"]
                    if f.get("childFolderCount", 0) > 0:
                        queue.append(f"{BASE}/me/mailFolders/{f['id']}/childFolders?$top=100")
                url = data.get("@odata.nextLink")
        raise GraphError(f"Outlookのフォルダ「{key}」が見つかりません（{seen}件のフォルダを確認）。")

    # ---- メール取得 ----
    def fetch_messages(self, folder_id: str, since: datetime, interactive: bool = True) -> list[MailItem]:
        since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{BASE}/me/mailFolders/{folder_id}/messages"
        params = {
            "$filter": f"receivedDateTime ge {since_utc}",
            "$orderby": "receivedDateTime asc",
            "$select": "id,subject,receivedDateTime,body",
            "$top": "50",
        }
        extra = {"Prefer": 'outlook.body-content-type="text"'}
        items: list[MailItem] = []
        while url:
            data = self._get(url, interactive, params=params, extra=extra)
            params = None  # nextLink にはクエリが含まれる
            for m in data.get("value", []):
                recv = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")).astimezone()
                body = m.get("body") or {}
                items.append(
                    MailItem(
                        mail_id=m["id"],
                        subject=m.get("subject") or "",
                        received=recv,
                        body=body.get("content") or "",
                        body_is_html=(body.get("contentType") == "html"),
                    )
                )
            url = data.get("@odata.nextLink")
        return items

    # ---- 送信 ----
    def send_mail(self, to: list[str], subject: str, body: str, interactive: bool = False) -> None:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            },
            "saveToSentItems": True,
        }
        try:
            r = requests.post(
                f"{BASE}/me/sendMail",
                headers=self._headers(interactive, {"Content-Type": "application/json"}),
                json=payload,
                timeout=60,
            )
        except requests.RequestException as e:
            raise GraphError(f"通信に失敗しました: {e}") from e
        if r.status_code >= 400:
            raise GraphError(f"メール送信エラー {r.status_code}: {r.text[:300]}")
