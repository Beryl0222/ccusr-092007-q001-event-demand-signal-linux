"""核心领域模型:参与方、信号版本、供给登记、实绩回填与授权事件。

所有模型均为不可变(frozen dataclass)。任何修订、撤回或迟到数据都以
新的版本记录追加,绝不改写历史版本 —— 已采取行动的参与方当时所见的
信息必须可以被完整重建。

标识与时间语义与 fixtures/demand_signal.json 保持一致的合同:
(source, record_id) 是业务身份,revision 单调递增,occurred_at 是业务
时间;模型额外引入 recorded_at(系统追加时间)以区分迟到数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .errors import ValidationError
from .timeutil import Window

SCHEMA_VERSION = 1
DOMAIN = "event_signal"


class Role(str, Enum):
    ORGANIZER = "organizer"      # 主办方
    TICKETING = "ticketing"      # 票务方
    OPERATOR = "operator"        # 行业经营者(酒店/交通/餐饮/景区)
    OPS_CENTER = "ops_center"    # 城市运营中心


class Visibility(str, Enum):
    PUBLIC = "public"            # 所有人可见
    NETWORK = "network"          # 协作网络内注册参与方可见
    RESTRICTED = "restricted"    # 白名单或显式授权可见
    PRIVATE = "private"          # 仅发布方与运营中心


class LifecycleStatus(str, Enum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"      # 撤回是新版本的状态,不是删除


class GeoLevel(str, Enum):
    CITY = "city"
    DISTRICT = "district"
    BLOCK = "block"
    VENUE = "venue"


GEO_DEPTH = {
    GeoLevel.CITY: 0,
    GeoLevel.DISTRICT: 1,
    GeoLevel.BLOCK: 2,
    GeoLevel.VENUE: 3,
}


class Purpose(str, Enum):
    CAPACITY_PLANNING = "capacity_planning"  # 运力规划
    STAFFING = "staffing"                    # 排班
    STOCKING = "stocking"                    # 备货
    TRANSPORT = "transport"                  # 交通调度
    SECURITY = "security"                    # 安保
    GENERAL = "general"


class GrantKind(str, Enum):
    GRANT = "grant"
    REVOKE = "revoke"


@dataclass(frozen=True)
class GeoRef:
    level: GeoLevel
    geo_id: str


@dataclass(frozen=True)
class QuantityRange:
    """置信范围:下限 / 预期 / 上限。"""

    low: float
    expected: float
    high: float

    def __post_init__(self) -> None:
        for name in ("low", "expected", "high"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"数量 {name} 必须为数值")
            if value < 0:
                raise ValidationError(f"数量 {name} 不能为负")
        if not (self.low <= self.expected <= self.high):
            raise ValidationError("置信范围必须满足 low <= expected <= high")

    def scaled(self, factor: float) -> "QuantityRange":
        return QuantityRange(
            low=self.low * factor,
            expected=self.expected * factor,
            high=self.high * factor,
        )


@dataclass(frozen=True)
class Participant:
    participant_id: str
    role: Role
    name: str
    registered_at: datetime


@dataclass(frozen=True)
class SignalRevision:
    """需求信号的不可变版本。

    修订、撤回、迟到更正都产生新的 SignalRevision;旧版本永久保留,
    供 as-of 查询重建"当时所见"。撤回版本 quantity 为 None。
    """

    signal_id: str
    source: str
    record_id: str
    revision: int
    occurred_at: datetime       # 业务时间(合同字段)
    recorded_at: datetime       # 系统追加时间
    publisher_id: str
    purpose: Purpose
    metric: str                 # 需求指标,如 visitor_arrivals / room_nights
    window: Window
    geo: GeoRef
    quantity: QuantityRange | None
    visibility: Visibility
    allowlist: frozenset[str]
    status: LifecycleStatus
    note: str
    fingerprint: str
    schema_version: int = SCHEMA_VERSION
    domain: str = DOMAIN


@dataclass(frozen=True)
class Commitment:
    """接收方基于所见信号版本登记的运力/供给决定(不可变版本)。

    signal_revision 永久固定为决定时所见的信号版本;stale 标记决定时
    信号已有更新版本(仍允许登记,供审计还原决策依据)。
    """

    commitment_id: str
    revision: int
    participant_id: str
    signal_id: str
    signal_revision: int
    resource: str               # 与实绩 metric 对应,如 hotel_rooms / shuttle_seats
    quantity: float             # 增配数量(调减通过修订登记实现)
    unit_cost: float | None     # 每单位运力成本(损失归因用)
    unit_margin: float | None   # 每单位服务收益(缺口损失用)
    window: Window
    geo: GeoRef
    decided_at: datetime
    recorded_at: datetime
    status: LifecycleStatus
    stale: bool
    note: str
    fingerprint: str


@dataclass(frozen=True)
class ActualRecord:
    """实绩回填(不可变版本);迟到或修正数据以新 revision 追加。

    实绩只以 (参与方, 指标, 地域, 时间窗) 的聚合形式上送,
    单个订单明细不进入系统。
    """

    series_id: str
    record_id: str
    revision: int
    participant_id: str
    metric: str
    window: Window
    geo: GeoRef
    observed: float
    occurred_at: datetime
    recorded_at: datetime
    fingerprint: str


@dataclass(frozen=True)
class GrantEvent:
    """权限变化事件(追加式,不改写);按 effective_at 参与 as-of 求值。"""

    signal_id: str
    participant_id: str
    kind: GrantKind
    effective_at: datetime
    recorded_at: datetime
    actor_id: str
