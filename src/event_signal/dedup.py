"""同源数据去重与版本指纹。

同一来源 (source, record_id) 的数据通过内容指纹实现:
- 重试/重复上报:指纹一致 → 幂等返回既有版本,不重复计入;
- 同 revision 不同内容:拒绝(冲突),必须提升 revision;
- 更高 revision:形成新版本,旧版本保留用于 as-of 重建。

此外提供同源不同 record 的时间窗重叠检测,提示可能的重复计入
(跨记录是否重复只有来源方知道,系统只告警不擅自合并)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import combinations
from typing import Callable, Iterable, Mapping, TypeVar

from .errors import ConflictError
from .models import SignalRevision
from .timeutil import Window

T = TypeVar("T")


def _canon(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Window):
        return {"start": value.start.isoformat(), "end": value.end.isoformat()}
    raise TypeError(f"不可序列化: {type(value).__name__}")


def fingerprint_payload(payload: Mapping[str, object]) -> str:
    """业务内容的稳定指纹(不含 recorded_at 等系统字段)。"""
    canon = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_canon,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def append_version(
    versions: list[T],
    revision: int,
    fingerprint: str,
    build: Callable[[], T],
) -> tuple[T, bool]:
    """向版本链追加新版本;返回 (version, created)。

    - revision 与既有版本重复且指纹一致:幂等返回既有版本(created=False);
    - revision 重复但指纹不一致:冲突;
    - revision 必须等于 最新 revision + 1(首版本必须为 1)。
    """
    if versions:
        latest = versions[-1]
        if revision <= latest.revision:
            for existing in versions:
                if existing.revision == revision and existing.fingerprint == fingerprint:
                    return existing, False
            raise ConflictError(
                f"revision {revision} 已存在且内容不一致;请使用更大的 revision 提交修订"
            )
        if revision != latest.revision + 1:
            raise ConflictError(
                f"revision 必须连续:当前最新为 {latest.revision},收到 {revision}"
            )
    elif revision != 1:
        raise ConflictError("首个 revision 必须为 1")
    version = build()
    versions.append(version)
    return version, True


@dataclass(frozen=True)
class OverlapWarning:
    """同源不同 record 在同地域、同指标上的时间窗重叠,可能被重复计入。"""

    source: str
    metric: str
    geo_id: str
    record_ids: tuple[str, str]
    signal_ids: tuple[str, str]
    overlap: Window
    message: str


def find_same_source_overlaps(
    revisions: Iterable[SignalRevision],
) -> list[OverlapWarning]:
    """检测同源、同指标、同地域下不同 record 的时间窗重叠。"""
    groups: dict[tuple[str, str, str], dict[str, SignalRevision]] = {}
    for rev in revisions:
        key = (rev.source, rev.metric, rev.geo.geo_id)
        groups.setdefault(key, {}).setdefault(rev.record_id, rev)
    warnings: list[OverlapWarning] = []
    for (source, metric, geo_id), by_record in groups.items():
        for first, second in combinations(sorted(by_record), 2):
            rev_a = by_record[first]
            rev_b = by_record[second]
            overlap = rev_a.window.intersection(rev_b.window)
            if overlap is None:
                continue
            warnings.append(
                OverlapWarning(
                    source=source,
                    metric=metric,
                    geo_id=geo_id,
                    record_ids=(first, second),
                    signal_ids=(rev_a.signal_id, rev_b.signal_id),
                    overlap=overlap,
                    message=(
                        f"来源 {source!r} 的 {first!r} 与 {second!r} 在 {geo_id} "
                        f"上时间窗重叠,聚合时可能重复计入"
                    ),
                )
            )
    return warnings
