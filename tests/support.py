"""测试共用装配：一套最小但结构完整的城市/参与方/地域数据。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from event_signal.audit import AuditLog
from event_signal.geography import (
    BUSINESS_CIRCLE,
    CITY,
    DISTRICT,
    VENUE,
    Region,
    RegionTree,
)
from event_signal.metrics import MetricRegistry
from event_signal.parties import (
    Party,
    PartyDirectory,
    ROLE_CATERING,
    ROLE_HOTEL,
    ROLE_OPERATOR,
    ROLE_ORGANIZER,
    ROLE_TICKETER,
    ROLE_TRANSIT,
)
from event_signal.service import DemandSignalService
from event_signal.store import AppendLog, SignalLedger

TZ = timezone(timedelta(hours=8))


def clock(hour: int, minute: int = 0, day: int = 20,
          month: int = 9, year: int = 2026) -> datetime:
    """在 +08:00 城市时区构造绝对时间，跨午夜直接给次日 day。"""

    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def build_service(*, min_cohorts: int = 3, min_sample: int = 10,
                  log_path=None, audit_path=None) -> DemandSignalService:
    parties = PartyDirectory()
    parties.register(Party("org", "主办方", ROLE_ORGANIZER))
    parties.register(Party("tk", "票务方", ROLE_TICKETER))
    parties.register(Party("hotel-a", "江畔酒店", ROLE_HOTEL, sectors=("hotel",)))
    parties.register(Party("hotel-b", "钟楼酒店", ROLE_HOTEL, sectors=("hotel",)))
    parties.register(Party("hotel-c", "北岸酒店", ROLE_HOTEL, sectors=("hotel",)))
    parties.register(Party("metro", "地铁公司", ROLE_TRANSIT, sectors=("transit",)))
    parties.register(Party("diner", "餐饮联采", ROLE_CATERING, sectors=("catering",)))
    parties.register(Party("op", "值班长", ROLE_OPERATOR,
                           can_bypass_threshold=True))

    regions = RegionTree()
    regions.add(Region("city-1", "赛事城市", CITY))
    regions.add(Region("d-1", "河东区", DISTRICT, parent="city-1"))
    regions.add(Region("bc-stadium", "体育场商圈", BUSINESS_CIRCLE, parent="d-1"))
    regions.add(Region("bc-oldtown", "老城商圈", BUSINESS_CIRCLE, parent="d-1"))
    regions.add(Region("v-stadium", "中心体育场", VENUE, parent="bc-stadium"))
    regions.add(Region("v-arena", "副馆", VENUE, parent="bc-stadium"))

    ledger = SignalLedger(AppendLog(log_path))
    return DemandSignalService(
        ledger=ledger, parties=parties, regions=regions,
        metrics=MetricRegistry(), audit=AuditLog(audit_path),
        min_cohorts=min_cohorts, min_sample=min_sample)


def iso(dt: datetime) -> str:
    return dt.isoformat()
