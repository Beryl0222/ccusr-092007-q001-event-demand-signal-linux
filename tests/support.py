"""测试公共支撑:假时钟、标准参与方与地域、跨午夜场次窗口。"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from event_signal import DemandSignalService  # noqa: E402

CN = timezone(timedelta(hours=8))

# 跨午夜场次:2026-09-20 20:00 至次日 02:00(+08:00),共 6 小时
NIGHT_START = "2026-09-20T20:00:00+08:00"
NIGHT_END = "2026-09-21T02:00:00+08:00"

T0 = "2026-09-15T09:00:00+08:00"


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class FakeClock:
    def __init__(self, start: str = T0):
        self.t = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.t

    def set(self, value) -> None:
        self.t = datetime.fromisoformat(value) if isinstance(value, str) else value

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def build_service():
    """标准协作网络:运营中心、主办方、票务方、四类经营者。"""
    clock = FakeClock()
    svc = DemandSignalService(clock=clock)
    svc.register_geo("CITY", "city")
    svc.register_geo("D1", "district", parent_id="CITY")
    svc.register_geo("D2", "district", parent_id="CITY")
    svc.register_geo("B1", "block", parent_id="D1")
    svc.register_geo("V1", "venue", parent_id="B1")
    svc.register_participant("ops", "ops_center", "运营中心")
    svc.register_participant("org", "organizer", "主办方")
    svc.register_participant("tix", "ticketing", "票务方")
    svc.register_participant("hotel1", "operator", "酒店一")
    svc.register_participant("hotel2", "operator", "酒店二")
    svc.register_participant("trans1", "operator", "交通一")
    svc.register_participant("dine1", "operator", "餐饮一")
    return svc, clock


def publish_night_signal(svc, *, publisher_id="org", source="org",
                         record_id="concert-0920", revision=1,
                         occurred_at="2026-09-15T10:00:00+08:00",
                         metric="visitor_arrivals", geo_id="D1",
                         low=8000, expected=12000, high=15000,
                         visibility="network", allowlist=(),
                         purpose="capacity_planning", note=""):
    return svc.submit_signal(
        publisher_id=publisher_id, source=source, record_id=record_id,
        revision=revision, occurred_at=occurred_at, purpose=purpose,
        metric=metric, window_start=NIGHT_START, window_end=NIGHT_END,
        geo_id=geo_id, low=low, expected=expected, high=high,
        visibility=visibility, allowlist=allowlist, note=note,
    )
