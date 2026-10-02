"""ISO-8601 时间与时间窗工具。

样例合同里的 ``occurred_at`` 是带偏移的 ISO-8601 字符串（如
``2026-09-20T09:00:00+08:00``）。服务内部一律存为带时区的 ``datetime``，
输出时恢复为 ISO 字符串，保证既有时间含义不变。

跨午夜场次（如 22:00 开赛、次日 01:30 散场）是常见情形：绝对时间窗直接
允许跨任意多小时；日程模板里的 ``HH:MM`` 区间用 :func:`resolve_clock_window`
把不晚于开始时间的结束时钟顺延一天。
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: 信号切桶时使用的标准桶长。
BUCKET = timedelta(hours=1)


def parse_iso(value: str | datetime) -> datetime:
    """解析带偏移/``Z`` 的 ISO-8601 字符串。

    赛事数据跨天又跨时区，静默按本地解释会让跨午夜场次整体错位，
    因此无时区信息的时间戳一律视为合同错误。
    """

    if isinstance(value, datetime):
        dt = value
    else:
        if not isinstance(value, str) or not value:
            raise ValueError("时间必须是 ISO-8601 字符串")
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"无法解析 ISO-8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"时间必须显式携带时区偏移: {value!r}")
    return dt


def to_iso(dt: datetime) -> str:
    """带时区 datetime 还原为 ISO-8601。"""

    if dt.tzinfo is None:
        raise ValueError("拒绝输出无时区的时间")
    return dt.isoformat()


def resolve_clock_window(day: datetime, start_clock: str, end_clock: str) -> tuple[datetime, datetime]:
    """把赛日 ``day`` 上的 ``HH:MM`` 区间解析为绝对时间，支持跨午夜。

    ``end_clock`` 不晚于 ``start_clock`` 时，结束时间顺延到下一自然日。
    例如 2026-09-20 上的 ("22:00", "01:30") 解析为当日 22:00 至次日 01:30；
    ``day`` 的时区决定“自然日”的归属，跨时区不会错日。
    """

    sh, sm = _parse_clock(start_clock)
    eh, em = _parse_clock(end_clock)
    if day.tzinfo is None:
        raise ValueError("赛日必须带时区")
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight + timedelta(hours=sh, minutes=sm)
    end = midnight + timedelta(hours=eh, minutes=em)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _parse_clock(value: str) -> tuple[int, int]:
    try:
        h, m = value.split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"时钟时间应为 HH:MM: {value!r}") from exc
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"时钟时间越界: {value!r}")
    return h, m


def absolute_window(start: str | datetime, end: str | datetime) -> tuple[datetime, datetime]:
    """返回一对带时区绝对时间，并校验 ``end > start``。"""

    s, e = parse_iso(start), parse_iso(end)
    if e <= s:
        raise ValueError(f"时间窗结束必须晚于开始: {to_iso(s)} ~ {to_iso(e)}")
    return s, e


def spans_midnight(start: datetime, end: datetime) -> bool:
    """绝对时间窗是否跨越城市本地午夜（按各端自身时区比较日历日）。"""

    return end > start and start.date() != end.astimezone(start.tzinfo).date()


def hourly_buckets(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """把时间窗切成与整点对齐的一小时桶，首尾两桶可能不足一小时。

    用于把“总人次/总房晚”按窗长均摊到小时，再与其他信号在统一桶上聚合。
    """

    if end <= start:
        return []
    buckets: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        boundary = cursor.replace(minute=0, second=0, microsecond=0) + BUCKET
        nxt = min(end, boundary)
        buckets.append((cursor, nxt))
        cursor = nxt
    return buckets


def overlap_seconds(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> float:
    """两个绝对时间窗的重叠秒数（无重叠为零）。"""

    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return (hi - lo).total_seconds() if hi > lo else 0.0


def window_seconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()
