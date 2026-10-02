"""v1 样例合同 -> v2 信号合同的迁移。

``fixtures/demand_signal.json`` 是脱敏的 v1 样例，只携带标识与时间：
``schema_version / record_id / domain / occurred_at / revision / source``。
v2 保留这些字段的全部语义（record_id 即 signal_id，source 即 publisher_id，
occurred_at 仍为业务时间），并要求补齐时间窗、指标、地域、估计量等业务字段。

迁移由发布方在导入时补齐这些字段；v1 的 revision 原样保留，``note`` 标注
来源，保证“这是迁来的号”可追溯。纯标识信息则先落成 ``migrated`` 信封，
补齐前不得进入聚合。
"""

from __future__ import annotations

from typing import Any

from .contracts import DomainRecord
from .errors import ValidationError
from .models import (
    SignalVersion,
    STATUS_ACTIVE,
    VIS_OPEN,
    BASIS_FORECAST,
)
from .timeutils import parse_iso

REQUIRED_V2_FIELDS = (
    "purpose", "metric", "region_code",
    "window_end", "estimate", "unit",
)


def migrate_legacy(record: DomainRecord, *, fields: dict[str, Any]) -> SignalVersion:
    """把 v1 样例记录连同发布方补齐字段迁成首个 v2 版本。

    ``occurred_at`` 沿用 v1；``window_start`` 缺省取 v1 的 ``occurred_at``。
    """

    if record.schema_version != 1:
        raise ValidationError(f"仅支持从 schema_version=1 迁移，得到 {record.schema_version}")
    if record.domain != "event_signal":
        raise ValidationError(f"域不匹配: {record.domain}")
    missing = [k for k in REQUIRED_V2_FIELDS if k not in fields]
    if missing:
        raise ValidationError(f"迁移缺少 v2 必填字段: {missing}")

    occurred = parse_iso(record.occurred_at)
    window_start = parse_iso(fields.get("window_start", record.occurred_at))
    window_end = parse_iso(fields["window_end"])
    if window_end <= window_start:
        raise ValidationError("迁移记录的时间窗结束必须晚于开始（跨午夜请直接给绝对时间）")

    return SignalVersion(
        signal_id=record.record_id,
        # 迁入即 v2 首版 r1（版本链必须连续，不凭空缺号）；
        # 原 v1 版本号记入 note 可追溯。
        revision=1,
        publisher_id=fields.get("publisher_id", record.source),
        purpose=fields["purpose"],
        metric=fields["metric"],
        region_code=fields["region_code"],
        window_start=window_start,
        window_end=window_end,
        estimate=float(fields["estimate"]),
        unit=fields["unit"],
        ci_low=fields.get("ci_low"),
        ci_high=fields.get("ci_high"),
        confidence=fields.get("confidence"),
        sample_size=fields.get("sample_size"),
        cohort_key=fields.get("cohort_key"),
        source_ref=fields.get("source_ref", f"v1:{record.record_id}"),
        visibility=fields.get("visibility", VIS_OPEN),
        audience=tuple(fields.get("audience", ())),
        sectors=tuple(fields.get("sectors", ())),
        basis=fields.get("basis", BASIS_FORECAST),
        status=fields.get("status", STATUS_ACTIVE),
        note=fields.get(
            "note",
            f"自 v1 合同迁移（原 source={record.source}，原 revision={record.revision}）"),
        occurred_at=occurred,
    )
