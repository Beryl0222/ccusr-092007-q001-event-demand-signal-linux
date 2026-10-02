"""可见性过滤、同源去重与隐私安全聚合。

核心难题有三个：

1. **重复计数**——同一批订单可能被票务方、主办方平台、商户各报一次。
   信号携带 ``cohort_key`` 标注底层数据批次；同一 cohort 在同一时段只计一次，
   多个同 cohort 信号时间窗部分重叠时，按发布方优先级逐秒裁决（高优先级
   覆盖的时间片不再计入低优先级），既不双算也不漏算不重叠的部分。
2. **量纲/时间窗不一致**——聚合前先把单位归一到标准单位，再按信号窗长把
   估计量均摊到秒，按与查询窗的重叠比例计入。
3. **不泄露单个订单**——输出至少跨 ``min_cohorts`` 个不同 cohort，
   否则整体抑制（运营中心特许身份可越过，但访问会被标记审计）。
   结果里只给聚合值、区间和 cohort 数，不给任何单条明细。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .errors import SuppressedAggregate
from .geography import RegionTree
from .metrics import MetricRegistry
from .models import (
    BASIS_FORECAST,
    STATUS_MIGRATED,
    STATUS_WITHDRAWN,
    SignalVersion,
)
from .parties import Party, PartyDirectory, ROLE_OPERATOR
from .store import SignalLedger
from .timeutils import hourly_buckets, overlap_seconds, window_seconds, to_iso

#: 同 cohort 多来源时的发布方角色优先级（序号小者权威）。
DEFAULT_SOURCE_PRIORITY = {
    "organizer": 0,
    "ticketer": 1,
    "operator": 2,
    "hotel": 3,
    "transit": 3,
    "catering": 3,
    "attraction": 3,
}

DEFAULT_MIN_COHORTS = 3
DEFAULT_MIN_SAMPLE = 10


@dataclass(frozen=True)
class BucketRow:
    start: datetime
    end: datetime
    point: float
    ci_low: float | None
    ci_high: float | None
    cohort_count: int
    cohort_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class AggregationResult:
    metric: str
    region_code: str
    window_start: datetime
    window_end: datetime
    unit: str
    point: float
    ci_low: float | None
    ci_high: float | None
    cohort_count: int
    sample_size: int | None
    contributor_count: int
    suppressed: bool
    as_of: datetime
    basis: str
    bypassed_threshold: bool = False
    buckets: tuple[BucketRow, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = {
            "metric": self.metric,
            "region_code": self.region_code,
            "window_start": to_iso(self.window_start),
            "window_end": to_iso(self.window_end),
            "unit": self.unit,
            "point": _round(self.point),
            "ci_low": None if self.ci_low is None else _round(self.ci_low),
            "ci_high": None if self.ci_high is None else _round(self.ci_high),
            "cohort_count": self.cohort_count,
            "sample_size": self.sample_size,
            "contributor_count": self.contributor_count,
            "suppressed": self.suppressed,
            "basis": self.basis,
            "as_of": to_iso(self.as_of),
            "bypassed_threshold": self.bypassed_threshold,
            "buckets": [
                {
                    "start": to_iso(b.start),
                    "end": to_iso(b.end),
                    "point": _round(b.point),
                    "ci_low": None if b.ci_low is None else _round(b.ci_low),
                    "ci_high": None if b.ci_high is None else _round(b.ci_high),
                    "cohort_count": b.cohort_count,
                }
                for b in self.buckets
            ],
        }
        return d


def _round(x: float) -> float:
    return round(x + 0.0, 3)


class AggregationService:
    def __init__(self, ledger: SignalLedger, parties: PartyDirectory,
                 regions: RegionTree, metrics: MetricRegistry, *,
                 source_priority: dict[str, int] | None = None,
                 min_cohorts: int = DEFAULT_MIN_COHORTS,
                 min_sample: int = DEFAULT_MIN_SAMPLE) -> None:
        self.ledger = ledger
        self.parties = parties
        self.regions = regions
        self.metrics = metrics
        self.source_priority = source_priority or dict(DEFAULT_SOURCE_PRIORITY)
        self.min_cohorts = min_cohorts
        self.min_sample = min_sample

    # ---- 可见信号筛选 ----------------------------------------------------

    def visible_signals(self, viewer: Party, *, as_of: datetime,
                        basis: str = BASIS_FORECAST) -> list[SignalVersion]:
        """``as_of`` 时刻 viewer 有权看到、且当时为最新的有效版本。

        撤回版本与对方私货（private）不进入行业聚合；权限按 ``as_of``
        时点重放授权（商户被移出行业组后即看不到新聚合）。
        """

        out: list[SignalVersion] = []
        for ver in self.ledger.current_versions(as_of=as_of):
            if ver.status in (STATUS_WITHDRAWN, STATUS_MIGRATED):
                continue
            if ver.basis != basis:
                continue
            publisher = self.parties.get(ver.publisher_id)
            if self.parties.can_see(
                viewer,
                publisher=publisher,
                visibility=ver.visibility,
                audience=ver.audience,
                industry_sectors=ver.sectors,
                moment=as_of,
            ):
                out.append(ver)
        return out

    # ---- 聚合主入口 ------------------------------------------------------

    def aggregate(self, viewer: Party, *, metric: str, region_code: str,
                  window_start: datetime, window_end: datetime,
                  as_of: datetime, basis: str = BASIS_FORECAST,
                  hourly: bool = False) -> AggregationResult:
        candidates = [
            v for v in self.visible_signals(viewer, as_of=as_of, basis=basis)
            if v.metric == metric
            and self.regions.covers(v.region_code, region_code)
            and overlap_seconds(v.window_start, v.window_end,
                                window_start, window_end) > 0
        ]
        unit = self.metrics.standard_unit(metric)

        rows: list[BucketRow] = []
        split = hourly_buckets(window_start, window_end) if hourly else [
            (window_start, window_end)
        ]
        for b_start, b_end in split:
            rows.append(self._allocate_bucket(metric, candidates, b_start, b_end))

        point = sum(r.point for r in rows)
        ci_low = None if any(r.ci_low is None for r in rows) else sum(r.ci_low for r in rows)
        ci_high = None if any(r.ci_high is None for r in rows) else sum(r.ci_high for r in rows)
        cohort_keys = {c for r in rows for c in r.cohort_keys}

        # 样本量按 cohort 取该批次最大样本数后相加：同 cohort 的多条信号是
        # 同一批数据的不同渠道转述，样本不能重复相加。
        per_cohort_sample: dict[str, int | None] = {}
        for ver in candidates:
            key = ver.cohort_key or f"sid:{ver.signal_id}"
            if key not in cohort_keys:
                continue
            if ver.sample_size is None or per_cohort_sample.get(key) is None:
                per_cohort_sample[key] = None
            else:
                per_cohort_sample[key] = max(per_cohort_sample.get(key, 0),
                                             ver.sample_size)
        sample_size = (None if any(v is None for v in per_cohort_sample.values())
                       else sum(per_cohort_sample.values()))  # type: ignore[arg-type]

        suppressed = (len(cohort_keys) < self.min_cohorts
                      or (sample_size is not None and sample_size < self.min_sample))
        bypassed = False
        if suppressed:
            # 特许放行只在“确实存在可见 cohort、只是凑不满阈值”时有意义：
            # 零可见信号（如全是他人 private）时没有任何可放行内容，仍抑制，
            # 避免值班席借绕过能力反推出私有信号是否存在。
            if (viewer.can_bypass_threshold and viewer.role == ROLE_OPERATOR
                    and cohort_keys):
                bypassed = True  # 特许放行，由调用方写审计
            else:
                raise SuppressedAggregate(
                    cohort_count=len(cohort_keys), min_cohorts=self.min_cohorts
                )

        return AggregationResult(
            metric=metric,
            region_code=region_code,
            window_start=window_start,
            window_end=window_end,
            unit=unit,
            point=point,
            ci_low=ci_low,
            ci_high=ci_high,
            cohort_count=len(cohort_keys),
            sample_size=sample_size,
            contributor_count=len(candidates),
            suppressed=False,
            as_of=as_of,
            basis=basis,
            bypassed_threshold=bypassed,
            buckets=tuple(rows) if hourly else (),
        )

    # ---- 单时间桶：cohort 去重的逐秒裁决 --------------------------------

    def _allocate_bucket(self, metric: str, candidates: list[SignalVersion],
                         b_start: datetime, b_end: datetime) -> BucketRow:
        alloc = allocate_cohorts(
            candidates,
            cohort_of=lambda v: v.cohort_key or f"sid:{v.signal_id}",
            window_of=lambda v: (v.window_start, v.window_end),
            value_of=lambda v: v.estimate,
            ci_of=lambda v: (v.ci_low, v.ci_high),
            normalize=lambda v: self.metrics.normalize(metric, 1.0, v.unit),
            priority_key=self._priority_key,
            b_start=b_start, b_end=b_end,
        )
        return BucketRow(
            start=b_start, end=b_end, point=alloc.point,
            ci_low=alloc.ci_low if alloc.have_ci else None,
            ci_high=alloc.ci_high if alloc.have_ci else None,
            cohort_count=len(alloc.cohorts),
            cohort_keys=tuple(alloc.cohorts),
        )

    def _priority_key(self, ver: SignalVersion) -> tuple[int, int, int]:
        role = self.parties.get(ver.publisher_id).role
        # 角色优先级；同角色样本量大者优先；再以修订新者优先。
        sample = -(ver.sample_size or 0)
        return (self.source_priority.get(role, 9), sample, -ver.revision)


@dataclass(frozen=True)
class Allocation:
    point: float
    ci_low: float
    ci_high: float
    have_ci: bool
    cohorts: tuple[str, ...]


def allocate_cohorts(items: list, *,
                     cohort_of,
                     window_of,
                     value_of,
                     ci_of,
                     normalize,
                     priority_key,
                     b_start: datetime, b_end: datetime) -> Allocation:
    """跨 cohort 的逐秒分配通用原语。

    同一 cohort 的多个条目按 ``priority_key`` 升序裁决：高优先级先占用时间片，
    低优先级仅计入未被覆盖的秒数；不同 cohort 直接相加。
    ``value_of(item)`` / ``ci_of(item)`` 给未归一化数值与 (low, high)，
    ``normalize(item)`` 给该条目单位到标准单位的系数。
    """

    groups: dict[str, list] = {}
    for item in items:
        w_start, w_end = window_of(item)
        if overlap_seconds(w_start, w_end, b_start, b_end) <= 0:
            continue
        groups.setdefault(cohort_of(item), []).append(item)

    point = ci_low = ci_high = 0.0
    have_ci = True
    present: list[str] = []
    for cohort, members in groups.items():
        ordered = sorted(members, key=priority_key)
        claimed: list[tuple[datetime, datetime]] = []
        c_point = c_low = c_high = 0.0
        c_has_ci = False
        for item in ordered:
            w_start, w_end = window_of(item)
            seg_start = max(w_start, b_start)
            seg_end = min(w_end, b_end)
            remaining = _subtract_intervals(seg_start, seg_end, claimed)
            if not remaining:
                continue
            claimed.extend(remaining)
            factor = normalize(item)
            share = sum((e - s).total_seconds() for s, e in remaining) / \
                window_seconds(w_start, w_end)
            c_point += value_of(item) * factor * share
            low, high = ci_of(item)
            if low is not None and high is not None:
                c_has_ci = True
                c_low += low * factor * share
                c_high += high * factor * share
        point += c_point
        if c_has_ci:
            ci_low += c_low
            ci_high += c_high
        else:
            have_ci = False
        present.append(cohort)
    return Allocation(point, ci_low, ci_high, have_ci, tuple(present))


def _subtract_intervals(start: datetime, end: datetime,
                        claimed: list[tuple[datetime, datetime]]
                        ) -> list[tuple[datetime, datetime]]:
    """返回 [start, end) 去掉已占用区间后的剩余片段。"""

    fragments = [(start, end)]
    for c_start, c_end in claimed:
        nxt: list[tuple[datetime, datetime]] = []
        for f_start, f_end in fragments:
            if c_end <= f_start or c_start >= f_end:
                nxt.append((f_start, f_end))
                continue
            if c_start > f_start:
                nxt.append((f_start, min(c_start, f_end)))
            if c_end < f_end:
                nxt.append((max(c_end, f_start), f_end))
        fragments = nxt
    return [(s, e) for s, e in fragments if e > s]
