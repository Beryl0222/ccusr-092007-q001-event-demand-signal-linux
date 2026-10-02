"""聚合与误差计算:只输出协议允许的聚合结果,个体订单不出系统。

隐私协议:非运营中心视角下,贡献方少于 k(默认 3)的实绩聚合被抑制;
需求侧按可见级别过滤后再聚合;同源重叠只告警不擅自合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .access import can_view
from .dedup import OverlapWarning, find_same_source_overlaps
from .errors import PermissionDenied
from .models import LifecycleStatus, QuantityRange, Role, SignalRevision
from .store import EventStore
from .timeutil import Window

DEFAULT_MIN_CONTRIBUTORS = 3
_EPS = 1e-9


@dataclass(frozen=True)
class OutlookLine:
    signal_id: str
    record_id: str
    source: str
    publisher_id: str
    revision: int
    metric: str
    geo_id: str
    window: Window
    quantity: QuantityRange
    prorated: bool  # 查询窗口只覆盖信号窗口的一部分,已按时间比例分摊


@dataclass(frozen=True)
class MetricTotal:
    metric: str
    low: float
    expected: float
    high: float
    publishers: int


@dataclass(frozen=True)
class OutlookReport:
    viewer_id: str
    geo_id: str
    window: Window
    as_of: datetime
    lines: tuple[OutlookLine, ...]
    totals: tuple[MetricTotal, ...]
    warnings: tuple[OverlapWarning, ...]
    context: tuple[OutlookLine, ...]  # 上级地域信号,仅作背景不计入合计


def demand_outlook(
    store: EventStore,
    viewer_id: str,
    geo_id: str,
    window: Window,
    as_of: datetime,
) -> OutlookReport:
    """某地域、某时间窗的需求聚合(按查看者可见性过滤)。"""
    if viewer_id not in store.participants:
        raise PermissionDenied(f"未注册的参与方: {viewer_id}")
    store.geo_ref(geo_id)
    lines: list[OutlookLine] = []
    context: list[OutlookLine] = []
    used: list[SignalRevision] = []
    for rev in store.latest_signals(as_of):
        if rev.status != LifecycleStatus.ACTIVE or rev.quantity is None:
            continue
        if not rev.window.overlaps(window):
            continue
        if not can_view(store, viewer_id, rev, as_of):
            continue
        if store.is_descendant(rev.geo.geo_id, geo_id):
            factor = rev.window.fraction_covered_by(window)
            lines.append(
                OutlookLine(
                    signal_id=rev.signal_id,
                    record_id=rev.record_id,
                    source=rev.source,
                    publisher_id=rev.publisher_id,
                    revision=rev.revision,
                    metric=rev.metric,
                    geo_id=rev.geo.geo_id,
                    window=rev.window,
                    quantity=rev.quantity.scaled(factor),
                    prorated=factor < 1.0 - _EPS,
                )
            )
            used.append(rev)
        elif store.is_descendant(geo_id, rev.geo.geo_id):
            context.append(
                OutlookLine(
                    signal_id=rev.signal_id,
                    record_id=rev.record_id,
                    source=rev.source,
                    publisher_id=rev.publisher_id,
                    revision=rev.revision,
                    metric=rev.metric,
                    geo_id=rev.geo.geo_id,
                    window=rev.window,
                    quantity=rev.quantity,
                    prorated=False,
                )
            )
    return OutlookReport(
        viewer_id=viewer_id,
        geo_id=geo_id,
        window=window,
        as_of=as_of,
        lines=tuple(lines),
        totals=_totals(lines),
        warnings=tuple(find_same_source_overlaps(used)),
        context=tuple(context),
    )


def _totals(lines: list[OutlookLine]) -> tuple[MetricTotal, ...]:
    by_metric: dict[str, list[OutlookLine]] = {}
    for line in lines:
        by_metric.setdefault(line.metric, []).append(line)
    totals = []
    for metric in sorted(by_metric):
        group = by_metric[metric]
        totals.append(
            MetricTotal(
                metric=metric,
                low=sum(line.quantity.low for line in group),
                expected=sum(line.quantity.expected for line in group),
                high=sum(line.quantity.high for line in group),
                publishers=len({line.publisher_id for line in group}),
            )
        )
    return tuple(totals)


@dataclass(frozen=True)
class ActualAggregate:
    metric: str
    geo_id: str
    window: Window
    observed: float | None   # None = 被抑制或无数据
    contributors: int
    series: tuple[str, ...]
    prorated: bool
    suppressed: bool
    has_data: bool


def aggregate_actuals(
    store: EventStore,
    viewer_id: str,
    metric: str,
    geo_id: str,
    window: Window,
    as_of: datetime,
    min_contributors: int = DEFAULT_MIN_CONTRIBUTORS,
) -> ActualAggregate:
    """实绩聚合:子地域汇总 + 时间窗按比例分摊 + k 匿名抑制。"""
    if viewer_id not in store.participants:
        raise PermissionDenied(f"未注册的参与方: {viewer_id}")
    store.geo_ref(geo_id)
    observed = 0.0
    contributors: set[str] = set()
    series: list[str] = []
    prorated = False
    for record in store.latest_actuals(as_of):
        if record.metric != metric:
            continue
        if not store.is_descendant(record.geo.geo_id, geo_id):
            continue
        inter = record.window.intersection(window)
        if inter is None:
            continue
        factor = inter.duration / record.window.duration
        observed += record.observed * factor
        prorated = prorated or factor < 1.0 - _EPS
        contributors.add(record.participant_id)
        series.append(record.series_id)
    viewer = store.participants[viewer_id]
    has_data = bool(series)
    suppressed = (
        has_data
        and viewer.role != Role.OPS_CENTER
        and len(contributors) < min_contributors
    )
    return ActualAggregate(
        metric=metric,
        geo_id=geo_id,
        window=window,
        observed=None if (suppressed or not has_data) else observed,
        contributors=len(contributors),
        series=tuple(series),
        prorated=prorated,
        suppressed=suppressed,
        has_data=has_data,
    )


@dataclass(frozen=True)
class RevisionAccuracy:
    revision: int
    occurred_at: datetime
    recorded_at: datetime
    status: LifecycleStatus
    forecast: QuantityRange | None
    actual: float | None
    error: float | None          # 实绩 - 预期
    abs_pct_error: float | None  # |误差| / 实绩
    within_range: bool | None    # 实绩是否落在置信区间内
    commitments_acted: int       # 有多少供给登记依据该版本行动
    prorated: bool
    suppressed: bool


@dataclass(frozen=True)
class AccuracyReport:
    signal_id: str
    viewer_id: str
    as_of: datetime
    metric: str
    geo_id: str
    window: Window
    entries: tuple[RevisionAccuracy, ...]
    actual_contributors: int
    suppressed: bool


def signal_accuracy(
    store: EventStore,
    viewer_id: str,
    signal_id: str,
    as_of: datetime,
    min_contributors: int = DEFAULT_MIN_CONTRIBUTORS,
) -> AccuracyReport:
    """逐版本误差回看:每个历史版本对照当前实绩,标出哪些版本被行动采用。"""
    versions = store.signal_versions(signal_id)
    viewer = store.participants.get(viewer_id)
    if viewer is None:
        raise PermissionDenied(f"未注册的参与方: {viewer_id}")
    publisher_id = versions[-1].publisher_id
    if viewer.role != Role.OPS_CENTER and viewer_id != publisher_id:
        raise PermissionDenied("误差回看仅对发布方与运营中心开放")
    entries: list[RevisionAccuracy] = []
    suppressed_any = False
    contributors_max = 0
    for rev in versions:
        acted = len(
            {
                c.commitment_id
                for c in store.all_commitment_versions()
                if c.signal_id == signal_id
                and c.signal_revision == rev.revision
                and c.decided_at <= as_of
            }
        )
        if rev.status == LifecycleStatus.WITHDRAWN or rev.quantity is None:
            entries.append(
                RevisionAccuracy(
                    revision=rev.revision,
                    occurred_at=rev.occurred_at,
                    recorded_at=rev.recorded_at,
                    status=rev.status,
                    forecast=None,
                    actual=None,
                    error=None,
                    abs_pct_error=None,
                    within_range=None,
                    commitments_acted=acted,
                    prorated=False,
                    suppressed=False,
                )
            )
            continue
        aggregate = aggregate_actuals(
            store, viewer_id, rev.metric, rev.geo.geo_id, rev.window,
            as_of=as_of, min_contributors=min_contributors,
        )
        contributors_max = max(contributors_max, aggregate.contributors)
        suppressed_any = suppressed_any or aggregate.suppressed
        actual = aggregate.observed
        if actual is None:
            error = pct = within = None
        else:
            error = actual - rev.quantity.expected
            pct = abs(error) / actual if actual else None
            within = rev.quantity.low <= actual <= rev.quantity.high
        entries.append(
            RevisionAccuracy(
                revision=rev.revision,
                occurred_at=rev.occurred_at,
                recorded_at=rev.recorded_at,
                status=rev.status,
                forecast=rev.quantity,
                actual=actual,
                error=error,
                abs_pct_error=pct,
                within_range=within,
                commitments_acted=acted,
                prorated=aggregate.prorated,
                suppressed=aggregate.suppressed,
            )
        )
    latest = versions[-1]
    return AccuracyReport(
        signal_id=signal_id,
        viewer_id=viewer_id,
        as_of=as_of,
        metric=latest.metric,
        geo_id=latest.geo.geo_id,
        window=latest.window,
        entries=tuple(entries),
        actual_contributors=contributors_max,
        suppressed=suppressed_any,
    )
