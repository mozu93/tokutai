"""メールの件名判定と本文解析。"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

KIND_CREATE = "create"
KIND_DONE = "done"

_CASE_RE = re.compile(r"案件番号\s*[:：]\s*([A-Za-z]\d+)")
_COMPANY_RE = re.compile(r"上記書類を\s*(.+?)\s*様へ", re.S)


def _squash(s: str) -> str:
    """空白（全角含む）を除いた比較用文字列。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))


def classify(subject: str, kw_create: str, kw_done: str) -> str | None:
    subj = _squash(subject)
    if kw_create and _squash(kw_create) in subj:
        return KIND_CREATE
    if kw_done and _squash(kw_done) in subj:
        return KIND_DONE
    return None


def html_to_text(content: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", content)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


@dataclass
class ParsedMail:
    case_no: str
    company_raw: str = ""


def parse_body(kind: str, body: str) -> ParsedMail | None:
    text = unicodedata.normalize("NFKC", body or "")
    m = _CASE_RE.search(text)
    if not m:
        return None
    case_no = m.group(1).upper()
    company = ""
    if kind == KIND_CREATE:
        # 半角カナは会社名側で扱うため、元の本文から取る
        cm = _COMPANY_RE.search(body or "")
        if cm:
            company = re.sub(r"\s+", " ", cm.group(1)).strip()
    return ParsedMail(case_no, company)
