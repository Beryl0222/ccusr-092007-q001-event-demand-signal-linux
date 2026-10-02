"""审计与重建:回答"为何增配、哪些信号失准、损失来自哪条决策链"。

重建类查询仅对运营中心(值班人员)开放;所有结论都锚定不可变的历史
版本,事后修订、撤回、迟到数据都不会改变"当时所见"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .errors import PermissionDenied
from .models import LifecycleStatus, QuantityRange, Role
from .store import EventStore
from .timeutil import Window


def _require_ops(store: EventStore, viewer_id: str) -> None:
    viewer = store.participants.get(viewer_id)
    if viewer is None or viewer.role != Role.OPS_CENTER:
        raise PermissionDenied("仅运营中心可执行该审计查询")


@dataclass(frozen=True)
class SignalSnapshot:
    """登记决定时所见的信号版本快照。"""

    signal_id: str
    revision: int
    publisher_id: str
    source: str
    record_id: str
    metric: str
    occurred_at: datetime
    quantity: QuantityRange | None
    status: LifecycleStatus


@dataclass(frozen=True)
class CommitmentExplanation:
    commitment_id: str
    participant_id: str
    resource: str
    quantity: float
    geo_id: str
    window: Window
    decided_at: datetime
    stale: bool
    signal: SignalSnapshot


@dataclass(frozen=True)
class ResourceTotal:
    resource: str
    quantity: float
    commitments: int


@dataclass(frozen=True)
class BuildupReport:
    geo_id: str
    window: Window
    as_of: datetime
    entries: tuple[CommitmentExplanation, ...]
    totals_by_resource: tuple[ResourceTotal, ...]


def explain_buildup(
    store: EventStore, viewer_id: str, geo_id: str, window: Window, as_of: datetime
) -> BuildupReport:
    """重建某商圈在某时间窗为何增配:每条登记 → 当时所见的信号版本 → 发布方。"""
    _require_ops(store, viewer_id)
    store.geo_ref(geo_id)
    entries: list[CommitmentExplanation] = []
    for commitment in store.latest_commitments(as_of):
        if commitment.status != LifecycleStatus.ACTIVE:
            continue
        if commitment.decided_at > as_of:
            continue
        if not store.is_descendant(commitment.geo.geo_id, geo_id):
            continue
        if not commitment.window.overlaps(window):
            continue
        rev = store.signal_revision(commitment.signal_id, commitment.signal_revision)
        entries.append(
            CommitmentExplanation(
                commitment_id=commitment.commitment_id,
                participant_id=commitment.participant_id,
                resource=commitment.resource,
                quantity=commitment.quantity,
                geo_id=commitment.geo.geo_id,
                window=commitment.window,
                decided_at=commitment.decided_at,
                stale=commitment.stale,
                signal=SignalSnapshot(
                    signal_id=rev.signal_id,
                    revision=rev.revision,
                    publisher_id=rev.publisher_id,
                    source=rev.source,
                    record_id=rev.record_id,
                    metric=rev.metric,
                    occurred_at=rev.occurred_at,
                    quantity=rev.quantity,
                    status=rev.status,
                ),
            )
        )
    entries.sort(key=lambda e: (e.decided_at, e.commitment_id))
    totals: dict[str, list] = {}
    for entry in entries:
        bucket = totals.setdefault(entry.resource, [0.0, 0])
        bucket[0] += entry.quantity
        bucket[1] += 1
    return BuildupReport(
        geo_id=geo_id,
        window=window,
        as_of=as_of,
        entries=tuple(entries),
        totals_by_resource=tuple(
            ResourceTotal(resource=resource, quantity=value[0], commitments=value[1])
            for resource, value in sorted(totals.items())
        ),
    )


@dataclass(frozen=True)
class LossEntry:
    commitment_id: str
    participant_id: str
    resource: str
    provisioned: float
    actual: float | None          # 该参与方自身实绩(不触碰他方明细)
    over_provisioned: float | None
    unmet: float | None
    loss: float | None            # 多备×单位成本 + 缺口×单位收益
    unpriced: bool                # 存在正损失分量但缺少单价
    signal_id: str
    signal_revision: int
    publisher_id: str
    decided_at: datetime


@dataclass(frozen=True)
class LossReport:
    geo_id: str
    window: Window
    as_of: datetime
    entries: tuple[LossEntry, ...]
    total_loss: float
    by_signal: tuple[tuple[str, float], ...]
    by_publisher: tuple[tuple[str, float], ...]


def loss_attribution(
    store: EventStore, viewer_id: str, geo_id: str, window: Window, as_of: datetime
) -> LossReport:
    """损失归因:每条损失沿 登记 → 所见信号版本 → 发布方 的决策链回溯。"""
    _require_ops(store, viewer_id)
    store.geo_ref(geo_id)
    entries: list[LossEntry] = []
    for commitment in store.latest_commitments(as_of):
        if commitment.status != LifecycleStatus.ACTIVE:
            continue
        if commitment.decided_at > as_of:
            continue
        if not store.is_descendant(commitment.geo.geo_id, geo_id):
            continue
        if not commitment.window.overlaps(window):
            continue
        actual = _own_actual(store, commitment, as_of)
        if actual is None:
            over = unmet = loss = None
            unpriced = False
        else:
            over = max(0.0, commitment.quantity - actual)
            unmet = max(0.0, actual - commitment.quantity)
            parts: list[float] = []
            unpriced = False
            if over > 0:
                if commitment.unit_cost is None:
                    unpriced = True
                else:
                    parts.append(over * commitment.unit_cost)
            if unmet > 0:
                if commitment.unit_margin is None:
                    unpriced = True
                else:
                    parts.append(unmet * commitment.unit_margin)
            loss = sum(parts) if parts else (None if unpriced else 0.0)
        rev = store.signal_revision(commitment.signal_id, commitment.signal_revision)
        entries.append(
            LossEntry(
                commitment_id=commitment.commitment_id,
                participant_id=commitment.participant_id,
                resource=commitment.resource,
                provisioned=commitment.quantity,
                actual=actual,
                over_provisioned=over,
                unmet=unmet,
                loss=loss,
                unpriced=unpriced,
                signal_id=commitment.signal_id,
                signal_revision=commitment.signal_revision,
                publisher_id=rev.publisher_id,
                decided_at=commitment.decided_at,
            )
        )
    entries.sort(key=lambda e: (e.decided_at, e.commitment_id))
    return LossReport(
        geo_id=geo_id,
        window=window,
        as_of=as_of,
        entries=tuple(entries),
        total_loss=sum(e.loss for e in entries if e.loss is not None),
        by_signal=_group_sum(entries, key=lambda e: e.signal_id),
        by_publisher=_group_sum(entries, key=lambda e: e.publisher_id),
    )


def _own_actual(store: EventStore, commitment, as_of: datetime) -> float | None:
    """登记方自身的实绩(按时间窗重叠比例分摊),不依赖他方数据。"""
    total = 0.0
    found = False
    for record in store.latest_actuals(as_of):
        if record.participant_id != commitment.participant_id:
            continue
        if record.metric != commitment.resource:
            continue
        if not store.is_descendant(record.geo.geo_id, commitment.geo.geo_id):
            continue
        inter = record.window.intersection(commitment.window)
        if inter is None:
            continue
        total += record.observed * (inter.duration / record.window.duration)
        found = True
    return total if found else None


def _group_sum(entries: list[LossEntry], key) -> tuple[tuple[str, float], ...]:
    totals: dict[str, float] = {}
    for entry in entries:
        if entry.loss is None:
            continue
        group = key(entry)
        totals[group] = totals.get(group, 0.0) + entry.loss
    return tuple(sorted(totals.items()))
