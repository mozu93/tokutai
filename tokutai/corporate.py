"""会社名の整形：半角カナの全角化と、法人略称（全銀の法人略語）の読み替え。"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

NOTE_AMBIGUOUS = "法人格は略称から推定（要確認）"
NOTE_UNKNOWN = "法人略称が未登録（要確認）"

_SMALL = str.maketrans("ャュョッ", "ヤユヨツ")
# 漢字の略称（㈱ などは NFKC で (株) になる）
_KANJI = {"株": "株式会社", "有": "有限会社", "合": "合同会社"}


@dataclass(frozen=True)
class Abbr:
    abbr: str
    formal: str
    ambiguous: bool


@dataclass
class CompanyResult:
    name: str
    notes: list[str]


def load_table(path: Path) -> list[Abbr]:
    rows: list[Abbr] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            a = (r.get("abbr") or "").strip()
            if not a:
                continue
            a = a.translate(_SMALL)
            rows.append(Abbr(a, (r.get("formal") or "").strip(), (r.get("ambiguous") or "0").strip() == "1"))
    return rows


class CompanyNormalizer:
    def __init__(self, table: list[Abbr]):
        self._map = {a.abbr: a for a in table}
        alts = sorted(self._map, key=len, reverse=True)  # 長い略称を先に照合
        alt = "|".join(re.escape(a) for a in alts)
        # 括弧付き: (カ) / カ) / (カ（閉じ括弧なしは末尾のみ）
        self._closed = re.compile(rf"\(\s*({alt})\s*\)|^({alt})\s*\)")
        self._open_end = re.compile(rf"\(\s*({alt})\s*$")
        self._unknown = re.compile(r"\(\s*[ァ-ヶー]{1,8}\s*(?:\)|$)|^[ァ-ヶー]{1,8}\s*\)")

    def normalize(self, raw: str) -> CompanyResult:
        s = unicodedata.normalize("NFKC", raw or "")
        s = re.sub(r"\s*\(", "(", s)
        s = re.sub(r"\)\s*", ")", s).strip()
        for k, v in _KANJI.items():
            s = s.replace(f"({k})", v)
        notes: list[str] = []

        # 照合は小さい字を大きい字に直した影文字列 t で行い、出力は元の s を使う（1字対1字なので位置は同じ）
        def apply(rx: re.Pattern, s: str) -> str:
            t = s.translate(_SMALL)
            out, pos = [], 0
            for m in rx.finditer(t):
                a = self._map[m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)]
                if a.ambiguous and NOTE_AMBIGUOUS not in notes:
                    notes.append(NOTE_AMBIGUOUS)
                out.append(s[pos:m.start()])
                out.append(a.formal)
                pos = m.end()
            out.append(s[pos:])
            return "".join(out)

        s = apply(self._closed, s)
        s = apply(self._open_end, s)
        if self._unknown.search(s.translate(_SMALL)):
            notes.append(NOTE_UNKNOWN)
        return CompanyResult(s.strip(), notes)
