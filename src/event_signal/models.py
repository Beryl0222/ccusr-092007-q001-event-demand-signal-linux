"""信号、决策与实绩的不可变记录模型。

三类记录都只追加、不原地改写：

* :class:`SignalVersion` —— 同一 ``signal_id`` 的每次发布/修订/撤回都是新版本，
  ``revision`` 单调递增，``supersedes`` 指向上一版本；
* :class:`Decision` —— 接收方登记的运力/供给决定，记录决策时所见的精确
  版本号（basis），事后信号修订不会改变这一依据；
* :class:`ActualOutcome` —— 赛后实绩回填，与预测分开存放，供误差核算。

字段命名沿用样例合同：``schema_version / record_id / domain / occurred_at /
revision / source``。其中 signal 域的 ``record_id`` 即 ``signal_id``，
``source`` 为发布方标识，``occurred_at`` 为业务事件时间（信号描述的时间窗起点）。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

from .timeutils import parse_iso, to_iso

SCHEMA_VERSION = 2
DOMAIN_SIGNAL = "event_signal"          # 与 fixtures 样例保持一致
DOMAIN_DECISION = "supply_decision"
DOMAIN_OUTCOME = "actual_outcome"

# 可见级别
VIS_OPEN = "open"
VIS_INDUSTRY = "industry"
VIS_PARTIES = "parties"
VIS_PRIVATE = "private"
VIS_LEVELS = (VIS_OPEN, VIS_INDUSTRY, VIS_PARTIES, VIS_PRIVATE)

# 生命周期
STATUS_ACTIVE = "active"
STATUS_WITHDRAWN = "withdrawn"
#: v1 样例记录迁入 v2 时的初始状态（迁移说明见 README）。
STATUS_MIGRATED = "migrated"

# 数据依据
BASIS_FORECAST = "forecast"
BASIS_ACTUAL = "actual"


@dataclass(frozen=True)
class SignalVersion:
    # ---- 合同标识（对齐 fixtures/demand_signal.json） ----
    signal_id: str
    revision: int
    publisher_id: str
    purpose: str                         # 发布用途，如 "hotel_blocking"
    metric: str
    region_code: str
    window_start: datetime
    window_end: datetime
    estimate: float
    unit: str
    # ---- 置信范围 ----
    ci_low: float | None = None
    ci_high: float | None = None
    confidence: float | None = None      # 0~1
    distribution: str | None = None      # 如 "point" / "uniform" / 经验分布引用
    # ---- 去重与脱敏 ----
    sample_size: int | None = None       # 该估计背后的订单/样本数
    cohort_key: str | None = None        # 同源去重键（同渠道同一批数据）
    source_ref: str | None = None        # 发布方系统内的幂等引用
    # ---- 可见性 ----
    visibility: str = VIS_OPEN
    audience: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    # ---- 版本链 ----
    basis: str = BASIS_FORECAST          # forecast | actual
    status: str = STATUS_ACTIVE
    supersedes: int | None = None
    note: str = ""
    # ---- 双时态时间戳 ----
    occurred_at: datetime | None = None  # 业务时间（缺省取窗起点）
    recorded_at: datetime | None = None  # 系统接收时间，由存储层盖戳
    replaced_at: datetime | None = None  # 被下一版本取代的时间（链上回填，仅查询视图用）

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("revision 自 1 起")
        if self.window_end <= self.window_start:
            raise ValueError("时间窗结束必须晚于开始")
        if self.visibility not in VIS_LEVELS:
            raise ValueError(f"未知可见级别: {self.visibility}")
        if self.ci_low is not None and self.ci_high is not None:
            if self.ci_low > self.estimate or self.ci_high < self.estimate:
                raise ValueError("估计点必须落在置信区间内")
            if self.ci_low > self.ci_high:
                raise ValueError("置信区间下界不能大于上界")
        if self.confidence is not None and not 0 < self.confidence <= 1:
            raise ValueError("confidence 应在 (0,1]")
        if self.basis not in (BASIS_FORECAST, BASIS_ACTUAL):
            raise ValueError(f"未知依据: {self.basis}")

    @property
    def effective_time(self) -> datetime:
        return self.occurred_at or self.window_start

    @property
    def withdrawn(self) -> bool:
        return self.status == STATUS_WITHDRAWN

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("window_start", "window_end", "occurred_at",
                    "recorded_at", "replaced_at"):
            if d[key] is not None:
                d[key] = to_iso(d[key])
        d.update({
            "schema_version": SCHEMA_VERSION,
            "record_id": self.signal_id,
            "domain": DOMAIN_SIGNAL,
            "source": self.publisher_id,
        })
        return d


@dataclass(frozen=True)
class Decision:
    """接收方基于其当时所见信号版本作出的供给决定。"""

    decision_id: str
    party_id: str
    metric: str
    region_code: str
    window_start: datetime
    window_end: datetime
    provision: float                     # 增配的运力/供给量（标准单位）
    unit: str
    basis: dict[str, int]                # signal_id -> 决策所见 revision
    basis_share: dict[str, float] | None = None  # 各依据信号对 provision 的归因权重
    rationale: str = ""
    recorded_at: datetime | None = None
    # 决定本身也可被修订（追加新版本）；撤回链与信号同构。
    revision: int = 1
    supersedes: int | None = None
    status: str = STATUS_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("window_start", "window_end", "recorded_at"):
            if d[key] is not None:
                d[key] = to_iso(d[key])
        d.update({
            "schema_version": SCHEMA_VERSION,
            "record_id": self.decision_id,
            "domain": DOMAIN_DECISION,
            "source": self.party_id,
            "occurred_at": to_iso(self.window_start),
        })
        return d


@dataclass(frozen=True)
class ActualOutcome:
    """实绩回填。一个 (metric, region, window, cohort_key) 一条。"""

    outcome_id: str
    metric: str
    region_code: str
    window_start: datetime
    window_end: datetime
    actual: float
    unit: str
    sample_size: int
    cohort_key: str
    source: str                          # 回填方（通常为票务方/运营中心）
    note: str = ""
    recorded_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("window_start", "window_end", "recorded_at"):
            if d[key] is not None:
                d[key] = to_iso(d[key])
        d.update({
            "schema_version": SCHEMA_VERSION,
            "record_id": self.outcome_id,
            "domain": DOMAIN_OUTCOME,
            "occurred_at": to_iso(self.window_start),
            "revision": 1,
        })
        return d
