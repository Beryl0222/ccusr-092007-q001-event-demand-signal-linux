"""需求信号协作服务门面。

写路径:发布/修订/撤回信号、登记供给、回填实绩、授权变更 —— 全部以
新版本追加,历史不可变。读路径:按 as-of 与可见级别求值,只输出协议
允许的聚合结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable

from . import analytics, audit
from .access import require_view
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .models import (
    ActualRecord,
    Commitment,
    GeoLevel,
    GrantEvent,
    GrantKind,
    LifecycleStatus,
    Participant,
    Purpose,
    QuantityRange,
    Role,
    SignalRevision,
    Visibility,
)
from .store import EventStore
from .timeutil import Window, parse_instant


def _coerce(enum_cls, value, field: str):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 非法取值: {value!r}") from exc


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 必须为非空字符串")
    return value


class DemandSignalService:
    """协作后端入口。clock 可注入以便测试与回放。"""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        min_contributors: int = analytics.DEFAULT_MIN_CONTRIBUTORS,
    ):
        self.store = EventStore(clock)
        self.min_contributors = min_contributors

    # ---- 注册 ----
    def register_participant(
        self, participant_id: str, role, name: str = ""
    ) -> Participant:
        return self.store.add_participant(
            _require_text(participant_id, "participant_id"),
            _coerce(Role, role, "role"),
            name,
        )

    def register_geo(self, geo_id: str, level, parent_id: str | None = None) -> None:
        self.store.add_geo(
            _require_text(geo_id, "geo_id"), _coerce(GeoLevel, level, "level"), parent_id
        )

    def _participant(self, participant_id: str) -> Participant:
        participant = self.store.participants.get(participant_id)
        if participant is None:
            raise NotFoundError(f"未注册的参与方: {participant_id}")
        return participant

    def _as_of(self, as_of) -> datetime:
        if as_of is None:
            return self.store.now()
        return parse_instant(as_of, field="as_of")

    # ---- 信号生命周期 ----
    def submit_signal(
        self,
        *,
        publisher_id: str,
        source: str,
        record_id: str,
        revision: int,
        occurred_at,
        purpose,
        metric: str,
        window_start,
        window_end,
        geo_id: str,
        low: float,
        expected: float,
        high: float,
        visibility=Visibility.NETWORK,
        allowlist: Iterable[str] = (),
        note: str = "",
    ) -> SignalRevision:
        """发布或修订需求信号。

        同源同 record 的重试(相同 revision 与内容)幂等返回;修订必须使用
        连续递增的 revision;旧版本保留,as-of 查询可重建当时所见。
        """
        publisher = self._participant(publisher_id)
        purpose = _coerce(Purpose, purpose, "purpose")
        visibility = _coerce(Visibility, visibility, "visibility")
        _require_text(source, "source")
        _require_text(record_id, "record_id")
        _require_text(metric, "metric")
        quantity = QuantityRange(low=low, expected=expected, high=high)
        window = Window(window_start, window_end)
        existing_id = self.store.find_signal_id(source, record_id)
        if existing_id is not None:
            latest = self.store.signal_versions(existing_id)[-1]
            if latest.publisher_id != publisher_id:
                raise PermissionDenied(
                    f"信号 {existing_id} 只能由发布方 {latest.publisher_id} 修订"
                )
        revision_obj, _ = self.store.append_signal(
            source=source,
            record_id=record_id,
            revision=revision,
            occurred_at=occurred_at,
            publisher_id=publisher.participant_id,
            purpose=purpose,
            metric=metric,
            window=window,
            geo_id=geo_id,
            quantity=quantity,
            visibility=visibility,
            allowlist=tuple(allowlist),
            status=LifecycleStatus.ACTIVE,
            note=note,
        )
        return revision_obj

    def withdraw_signal(
        self, *, publisher_id: str, source: str, record_id: str, revision: int,
        occurred_at, note: str = "",
    ) -> SignalRevision:
        """撤回信号:追加 status=withdrawn 的新版本,历史版本保留。"""
        self._participant(publisher_id)
        signal_id = self.store.find_signal_id(source, record_id)
        if signal_id is None:
            raise NotFoundError(f"未知信号: {source}/{record_id}")
        latest = self.store.signal_versions(signal_id)[-1]
        if latest.publisher_id != publisher_id:
            raise PermissionDenied(
                f"信号 {signal_id} 只能由发布方 {latest.publisher_id} 撤回"
            )
        revision_obj, _ = self.store.append_signal(
            source=source,
            record_id=record_id,
            revision=revision,
            occurred_at=occurred_at,
            publisher_id=publisher_id,
            purpose=latest.purpose,
            metric=latest.metric,
            window=latest.window,
            geo_id=latest.geo.geo_id,
            quantity=None,
            visibility=latest.visibility,
            allowlist=tuple(latest.allowlist),
            status=LifecycleStatus.WITHDRAWN,
            note=note,
        )
        return revision_obj

    # ---- 供给登记 ----
    def register_commitment(
        self,
        *,
        participant_id: str,
        record_id: str,
        revision: int = 1,
        signal_id: str,
        signal_revision: int | None = None,
        resource: str,
        quantity: float,
        unit_cost: float | None = None,
        unit_margin: float | None = None,
        window_start=None,
        window_end=None,
        geo_id: str | None = None,
        decided_at,
        note: str = "",
    ) -> Commitment:
        """登记运力/供给决定;决定永久绑定登记时所见的信号版本。"""
        self._participant(participant_id)
        _require_text(record_id, "record_id")
        _require_text(resource, "resource")
        decided = parse_instant(decided_at, field="decided_at")
        if decided > self.store.now():
            raise ValidationError("decided_at 不能晚于当前时间")
        if quantity <= 0:
            raise ValidationError("运力/供给数量必须为正;调减请修订既有登记")
        if (window_start is None) != (window_end is None):
            raise ValidationError("window_start 与 window_end 必须同时提供")
        self.store.signal_versions(signal_id)  # 不存在则 NotFound
        if signal_revision is not None:
            seen = self.store.signal_revision(signal_id, signal_revision)
        else:
            seen = self.store.signal_at(signal_id, decided)
            if seen is None:
                raise ValidationError("决定时刻该信号尚未发布")
        if seen.recorded_at > decided:
            raise ValidationError(
                f"决定时刻信号版本 {seen.revision} 尚未发布,登记无效"
            )
        require_view(self.store, participant_id, seen, decided)
        latest_then = self.store.signal_at(signal_id, decided)
        if latest_then is not None and latest_then.status == LifecycleStatus.WITHDRAWN:
            raise ConflictError(f"信号 {signal_id} 在决定时刻已撤回")
        stale = latest_then is not None and latest_then.revision > seen.revision
        window = Window(window_start, window_end) if window_start is not None else seen.window
        commitment, _ = self.store.append_commitment(
            participant_id=participant_id,
            record_id=record_id,
            revision=revision,
            signal_id=signal_id,
            signal_revision=seen.revision,
            resource=resource,
            quantity=quantity,
            unit_cost=unit_cost,
            unit_margin=unit_margin,
            window=window,
            geo_id=geo_id or seen.geo.geo_id,
            decided_at=decided,
            status=LifecycleStatus.ACTIVE,
            stale=stale,
            note=note,
        )
        return commitment

    def withdraw_commitment(
        self, *, participant_id: str, record_id: str, revision: int, decided_at,
        note: str = "",
    ) -> Commitment:
        """撤回登记:追加新版本,历史保留。"""
        self._participant(participant_id)
        commitment_id = EventStore.commitment_key(participant_id, record_id)
        latest = self.store.commitment_versions(commitment_id)[-1]
        commitment, _ = self.store.append_commitment(
            participant_id=participant_id,
            record_id=record_id,
            revision=revision,
            signal_id=latest.signal_id,
            signal_revision=latest.signal_revision,
            resource=latest.resource,
            quantity=latest.quantity,
            unit_cost=latest.unit_cost,
            unit_margin=latest.unit_margin,
            window=latest.window,
            geo_id=latest.geo.geo_id,
            decided_at=decided_at,
            status=LifecycleStatus.WITHDRAWN,
            stale=latest.stale,
            note=note,
        )
        return commitment

    # ---- 实绩回填 ----
    def submit_actual(
        self,
        *,
        participant_id: str,
        record_id: str,
        revision: int = 1,
        metric: str,
        window_start,
        window_end,
        geo_id: str,
        observed: float,
        occurred_at,
    ) -> ActualRecord:
        """实绩回填;迟到或修正数据以递增 revision 形成新版本。"""
        self._participant(participant_id)
        _require_text(record_id, "record_id")
        _require_text(metric, "metric")
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise ValidationError("observed 必须为数值")
        if observed < 0:
            raise ValidationError("实绩不能为负")
        record, _ = self.store.append_actual(
            participant_id=participant_id,
            record_id=record_id,
            revision=revision,
            metric=metric,
            window=Window(window_start, window_end),
            geo_id=geo_id,
            observed=observed,
            occurred_at=occurred_at,
        )
        return record

    # ---- 权限变化 ----
    def grant_access(
        self, *, actor_id: str, signal_id: str, participant_id: str, effective_at
    ) -> GrantEvent:
        return self._grant(
            actor_id=actor_id, signal_id=signal_id, participant_id=participant_id,
            kind=GrantKind.GRANT, effective_at=effective_at,
        )

    def revoke_access(
        self, *, actor_id: str, signal_id: str, participant_id: str, effective_at
    ) -> GrantEvent:
        return self._grant(
            actor_id=actor_id, signal_id=signal_id, participant_id=participant_id,
            kind=GrantKind.REVOKE, effective_at=effective_at,
        )

    def _grant(self, *, actor_id, signal_id, participant_id, kind, effective_at):
        actor = self._participant(actor_id)
        self._participant(participant_id)
        if actor.role != Role.OPS_CENTER:
            latest = self.store.signal_at(signal_id)
            if latest is None:
                raise NotFoundError(f"未知信号: {signal_id}")
            if latest.publisher_id != actor_id:
                raise PermissionDenied("只有发布方或运营中心可以变更授权")
        return self.store.append_grant(
            signal_id=signal_id,
            participant_id=participant_id,
            kind=kind,
            effective_at=effective_at,
            actor_id=actor_id,
        )

    # ---- 查询 ----
    def get_signal(
        self, viewer_id: str, signal_id: str, as_of=None
    ) -> SignalRevision:
        as_of = self._as_of(as_of)
        revision = self.store.signal_at(signal_id, as_of)
        if revision is None:
            raise NotFoundError(f"未知信号: {signal_id}")
        require_view(self.store, viewer_id, revision, as_of)
        return revision

    def signal_history(
        self, viewer_id: str, signal_id: str, as_of=None
    ) -> list[SignalRevision]:
        as_of = self._as_of(as_of)
        latest = self.store.signal_at(signal_id, as_of)
        if latest is None:
            raise NotFoundError(f"未知信号: {signal_id}")
        require_view(self.store, viewer_id, latest, as_of)
        return [
            v for v in self.store.signal_versions(signal_id) if v.recorded_at <= as_of
        ]

    def demand_outlook(
        self, viewer_id: str, geo_id: str, window_start, window_end, as_of=None
    ) -> analytics.OutlookReport:
        return analytics.demand_outlook(
            self.store, viewer_id, geo_id, Window(window_start, window_end),
            as_of=self._as_of(as_of),
        )

    def actual_aggregate(
        self, viewer_id: str, metric: str, geo_id: str, window_start, window_end,
        as_of=None,
    ) -> analytics.ActualAggregate:
        return analytics.aggregate_actuals(
            self.store, viewer_id, metric, geo_id, Window(window_start, window_end),
            as_of=self._as_of(as_of), min_contributors=self.min_contributors,
        )

    def signal_accuracy(
        self, viewer_id: str, signal_id: str, as_of=None
    ) -> analytics.AccuracyReport:
        return analytics.signal_accuracy(
            self.store, viewer_id, signal_id, as_of=self._as_of(as_of),
            min_contributors=self.min_contributors,
        )

    def explain_buildup(
        self, viewer_id: str, geo_id: str, window_start, window_end, as_of=None
    ) -> audit.BuildupReport:
        return audit.explain_buildup(
            self.store, viewer_id, geo_id, Window(window_start, window_end),
            as_of=self._as_of(as_of),
        )

    def loss_attribution(
        self, viewer_id: str, geo_id: str, window_start, window_end, as_of=None
    ) -> audit.LossReport:
        return audit.loss_attribution(
            self.store, viewer_id, geo_id, Window(window_start, window_end),
            as_of=self._as_of(as_of),
        )
