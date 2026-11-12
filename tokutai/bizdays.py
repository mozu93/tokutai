"""営業日（土日・祝日を除く）の計算。"""
from __future__ import annotations

from datetime import date, timedelta

import jpholiday


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and not jpholiday.is_holiday(d)


def business_days_between(start: date, end: date) -> int:
    """start の翌日から end までに含まれる営業日の数。start より前の end は 0。"""
    if end <= start:
        return 0
    n = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_business_day(d):
            n += 1
        d += timedelta(days=1)
    return n
