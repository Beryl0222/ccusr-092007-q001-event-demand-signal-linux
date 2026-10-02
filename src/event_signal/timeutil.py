"""时间表达与窗口运算。

全系统统一使用带时区偏移的 ISO 8601(与 fixtures/demand_signal.json 中
occurred_at 的写法一致,如 2026-09-20T09:00:00+08:00)。窗口为半开区间
[start, end);跨午夜场次(如 20:00 至次日 02:00)按绝对时刻参与比较,
其营业日归属取窗口开始时刻的本地日期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .errors import ValidationError


def parse_instant(value: str | datetime, *, field: str = "time") -> datetime:
    """解析带时区的 ISO 8601 时刻;拒绝无时区的朴素时间。"""
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是合法 ISO 8601: {value!r}") from exc
    else:
        raise ValidationError(f"{field} 类型不支持: {type(value).__name__}")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValidationError(
            f"{field} 必须带时区偏移(例如 2026-09-20T09:00:00+08:00)"
        )
    return instant


def format_instant(instant: datetime) -> str:
    """统一输出 ISO 8601 表达。"""
    return parse_instant(instant).isoformat()


@dataclass(frozen=True)
class Window:
    """半开时间窗 [start, end),支持跨午夜。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = parse_instant(self.start, field="window.start")
        end = parse_instant(self.end, field="window.end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if end <= start:
            raise ValidationError("时间窗结束必须晚于开始")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def business_date(self) -> date:
        """营业日:窗口开始时刻的本地日期(跨午夜场次计入开场日)。"""
        return self.start.date()

    @property
    def crosses_midnight(self) -> bool:
        """窗口是否跨越本地午夜(结束恰为 00:00 不算跨午夜)。"""
        last = (self.end - timedelta(microseconds=1)).astimezone(self.start.tzinfo)
        return last.date() != self.start.date()

    def overlaps(self, other: "Window") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Window") -> "Window | None":
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        if lo >= hi:
            return None
        return Window(lo, hi)

    def fraction_covered_by(self, other: "Window") -> float:
        """本窗口被 other 覆盖的时长比例(用于跨窗口按比例分摊)。"""
        inter = self.intersection(other)
        if inter is None:
            return 0.0
        return inter.duration / self.duration
