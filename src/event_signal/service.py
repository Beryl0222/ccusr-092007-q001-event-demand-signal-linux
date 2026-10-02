"""应用服务门面：请求字典 <-> 领域对象，串联鉴权、审计与各服务。

接口层（WSGI / 测试）只跟本类打交道，不直接拼模型，保证所有入口
共用同一套校验、盖戳与审计规则。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .aggregation import AggregationService
from .audit import AuditEntry, AuditLog
from .errors import NotFound, PermissionDenied, ValidationError
from .evaluation import EvaluationService
from .geography import Region, RegionTree
from .metrics import MetricRegistry
from .models import (
    ActualOutcome,
    Decision,
    SignalVersion,
    VIS_LEVELS,
)
from .parties import Party, PartyDirectory
from .store import SignalLedger, utc_now
from .timeutils import parse_iso


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _dt(value: Any, field: str) -> datetime:
    try:
        return parse_iso(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"字段 {field}: {exc}") from exc


def _tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"字段 {field} 必须是数组")
    return tuple(value)


class DemandSignalService:
    def __init__(self, ledger: SignalLedger, parties: PartyDirectory,
                 regions: RegionTree, metrics: MetricRegistry,
                 audit: AuditLog, *,
                 min_cohorts: int = 3, min_sample: int = 10) -> None:
        self.ledger = ledger
        self.parties = parties
        self.regions = regions
        self.metrics = metrics
        self.audit = audit
        self.min_cohorts = min_cohorts
        self.min_sample = min_sample
        self.aggregation = AggregationService(
            ledger, parties, regions, metrics,
            min_cohorts=min_cohorts, min_sample=min_sample)
        self.evaluation = EvaluationService(
            ledger, parties, metrics,
            min_cohorts=min_cohorts, min_sample=min_sample)

    # ---- 身份与目录 ------------------------------------------------------

    def _viewer(self, party_id: str) -> Party:
        if not party_id:
            raise ValidationError("缺少参与方身份")
        return self.parties.get(party_id)

    def register_party(self, payload: dict[str, Any]) -> Party:
        try:
            return self.parties.register(Party(
                party_id=payload["party_id"],
                name=payload.get("name", payload["party_id"]),
                role=payload["role"],
                sectors=_tuple(payload.get("sectors"), "sectors"),
                can_bypass_threshold=bool(payload.get("can_bypass_threshold", False)),
            ))
        except KeyError as exc:
            raise ValidationError(f"缺少字段: {exc.args[0]}") from exc

    def add_region(self, payload: dict[str, Any]) -> Region:
        try:
            region = Region(
                code=payload["code"], name=payload.get("name", payload["code"]),
                level=payload["level"], parent=payload.get("parent"))
        except KeyError as exc:
            raise ValidationError(f"缺少字段: {exc.args[0]}") from exc
        if region.parent and not self.regions.exists(region.parent):
            raise ValidationError(f"父地域不存在: {region.parent}")
        return self.regions.add(region)

    def grant(self, payload: dict[str, Any]) -> None:
        self.parties.grant(payload["grantee_id"], payload["group"],
                          effective_from=_dt(payload["effective_from"],
                                             "effective_from"))

    def revoke_group(self, payload: dict[str, Any]) -> None:
        self.parties.revoke(payload["grantee_id"], payload["group"],
                           at=_dt(payload["at"], "at"))

    def suspend_sector(self, payload: dict[str, Any]) -> None:
        self.parties.suspend_sector(payload["grantee_id"], payload["sector"],
                                   at=_dt(payload["at"], "at"))

    def restore_sector(self, payload: dict[str, Any]) -> None:
        self.parties.restore_sector(payload["grantee_id"], payload["sector"],
                                   at=_dt(payload["at"], "at"))

    # ---- 信号发布/修订/撤回 ---------------------------------------------

    def _build_signal(self, payload: dict[str, Any], *, revision: int) -> SignalVersion:
        required = ("publisher_id", "purpose", "metric", "region_code",
                    "window_start", "window_end", "estimate", "unit")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValidationError(f"缺少字段: {missing}")
        visibility = payload.get("visibility", "open")
        if visibility not in VIS_LEVELS:
            raise ValidationError(f"未知可见级别: {visibility}")
        ws, we = (_dt(payload["window_start"], "window_start"),
                  _dt(payload["window_end"], "window_end"))
        if we <= ws:
            raise ValidationError("window_end 必须晚于 window_start（跨午夜直接给跨日绝对时间）")
        self.metrics.get(payload["metric"])  # 未知指标提前失败
        if not self.regions.exists(payload["region_code"]):
            raise ValidationError(f"未知地域: {payload['region_code']}")
        if not self.parties.exists(payload["publisher_id"]):
            raise ValidationError(f"未知发布方: {payload['publisher_id']}")
        try:
            return SignalVersion(
                signal_id=payload.get("signal_id") or _new_id("sig"),
                revision=revision,
                publisher_id=payload["publisher_id"],
                purpose=payload["purpose"],
                metric=payload["metric"],
                region_code=payload["region_code"],
                window_start=ws,
                window_end=we,
                estimate=float(payload["estimate"]),
                unit=payload["unit"],
                ci_low=payload.get("ci_low"),
                ci_high=payload.get("ci_high"),
                confidence=payload.get("confidence"),
                distribution=payload.get("distribution"),
                sample_size=payload.get("sample_size"),
                cohort_key=payload.get("cohort_key"),
                source_ref=payload.get("source_ref"),
                visibility=visibility,
                audience=_tuple(payload.get("audience"), "audience"),
                sectors=_tuple(payload.get("sectors"), "sectors"),
                basis=payload.get("basis", "forecast"),
                note=payload.get("note", ""),
                occurred_at=_dt(payload["occurred_at"], "occurred_at")
                if payload.get("occurred_at") else ws,
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc

    def publish_signal(self, payload: dict[str, Any], *,
                       recorded_at: datetime | None = None) -> SignalVersion:
        ver = self._build_signal(payload, revision=1)
        stamped = self.ledger.publish(ver, recorded_at=recorded_at)
        self._audit(ver.publisher_id, "publish", ver.signal_id, True,
                    {"revision": 1, "metric": ver.metric})
        return stamped

    def revise_signal(self, signal_id: str, payload: dict[str, Any], *,
                      recorded_at: datetime | None = None) -> SignalVersion:
        expected = payload.get("expected_revision")
        actor = payload.get("actor_id")
        current = self.ledger.latest(signal_id)
        if actor and actor != current.publisher_id:
            # 仅发布方可修订自己的信号（运营中心代为纠偏需单独授权，这里拒绝）。
            raise PermissionDenied("只有发布方可以修订该信号")
        changes = {k: v for k, v in payload.items()
                   if k not in ("expected_revision", "actor_id", "recorded_at")}
        for dt_field in ("window_start", "window_end"):
            if dt_field in changes:
                changes[dt_field] = _dt(changes[dt_field], dt_field)
        if "audience" in changes:
            changes["audience"] = _tuple(changes["audience"], "audience")
        if "sectors" in changes:
            changes["sectors"] = _tuple(changes["sectors"], "sectors")
        stamped = self.ledger.revise(
            signal_id, changes=changes, expected_revision=expected,
            recorded_at=recorded_at, note=payload.get("note", ""))
        self._audit(actor or current.publisher_id, "revise", signal_id, True,
                    {"revision": stamped.revision})
        return stamped

    def withdraw_signal(self, signal_id: str, payload: dict[str, Any], *,
                        recorded_at: datetime | None = None) -> SignalVersion:
        actor = payload.get("actor_id")
        current = self.ledger.latest(signal_id)
        if actor and actor != current.publisher_id:
            raise PermissionDenied("只有发布方可以撤回该信号")
        stamped = self.ledger.withdraw(
            signal_id, expected_revision=payload.get("expected_revision"),
            recorded_at=recorded_at, note=payload.get("note", ""))
        self._audit(actor or current.publisher_id, "withdraw", signal_id, True,
                    {"revision": stamped.revision})
        return stamped

    # ---- 决定与实绩 ------------------------------------------------------

    def register_decision(self, payload: dict[str, Any], *,
                          recorded_at: datetime | None = None) -> Decision:
        required = ("party_id", "metric", "region_code", "window_start",
                    "window_end", "provision", "unit", "basis")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValidationError(f"缺少字段: {missing}")
        basis = payload["basis"]
        if not isinstance(basis, dict) or not basis:
            raise ValidationError("basis 必须是非空的 signal_id->revision 映射")
        shares = payload.get("basis_share")
        if shares is not None and set(shares) != set(basis):
            raise ValidationError("basis_share 的键必须与 basis 完全一致")
        try:
            decision = Decision(
                decision_id=payload.get("decision_id") or _new_id("dec"),
                party_id=payload["party_id"],
                metric=payload["metric"],
                region_code=payload["region_code"],
                window_start=_dt(payload["window_start"], "window_start"),
                window_end=_dt(payload["window_end"], "window_end"),
                provision=float(payload["provision"]),
                unit=payload["unit"],
                basis={str(k): int(v) for k, v in basis.items()},
                basis_share=None if shares is None
                else {str(k): float(v) for k, v in shares.items()},
                rationale=payload.get("rationale", ""),
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
        stamped = self.ledger.register_decision(decision, recorded_at=recorded_at)
        self._audit(decision.party_id, "register_decision",
                    decision.decision_id, True, {"basis": decision.basis})
        return stamped

    def record_outcome(self, payload: dict[str, Any], *,
                       recorded_at: datetime | None = None) -> ActualOutcome:
        required = ("metric", "region_code", "window_start", "window_end",
                    "actual", "unit", "sample_size", "cohort_key", "source")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValidationError(f"缺少字段: {missing}")
        if int(payload["sample_size"]) <= 0:
            raise ValidationError("实绩必须带正样本量")
        try:
            outcome = ActualOutcome(
                outcome_id=payload.get("outcome_id") or _new_id("out"),
                metric=payload["metric"],
                region_code=payload["region_code"],
                window_start=_dt(payload["window_start"], "window_start"),
                window_end=_dt(payload["window_end"], "window_end"),
                actual=float(payload["actual"]),
                unit=payload["unit"],
                sample_size=int(payload["sample_size"]),
                cohort_key=str(payload["cohort_key"]),
                source=payload["source"],
                note=payload.get("note", ""),
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError(str(exc)) from exc
        stamped = self.ledger.record_outcome(outcome, recorded_at=recorded_at)
        self._audit(outcome.source, "record_outcome", outcome.outcome_id, True,
                    {"metric": outcome.metric, "cohort_key": outcome.cohort_key})
        return stamped

    # ---- 读取：历史版本 / 聚合 / 重建 ------------------------------------

    def signal_history(self, viewer_id: str, signal_id: str, *,
                       as_of: datetime | None = None) -> list[dict[str, Any]]:
        viewer = self._viewer(viewer_id)
        versions = self.ledger.versions(signal_id)
        if not versions:
            raise NotFound(f"未知信号: {signal_id}")
        result = []
        for ver in versions:
            if as_of is not None and ver.recorded_at and ver.recorded_at > as_of:
                continue
            moment = as_of or (ver.recorded_at or utc_now())
            can = self.parties.can_see(
                viewer, publisher=self.parties.get(ver.publisher_id),
                visibility=ver.visibility, audience=ver.audience,
                industry_sectors=ver.sectors, moment=moment)
            if not can:
                continue
            result.append(ver.to_dict())
        if not result:
            raise PermissionDenied("无权查看该信号的任何版本")
        return result

    def query_aggregate(self, viewer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        viewer = self._viewer(viewer_id)
        required = ("metric", "region_code", "window_start", "window_end")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValidationError(f"缺少字段: {missing}")
        as_of = _dt(payload["as_of"], "as_of") if payload.get("as_of") else utc_now()
        result = self.aggregation.aggregate(
            viewer,
            metric=payload["metric"],
            region_code=payload["region_code"],
            window_start=_dt(payload["window_start"], "window_start"),
            window_end=_dt(payload["window_end"], "window_end"),
            as_of=as_of,
            basis=payload.get("basis", "forecast"),
            hourly=bool(payload.get("hourly", False)),
        )
        self._audit(viewer_id, "aggregate",
                    f"{payload['metric']}:{payload['region_code']}",
                    True, {"cohort_count": result.cohort_count,
                           "bypassed": result.bypassed_threshold})
        return result.to_dict()

    def reconstruct(self, viewer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        viewer = self._viewer(viewer_id)
        required = ("metric", "region_code", "window_start", "window_end")
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValidationError(f"缺少字段: {missing}")
        report = self.evaluation.reconstruct(
            metric=payload["metric"],
            region_code=payload["region_code"],
            window_start=_dt(payload["window_start"], "window_start"),
            window_end=_dt(payload["window_end"], "window_end"),
            viewer=viewer,
        )
        self._audit(viewer_id, "reconstruct",
                    f"{payload['metric']}:{payload['region_code']}",
                    True, {"suppressed": report["summary"]["suppressed"]})
        return report

    # ---- 审计 ------------------------------------------------------------

    def _audit(self, actor: str, action: str, target: str,
               granted: bool, detail: dict[str, Any]) -> None:
        self.audit.record(AuditEntry(
            at=utc_now(), actor=actor, action=action, target=target,
            granted=granted, detail=detail))
