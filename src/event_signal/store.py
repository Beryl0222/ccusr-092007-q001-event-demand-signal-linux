"""只追加存储与版本链。

存储介质是一行一个 JSON 的追加日志（JSONL）。任何修订、撤回、迟到数据都
*追加* 一条记录，从不修改或删除已有行；崩溃恢复时整份重放即可。

双时态：

* ``occurred_at`` / ``window_*`` 是**业务时间**——信号描述的现实时段；
* ``recorded_at`` 是**系统时间**——运营中心何时收到该版本。

两者解耦后，迟到数据（业务时间早已过去才送达）能正确形成新版本，
而值班员也能用 :meth:`SignalLedger.version_as_of` 还原“某一刻系统里
究竟看到的是哪个版本”。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import (
    ConflictError,
    DuplicateSubmission,
    NotFound,
    StaleRevision,
)
from .models import (
    ActualOutcome,
    Decision,
    SignalVersion,
    STATUS_WITHDRAWN,
)
from .timeutils import parse_iso, to_iso

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

_DATETIME_KEYS = {
    "signal": ("window_start", "window_end", "occurred_at", "recorded_at", "replaced_at"),
    "decision": ("window_start", "window_end", "recorded_at"),
    "outcome": ("window_start", "window_end", "recorded_at"),
}


def _encode(kind: str, obj: Any) -> dict[str, Any]:
    payload = obj.to_dict()
    payload["_kind"] = kind
    return payload


def _decode(row: dict[str, Any]) -> tuple[str, Any]:
    kind = row["_kind"]
    # 按数据类真实字段白名单取值，自动剔除合同别名与派生键，
    # 避免 to_dict() 增加的 occurred_at/revision 等造成多余关键字。
    if kind == "signal":
        cls = SignalVersion
    elif kind == "decision":
        cls = Decision
    else:
        cls = ActualOutcome
    allowed = {f.name for f in fields(cls)}
    data = {k: v for k, v in row.items() if k in allowed}
    for key in _DATETIME_KEYS[kind]:
        if data.get(key) is not None:
            data[key] = parse_iso(data[key])
    if kind == "signal":
        data["signal_id"] = row["record_id"]
        data["publisher_id"] = row["source"]
        data["revision"] = row["revision"]
        return kind, SignalVersion(**data)
    if kind == "decision":
        data["decision_id"] = row["record_id"]
        data["party_id"] = row["source"]
        return kind, Decision(**data)
    data["outcome_id"] = row["record_id"]
    data["source"] = row["source"]
    return kind, ActualOutcome(**data)


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

class AppendLog:
    """JSONL 追加日志；``path=None`` 时为纯内存日志（测试用）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._rows: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._rows.append(json.loads(line))

    def append(self, row: dict[str, Any]) -> None:
        self._rows.append(row)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


# --------------------------------------------------------------------------
# 信号台账
# --------------------------------------------------------------------------

