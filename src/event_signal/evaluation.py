"""预测实绩误差、损失归因与决策链重建。

赛后（或任何时点）值班员需要回答三类问题：

* 哪些信号失准——把决策当时所见版本与其 cohort 的事后实绩对齐，算误差、
  偏差、置信区间是否命中、相对误差；
* 损失由何种决策链产生——决定登记了 basis（所见版本），过度备货与缺口
  成本按决定总量与跨 cohort 实绩汇总计算，每个依据信号单独给出误差，
  信号后来被修订时还能区分“当时所见”与“最新预测”的差距；
* 迟到数据改变了什么——对比决策时点版本与最新版本，量化修订幅度。

全部只输出聚合/归因数字，不触碰任何单个订单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .aggregation import allocate_cohorts
from .errors import NotFound, SuppressedAggregate
from .metrics import MetricRegistry
from .models import (
    ActualOutcome,
    Decision,
    SignalVersion,
    STATUS_WITHDRAWN,
)
from .parties import Party, PartyDirectory, ROLE_OPERATOR
from .store import SignalLedger
from .timeutils import overlap_seconds, to_iso

#: 各类资源的默认单位成本：over=过度备货单位成本，under=缺口单位成本。
DEFAULT_COSTS: dict[str, dict[str, float]] = {
    "hotel_demand": {"over": 1.0, "under": 4.0},       # 空房成本低，住不进代价高
    "transit_demand": {"over": 0.5, "under": 3.0},
    "transit_capacity": {"over": 0.5, "under": 3.0},
    "catering_demand": {"over": 1.5, "under": 2.0},    # 食材报废贵
    "attraction_demand": {"over": 0.3, "under": 2.0},
    "labor_supply": {"over": 1.2, "under": 2.5},
}


@dataclass(frozen=True)
class SignalScore:
    signal_id: str
    revision_seen: int
    point: float
    actual: float
    abs_error: float
    rel_error: float | None
    bias: float                          # >0 高估，<0 低估
    ci_hit: bool | None
    revised_after: bool                  # 决策后该信号是否又出新版
    latest_revision: int
    latest_point: float
    late_change: float | None            # 所见版本 -> 最新版本 的变化量
    withdrawn: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "revision_seen": self.revision_seen,
            "point": _r(self.point),
            "actual": _r(self.actual),
            "abs_error": _r(self.abs_error),
            "rel_error": None if self.rel_error is None else _r(self.rel_error),
            "bias": _r(self.bias),
            "ci_hit": self.ci_hit,
            "revised_after": self.revised_after,
            "latest_revision": self.latest_revision,
            "latest_point": _r(self.latest_point),
            "late_change": None if self.late_change is None else _r(self.late_change),
            "withdrawn": self.withdrawn,
        }


@dataclass(frozen=True)
class DecisionAttribution:
    decision_id: str
    party_id: str
    provision: float
    actual: float | None
    overage: float
    shortfall: float
    over_cost: float
    under_cost: float
    total_cost: float
    basis: tuple[SignalScore | None, ...]
    window_start: datetime
    window_end: datetime
    rationale: str
    decided_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "party_id": self.party_id,
            "provision": _r(self.provision),
            "actual": None if self.actual is None else _r(self.actual),
            "overage": _r(self.overage),
            "shortfall": _r(self.shortfall),
            "over_cost": _r(self.over_cost),
            "under_cost": _r(self.under_cost),
            "total_cost": _r(self.total_cost),
            "window_start": to_iso(self.window_start),
            "window_end": to_iso(self.window_end),
            "rationale": self.rationale,
            "decided_at": None if self.decided_at is None else to_iso(self.decided_at),
            "basis": [None if b is None else b.to_dict() for b in self.basis],
        }


@dataclass(frozen=True)
class TimelineEvent:
    at: datetime
    kind: str                            # signal_published|revised|withdrawn|decision|outcome
    ref: str
    revision: int | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": to_iso(self.at),
            "kind": self.kind,
            "ref": self.ref,
            "revision": self.revision,
            "summary": self.summary,
        }


def _r(x: float) -> float:
    return round(x + 0.0, 3)


class EvaluationService:
    def __init__(self, ledger: SignalLedger, parties: PartyDirectory,
                 metrics: MetricRegistry, *,
                 costs: dict[str, dict[str, float]] | None = None,
                 min_cohorts: int = 3,
                 min_sample: int = 10) -> None:
        self.ledger = ledger
        self.parties = parties
        self.metrics = metrics
        self.costs = costs or DEFAULT_COSTS
        self.min_cohorts = min_cohorts
        self.min_sample = min_sample

    # ---- 实绩对齐（跨 cohort，与信号同口径） -----------------------------

    def _matching_outcomes(self, metric: str, region_code: str,
                           win_start: datetime, win_end: datetime) -> list[ActualOutcome]:
        return [
            o for o in self.ledger.outcomes()
            if o.metric == metric and o.region_code == region_code
            and overlap_seconds(o.window_start, o.window_end, win_start, win_end) > 0
        ]

    def _allocate_actuals(self, outcomes: list[ActualOutcome],
                          win_start: datetime, win_end: datetime):
        """逐秒 cohort 口径汇总实绩。

        同 cohort 多份回填（分时段补报、赛后修正）按回填时间裁决，
        后到的实绩覆盖先到的时间片；不同 cohort 相加。
        """

        return allocate_cohorts(
            outcomes,
            cohort_of=lambda o: o.cohort_key,
            window_of=lambda o: (o.window_start, o.window_end),
            value_of=lambda o: o.actual,
            ci_of=lambda o: (None, None),
            normalize=lambda o: self.metrics.normalize(o.metric, 1.0, o.unit),
            priority_key=lambda o: (o.recorded_at or o.window_start,),
            b_start=win_start, b_end=win_end,
        )

    def _actual_for_signal(self, ver: SignalVersion,
                           outcomes: list[ActualOutcome]) -> float | None:
        """信号所属 cohort 的实绩；信号无 cohort 或该 cohort 未回填则为 None。"""

        if not ver.cohort_key:
            return None
        own = [o for o in outcomes if o.cohort_key == ver.cohort_key]
        if not own:
            return None
        return self._allocate_actuals(own, ver.window_start, ver.window_end).point

    def _check_outcome_privacy(self, outcomes: list[ActualOutcome]) -> None:
        cohorts = {o.cohort_key for o in outcomes}
        sample = sum(o.sample_size for o in outcomes)
        if len(cohorts) < self.min_cohorts or sample < self.min_sample:
            raise SuppressedAggregate(
                cohort_count=len(cohorts), min_cohorts=self.min_cohorts
            )

    # ---- 单信号评分 ------------------------------------------------------

    def score_signal(self, ver: SignalVersion, *,
                     outcomes: list[ActualOutcome] | None = None) -> SignalScore:
        if outcomes is None:
            outcomes = self._matching_outcomes(ver.metric, ver.region_code,
                                               ver.window_start, ver.window_end)
        actual = self._actual_for_signal(ver, outcomes)
        if actual is None:
            raise NotFound(f"该信号 cohort 尚无匹配实绩: {ver.signal_id}")
        factor = self.metrics.normalize(ver.metric, 1.0, ver.unit)
        point = ver.estimate * factor
        err = point - actual
        latest = self.ledger.latest(ver.signal_id)
        latest_factor = self.metrics.normalize(ver.metric, 1.0, latest.unit)
        ci_hit = None
        if ver.ci_low is not None and ver.ci_high is not None:
            ci_hit = ver.ci_low * factor <= actual <= ver.ci_high * factor
        changed = latest.revision != ver.revision
        return SignalScore(
            signal_id=ver.signal_id,
            revision_seen=ver.revision,
            point=point,
            actual=actual,
            abs_error=abs(err),
            rel_error=abs(err) / actual if actual else None,
            bias=err,
            ci_hit=ci_hit,
            revised_after=changed,
            latest_revision=latest.revision,
            latest_point=latest.estimate * latest_factor,
            late_change=(latest.estimate * latest_factor - point) if changed else None,
            withdrawn=latest.status == STATUS_WITHDRAWN,
        )

    # ---- 决定归因与损失 --------------------------------------------------

    def attribute_decision(self, decision: Decision, *,
                           viewer: Party | None = None) -> DecisionAttribution:
        """按 provision 与跨 cohort 实绩算过度/缺口成本，basis 逐信号给误差。

        没有任何实绩时，actual 与成本留空（None），但仍返回决策链结构，
        保证“为何这么配”随时可重建。
        """

        outcomes = self._matching_outcomes(decision.metric, decision.region_code,
                                           decision.window_start, decision.window_end)
        factor = self.metrics.normalize(decision.metric, 1.0, decision.unit)
        provision = decision.provision * factor
        actual = (self._allocate_actuals(outcomes, decision.window_start,
                                         decision.window_end).point
                  if outcomes else None)

        overage = max(0.0, provision - actual) if actual is not None else 0.0
        shortfall = max(0.0, actual - provision) if actual is not None else 0.0
        cost = self.costs.get(decision.metric, {"over": 1.0, "under": 1.0})
        over_cost = overage * cost["over"]
        under_cost = shortfall * cost["under"]

        scores: list[SignalScore | None] = []
        for sid, rev in decision.basis.items():
            ver = self.ledger.version(sid, rev)
            try:
                scores.append(self.score_signal(ver, outcomes=outcomes))
            except NotFound:
                # 该依据信号的 cohort 没有实绩：链条保留、误差留空。
                scores.append(None)

        return DecisionAttribution(
            decision_id=decision.decision_id,
            party_id=decision.party_id,
            provision=provision,
            actual=actual,
            overage=overage,
            shortfall=shortfall,
            over_cost=over_cost,
            under_cost=under_cost,
            total_cost=over_cost + under_cost,
            basis=tuple(scores),
            window_start=decision.window_start,
            window_end=decision.window_end,
            rationale=decision.rationale,
            decided_at=decision.recorded_at,
        )

    # ---- 商圈重建 --------------------------------------------------------

    def reconstruct(self, *, metric: str, region_code: str,
                    window_start: datetime, window_end: datetime,
                    viewer: Party) -> dict[str, Any]:
        """重建某商圈某时段“为什么这样配、后来哪失准、损失从哪条链来”。

        时间线对所有有权查看者一致；决定与成本仅本人或运营中心可见。
        实绩 cohort 不足隐私阈值时，只返回时间线，抑制全部误差/成本。
        """

        timeline: list[TimelineEvent] = []
        related_decisions: list[Decision] = []

        for d in self.ledger.decisions():
            if d.metric != metric or d.region_code != region_code:
                continue
            if overlap_seconds(d.window_start, d.window_end,
                               window_start, window_end) <= 0:
                continue
            if viewer.role != ROLE_OPERATOR and d.party_id != viewer.party_id:
                continue
            related_decisions.append(d)

        related_sids = {sid for d in related_decisions for sid in d.basis}
        for sid in related_sids:
            for ver in self.ledger.versions(sid):
                if overlap_seconds(ver.window_start, ver.window_end,
                                   window_start, window_end) <= 0:
                    continue
                if ver.recorded_at is None:
                    continue
                if ver.status == STATUS_WITHDRAWN:
                    kind, summary = "signal_withdrawn", f"{sid} r{ver.revision} 撤回"
                elif ver.revision == 1:
                    kind = "signal_published"
                    summary = (f"{sid} r1 发布 估计={_r(ver.estimate)}{ver.unit} "
                               f"窗 {to_iso(ver.window_start)}~{to_iso(ver.window_end)}")
                else:
                    prev = self.ledger.version(sid, ver.supersedes)
                    kind = "signal_revised"
                    summary = (f"{sid} r{ver.revision} 修订 "
                               f"估计 {_r(prev.estimate)}->{_r(ver.estimate)}"
                               f"（基于 r{ver.supersedes}）")
                timeline.append(TimelineEvent(
                    ver.recorded_at, kind, sid, ver.revision, summary))

        for d in related_decisions:
            timeline.append(TimelineEvent(
                d.recorded_at or d.window_start, "decision", d.decision_id,
                d.revision,
                f"{d.party_id} 按 {d.basis} 增配 {_r(d.provision)}{d.unit}：{d.rationale}"))

        outcomes = self._matching_outcomes(metric, region_code,
                                           window_start, window_end)

        # 隐私门槛：相关实绩 cohort 不足时，不输出任何实绩/误差/成本
        # （时间线里也不逐条列实绩，避免单批数据泄露），只保留信号与决定。
        attributions: list[DecisionAttribution] = []
        suppressed = False
        privacy_ok = True
        if outcomes:
            try:
                self._check_outcome_privacy(outcomes)
            except SuppressedAggregate:
                privacy_ok = False
                suppressed = True
        if privacy_ok:
            for d in related_decisions:
                attributions.append(self.attribute_decision(d, viewer=viewer))
            if outcomes:
                total_actual = self._allocate_actuals(
                    outcomes, window_start, window_end).point
                last_outcome_at = max(o.recorded_at for o in outcomes
                                      if o.recorded_at)
                if last_outcome_at:
                    timeline.append(TimelineEvent(
                        last_outcome_at, "outcome",
                        f"{metric}:{region_code}", 1,
                        f"实绩聚合回填={_r(total_actual)}"
                        f"（跨 {len({o.cohort_key for o in outcomes})} 个 cohort）"))

        timeline.sort(key=lambda e: e.at)

        total_cost = sum(a.total_cost for a in attributions)
        total_over = sum(a.over_cost for a in attributions)
        total_under = sum(a.under_cost for a in attributions)

        return {
            "metric": metric,
            "region_code": region_code,
            "window_start": to_iso(window_start),
            "window_end": to_iso(window_end),
            "viewer": viewer.party_id,
            "timeline": [e.to_dict() for e in timeline],
            "decisions": [a.to_dict() for a in attributions],
            "summary": {
                "decision_count": len(related_decisions),
                "outcome_cohorts": len({o.cohort_key for o in outcomes}),
                "total_cost": _r(total_cost),
                "over_provision_cost": _r(total_over),
                "shortfall_cost": _r(total_under),
                "suppressed": suppressed,
            },
        }
