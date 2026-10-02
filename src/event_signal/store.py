"""只追加的事件存储:所有状态变化都是新记录,支持 as-of 历史重建。

内存实现;生产环境可替换为持久化存储,接口保持不变。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterator

from .dedup import append_version, fingerprint_payload
from .errors import NotFoundError, ValidationError
from .models import (
    GEO_DEPTH,
    ActualRecord,
    Commitment,
    GeoLevel,
    GeoRef,
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
from .timeutil import Window, parse_instant


def _latest_at(versions: list, as_of: datetime):
    """取 recorded_at <= as_of 的最新版本。"""
    candidates = [v for v in versions if v.recorded_at <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (v.recorded_at, v.revision))


class EventStore:
    """只追加存储:信号、供给登记、实绩、授权事件的版本链。"""

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.participants: dict[str, Participant] = {}
        self.geo_levels: dict[str, GeoLevel] = {}
        self.geo_parents: dict[str, str | None] = {}
        self._signals: dict[str, list[SignalRevision]] = {}
        self._signal_ids: dict[tuple[str, str], str] = {}
        self._commitments: dict[str, list[Commitment]] = {}
        self._actuals: dict[str, list[ActualRecord]] = {}
        self._grants: list[GrantEvent] = []

    def now(self) -> datetime:
        return self._clock()

    # ---- 参与方 ----
    def add_participant(
        self, participant_id: str, role: Role, name: str = ""
    ) -> Participant:
        if participant_id in self.participants:
            raise ValidationError(f"参与方已存在: {participant_id}")
        participant = Participant(
            participant_id=participant_id,
            role=role,
            name=name or participant_id,
            registered_at=self.now(),
        )
        self.participants[participant_id] = participant
        return participant

    # ---- 地域层级 ----
    def add_geo(
        self, geo_id: str, level: GeoLevel, parent_id: str | None = None
    ) -> None:
        if geo_id in self.geo_levels:
            raise ValidationError(f"地域已注册: {geo_id}")
        if parent_id is not None:
            parent_level = self.geo_levels.get(parent_id)
            if parent_level is None:
                raise NotFoundError(f"上级地域未注册: {parent_id}")
            if GEO_DEPTH[level] <= GEO_DEPTH[parent_level]:
                raise ValidationError(
                    f"地域层级不合法: {level.value} 不能挂在 {parent_level.value} 下"
                )
        self.geo_levels[geo_id] = level
        self.geo_parents[geo_id] = parent_id

    def geo_ref(self, geo_id: str) -> GeoRef:
        level = self.geo_levels.get(geo_id)
        if level is None:
            raise NotFoundError(f"未注册的地域: {geo_id}")
        return GeoRef(level=level, geo_id=geo_id)

    def is_descendant(self, geo_id: str, ancestor_id: str) -> bool:
        """geo_id 是否位于 ancestor_id 的子树内(含自身)。"""
        current: str | None = geo_id
        while current is not None:
            if current == ancestor_id:
                return True
            current = self.geo_parents.get(current)
        return False

    # ---- 信号 ----
    @staticmethod
    def signal_key(source: str, record_id: str) -> str:
        return f"{source}/{record_id}"

    def find_signal_id(self, source: str, record_id: str) -> str | None:
        return self._signal_ids.get((source, record_id))

    def append_signal(
        self,
        *,
        source: str,
        record_id: str,
        revision: int,
        occurred_at,
        publisher_id: str,
        purpose: Purpose,
        metric: str,
        window: Window,
        geo_id: str,
        quantity: QuantityRange | None,
        visibility: Visibility,
        allowlist,
        status: LifecycleStatus,
        note: str = "",
    ) -> tuple[SignalRevision, bool]:
        occurred_at = parse_instant(occurred_at, field="occurred_at")
        signal_id = self._signal_ids.get((source, record_id)) or self.signal_key(
            source, record_id
        )
        geo = self.geo_ref(geo_id)
        fingerprint = fingerprint_payload(
            {
                "source": source,
                "record_id": record_id,
                "revision": revision,
                "occurred_at": occurred_at,
                "publisher_id": publisher_id,
                "purpose": purpose,
                "metric": metric,
                "window": window,
                "geo_id": geo_id,
                "quantity": None
                if quantity is None
                else {
                    "low": quantity.low,
                    "expected": quantity.expected,
                    "high": quantity.high,
                },
                "visibility": visibility,
                "allowlist": sorted(allowlist),
                "status": status,
                "note": note,
            }
        )
        versions = self._signals.setdefault(signal_id, [])
        revision_obj, created = append_version(
            versions,
            revision,
            fingerprint,
            lambda: SignalRevision(
                signal_id=signal_id,
                source=source,
                record_id=record_id,
                revision=revision,
                occurred_at=occurred_at,
                recorded_at=self.now(),
                publisher_id=publisher_id,
                purpose=purpose,
                metric=metric,
                window=window,
                geo=geo,
                quantity=quantity,
                visibility=visibility,
                allowlist=frozenset(allowlist),
                status=status,
                note=note,
                fingerprint=fingerprint,
            ),
        )
        if created:
            self._signal_ids[(source, record_id)] = signal_id
        return revision_obj, created

    def signal_versions(self, signal_id: str) -> list[SignalRevision]:
        versions = self._signals.get(signal_id)
        if not versions:
            raise NotFoundError(f"未知信号: {signal_id}")
        return list(versions)

    def signal_revision(self, signal_id: str, revision: int) -> SignalRevision:
        for version in self.signal_versions(signal_id):
            if version.revision == revision:
                return version
        raise NotFoundError(f"信号 {signal_id} 无 revision {revision}")

    def signal_at(
        self, signal_id: str, as_of: datetime | None = None
    ) -> SignalRevision | None:
        versions = self._signals.get(signal_id)
        if not versions:
            return None
        return _latest_at(versions, as_of or self.now())

    def latest_signals(self, as_of: datetime | None = None) -> list[SignalRevision]:
        as_of = as_of or self.now()
        out = []
        for versions in self._signals.values():
            version = _latest_at(versions, as_of)
            if version is not None:
                out.append(version)
        return out

    # ---- 供给登记 ----
    @staticmethod
    def commitment_key(participant_id: str, record_id: str) -> str:
        return f"{participant_id}/{record_id}"

    def append_commitment(
        self,
        *,
        participant_id: str,
        record_id: str,
        revision: int,
        signal_id: str,
        signal_revision: int,
        resource: str,
        quantity: float,
        unit_cost: float | None,
        unit_margin: float | None,
        window: Window,
        geo_id: str,
        decided_at,
        status: LifecycleStatus,
        stale: bool,
        note: str = "",
    ) -> tuple[Commitment, bool]:
        decided_at = parse_instant(decided_at, field="decided_at")
        commitment_id = self.commitment_key(participant_id, record_id)
        geo = self.geo_ref(geo_id)
        # stale 由当时系统状态推导,不属于业务内容,不参与指纹,
        # 否则同一登记在信号更新后重试会被误判为冲突。
        fingerprint = fingerprint_payload(
            {
                "participant_id": participant_id,
                "record_id": record_id,
                "revision": revision,
                "signal_id": signal_id,
                "signal_revision": signal_revision,
                "resource": resource,
                "quantity": quantity,
                "unit_cost": unit_cost,
                "unit_margin": unit_margin,
                "window": window,
                "geo_id": geo_id,
                "decided_at": decided_at,
                "status": status,
                "note": note,
            }
        )
        versions = self._commitments.setdefault(commitment_id, [])
        commitment, created = append_version(
            versions,
            revision,
            fingerprint,
            lambda: Commitment(
                commitment_id=commitment_id,
                revision=revision,
                participant_id=participant_id,
                signal_id=signal_id,
                signal_revision=signal_revision,
                resource=resource,
                quantity=quantity,
                unit_cost=unit_cost,
                unit_margin=unit_margin,
                window=window,
                geo=geo,
                decided_at=decided_at,
                recorded_at=self.now(),
                status=status,
                stale=stale,
                note=note,
                fingerprint=fingerprint,
            ),
        )
        return commitment, created

    def commitment_versions(self, commitment_id: str) -> list[Commitment]:
        versions = self._commitments.get(commitment_id)
        if not versions:
            raise NotFoundError(f"未知供给登记: {commitment_id}")
        return list(versions)

    def commitment_at(
        self, commitment_id: str, as_of: datetime | None = None
    ) -> Commitment | None:
        versions = self._commitments.get(commitment_id)
        if not versions:
            return None
        return _latest_at(versions, as_of or self.now())

    def latest_commitments(self, as_of: datetime | None = None) -> list[Commitment]:
        as_of = as_of or self.now()
        out = []
        for versions in self._commitments.values():
            version = _latest_at(versions, as_of)
            if version is not None:
                out.append(version)
        return out

    def all_commitment_versions(self) -> Iterator[Commitment]:
        for versions in self._commitments.values():
            yield from versions

    # ---- 实绩 ----
    @staticmethod
    def actual_key(participant_id: str, record_id: str) -> str:
        return f"{participant_id}/{record_id}"

    def append_actual(
        self,
        *,
        participant_id: str,
        record_id: str,
        revision: int,
        metric: str,
        window: Window,
        geo_id: str,
        observed: float,
        occurred_at,
    ) -> tuple[ActualRecord, bool]:
        occurred_at = parse_instant(occurred_at, field="occurred_at")
        series_id = self.actual_key(participant_id, record_id)
        geo = self.geo_ref(geo_id)
        fingerprint = fingerprint_payload(
            {
                "participant_id": participant_id,
                "record_id": record_id,
                "revision": revision,
                "metric": metric,
                "window": window,
                "geo_id": geo_id,
                "observed": observed,
                "occurred_at": occurred_at,
            }
        )
        versions = self._actuals.setdefault(series_id, [])
        record, created = append_version(
            versions,
            revision,
            fingerprint,
            lambda: ActualRecord(
                series_id=series_id,
                record_id=record_id,
                revision=revision,
                participant_id=participant_id,
                metric=metric,
                window=window,
                geo=geo,
                observed=observed,
                occurred_at=occurred_at,
                recorded_at=self.now(),
                fingerprint=fingerprint,
            ),
        )
        return record, created

    def actual_versions(self, series_id: str) -> list[ActualRecord]:
        versions = self._actuals.get(series_id)
        if not versions:
            raise NotFoundError(f"未知实绩序列: {series_id}")
        return list(versions)

    def latest_actuals(self, as_of: datetime | None = None) -> list[ActualRecord]:
        as_of = as_of or self.now()
        out = []
        for versions in self._actuals.values():
            version = _latest_at(versions, as_of)
            if version is not None:
                out.append(version)
        return out

    # ---- 授权事件 ----
    def append_grant(
        self,
        *,
        signal_id: str,
        participant_id: str,
        kind: GrantKind,
        effective_at,
        actor_id: str,
    ) -> GrantEvent:
        effective_at = parse_instant(effective_at, field="effective_at")
        event = GrantEvent(
            signal_id=signal_id,
            participant_id=participant_id,
            kind=kind,
            effective_at=effective_at,
            recorded_at=self.now(),
            actor_id=actor_id,
        )
        self._grants.append(event)
        return event

    def grant_events(
        self,
        signal_id: str | None = None,
        participant_id: str | None = None,
    ) -> list[GrantEvent]:
        return [
            event
            for event in self._grants
            if (signal_id is None or event.signal_id == signal_id)
            and (participant_id is None or event.participant_id == participant_id)
        ]