class SignalLedger:
    def __init__(self, log: AppendLog, *, clock: Clock = utc_now) -> None:
        self.log = log
        self.clock = clock
        self._signals: dict[str, list[SignalVersion]] = defaultdict(list)
        self._idempotency: dict[tuple[str, str], tuple[str, int]] = {}
        self._decisions: dict[str, list[Decision]] = defaultdict(list)
        self._outcomes: list[ActualOutcome] = []
        self._replay()

    def _replay(self) -> None:
        for row in self.log.rows():
            kind, obj = _decode(row)
            if kind == "signal":
                self._index_signal(obj)
            elif kind == "decision":
                self._decisions[obj.decision_id].append(obj)
            else:
                self._outcomes.append(obj)

    def _index_signal(self, ver: SignalVersion) -> None:
        self._signals[ver.signal_id].append(ver)
        if ver.source_ref:
            key = (ver.publisher_id, ver.source_ref)
            # 仅首次出现占用幂等键（重放时同键重复即同一版本）。
            self._idempotency.setdefault(key, (ver.signal_id, ver.revision))

    # ---- 发布 / 修订 / 撤回 ---------------------------------------------

    def publish(self, ver: SignalVersion, *, recorded_at: datetime | None = None) -> SignalVersion:
        if ver.source_ref:
            dup = self._idempotency.get((ver.publisher_id, ver.source_ref))
            if dup is not None:
                # 同发布方同 source_ref 即同一批数据：网络重试/重复报送都落在这里，
                # 不占用第二次计数，返回既有版本号供调用方对齐。
                raise DuplicateSubmission(dup[0], existing_revision=dup[1])
        if ver.revision != 1:
            raise ConflictError("首次发布 revision 必须为 1")
        if ver.signal_id in self._signals:
            raise ConflictError(f"信号已存在，修订请走 revise: {ver.signal_id}")
        stamped = self._stamp(ver, recorded_at)
        self.log.append(_encode("signal", stamped))
        self._index_signal(stamped)
        return stamped

    def revise(self, signal_id: str, *, changes: dict[str, Any],
               expected_revision: int | None = None,
               recorded_at: datetime | None = None,
               note: str = "") -> SignalVersion:
        """基于最新版本（或调用方声明的版本）追加修订版。

        调用方携带 ``expected_revision`` 时做乐观并发检查：期间若已有他人
        修订，抛出 :class:`StaleRevision`，避免覆盖式更新。
        """

        current = self.latest(signal_id)
        if current.status == STATUS_WITHDRAWN:
            raise ConflictError("信号已撤回，不能修订；请重新发布")
        if expected_revision is not None and expected_revision != current.revision:
            raise StaleRevision(
                f"依据版本 {expected_revision} 已过期，最新为 r{current.revision}",
                details={"latest": current.revision},
            )
        from dataclasses import replace
        allowed = {
            "purpose", "metric", "region_code", "estimate", "unit",
            "ci_low", "ci_high", "confidence", "distribution",
            "sample_size", "cohort_key", "visibility", "audience",
            "sectors", "window_start", "window_end", "basis", "note",
        }
        bad = set(changes) - allowed
        if bad:
            raise ConflictError(f"字段不允许通过修订修改: {sorted(bad)}")
        data = {k: getattr(current, k) for k in allowed}
        data.update({k: v for k, v in changes.items() if k in allowed})
        data["note"] = note or changes.get("note", current.note)
        # 只改点值而未给新区间时，旧置信区间不再包裹新点，自动失效，
        # 避免产生自相矛盾的版本。
        if "estimate" in changes and not ({"ci_low", "ci_high"} & set(changes)):
            data["ci_low"] = data["ci_high"] = None
        new_ver = replace(
            current,
            revision=current.revision + 1,
            supersedes=current.revision,
            status="active",
            **data,
        )
        stamped = self._stamp(new_ver, recorded_at)
        self.log.append(_encode("signal", stamped))
        self._index_signal(stamped)
        return stamped

    def withdraw(self, signal_id: str, *, expected_revision: int | None = None,
                 recorded_at: datetime | None = None, note: str = "") -> SignalVersion:
        """撤回 = 追加一块墓碑版本，历史版本全部保留。"""

        current = self.latest(signal_id)
        if expected_revision is not None and expected_revision != current.revision:
            raise StaleRevision(
                f"依据版本 {expected_revision} 已过期，最新为 r{current.revision}",
                details={"latest": current.revision},
            )
        if current.status == STATUS_WITHDRAWN:
            raise ConflictError("信号已处于撤回状态")
        from dataclasses import replace
        tomb = replace(
            current,
            revision=current.revision + 1,
            supersedes=current.revision,
            status=STATUS_WITHDRAWN,
            note=note or f"撤回：{current.note}",
        )
        stamped = self._stamp(tomb, recorded_at)
        self.log.append(_encode("signal", stamped))
        self._index_signal(stamped)
        return stamped

    def _stamp(self, ver: SignalVersion, recorded_at: datetime | None) -> SignalVersion:
        from dataclasses import replace
        stamp = recorded_at or self.clock()
        if stamp.tzinfo is None:
            raise ValueError("recorded_at 必须带时区")
        return replace(ver, recorded_at=stamp)

    # ---- 读取 -------------------------------------------------------------

    def latest(self, signal_id: str) -> SignalVersion:
        versions = self._signals.get(signal_id)
        if not versions:
            raise NotFound(f"未知信号: {signal_id}")
        return versions[-1]

    def version(self, signal_id: str, revision: int) -> SignalVersion:
        """取精确版本——决策审计时还原“当时所见”的入口。"""

        for ver in self._signals.get(signal_id, ()):
            if ver.revision == revision:
                return ver
        raise NotFound(f"{signal_id} 不存在 r{revision}")

    def versions(self, signal_id: str) -> list[SignalVersion]:
        return list(self._signals.get(signal_id, ()))

    def version_as_of(self, signal_id: str, as_of: datetime) -> SignalVersion:
        """系统时间 ``as_of`` 那一刻能看到的最新版本（未送达的不可见）。"""

        visible = [v for v in self._signals.get(signal_id, ())
                   if v.recorded_at is not None and v.recorded_at <= as_of]
        if not visible:
            raise NotFound(f"{signal_id} 在 {to_iso(as_of)} 前尚无版本")
        return visible[-1]

    def all_versions(self, *, recorded_before: datetime | None = None) -> Iterable[SignalVersion]:
        for versions in self._signals.values():
            for ver in versions:
                if recorded_before and ver.recorded_at and ver.recorded_at > recorded_before:
                    continue
                yield ver

    def current_versions(self, *, as_of: datetime | None = None) -> list[SignalVersion]:
        """每个信号的最新版本（或 ``as_of`` 时点的最新版本）。"""

        out: list[SignalVersion] = []
        for sid, versions in self._signals.items():
            if as_of is None:
                out.append(versions[-1])
            else:
                visible = [v for v in versions
                           if v.recorded_at is not None and v.recorded_at <= as_of]
                if visible:
                    out.append(visible[-1])
        return out

    # ---- 决策 -------------------------------------------------------------

    def register_decision(self, decision: Decision, *,
                          recorded_at: datetime | None = None) -> Decision:
        # basis 中的每个版本必须真实存在；不强制当时是否最新——审计要的就是事实。
        for sid, rev in decision.basis.items():
            self.version(sid, rev)
        if decision.decision_id in self._decisions and decision.revision == 1:
            raise ConflictError(f"决定已存在: {decision.decision_id}")
        from dataclasses import replace
        stamped = replace(decision, recorded_at=recorded_at or self.clock())
        self.log.append(_encode("decision", stamped))
        self._decisions[stamped.decision_id].append(stamped)
        return stamped

    def revise_decision(self, decision_id: str, *, provision: float | None = None,
                        rationale: str | None = None,
                        basis: dict[str, int] | None = None,
                        recorded_at: datetime | None = None) -> Decision:
        versions = self._decisions[decision_id]
        cur = versions[-1]
        for sid, rev in (basis or {}).items():
            self.version(sid, rev)
        from dataclasses import replace
        new = replace(
            cur,
            revision=cur.revision + 1,
            supersedes=cur.revision,
            provision=cur.provision if provision is None else provision,
            rationale=cur.rationale if rationale is None else rationale,
            basis=dict(cur.basis, **(basis or {})),
        )
        stamped = replace(new, recorded_at=recorded_at or self.clock())
        self.log.append(_encode("decision", stamped))
        self._decisions[decision_id].append(stamped)
        return stamped

    def latest_decision(self, decision_id: str) -> Decision:
        versions = self._decisions.get(decision_id)
        if not versions:
            raise NotFound(f"未知决定: {decision_id}")
        return versions[-1]

    def decisions(self) -> list[Decision]:
        return [vs[-1] for vs in self._decisions.values()]

    def decision_history(self, decision_id: str) -> list[Decision]:
        return list(self._decisions.get(decision_id, ()))

    # ---- 实绩 -------------------------------------------------------------

    def record_outcome(self, outcome: ActualOutcome, *,
                       recorded_at: datetime | None = None) -> ActualOutcome:
        from dataclasses import replace
        stamped = replace(outcome, recorded_at=recorded_at or self.clock())
        self.log.append(_encode("outcome", stamped))
        self._outcomes.append(stamped)
        return stamped

    def outcomes(self, *, as_of: datetime | None = None) -> list[ActualOutcome]:
        """同一键的实绩若被多次回填，取 ``as_of`` 前最后一条。"""

        latest: dict[tuple, ActualOutcome] = {}
        for o in self._outcomes:
            if as_of and o.recorded_at and o.recorded_at > as_of:
                continue
            key = (o.metric, o.region_code, o.window_start, o.window_end, o.cohort_key)
            latest[key] = o
        return list(latest.values())
