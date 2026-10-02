"""指标与计量单位注册表。

“酒店报了 500、地铁报了 500”不能相加，因为量纲不同；同一指标也可能用
不同单位报送（人次 vs 千人次）。注册表登记每个指标的标准单位、是否可加，
以及各报送单位到标准单位的换算系数，聚合前先归一，避免量纲错误。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    standard_unit: str
    additive: bool = True
    units: tuple[str, ...] = ()

    def unit_factor(self, unit: str) -> float:
        """报送单位 -> 标准单位的换算系数。"""

        factors = _UNIT_FACTORIES.get(self.standard_unit, {})
        if unit not in self.units:
            raise ValidationError(
                f"指标 {self.key} 不接受单位 {unit}",
                details={"allowed": list(self.units)},
            )
        return float(factors.get(unit, 1.0))


# 每个标准单位体系下的换算（非标准单位 -> 乘系数）。
_UNIT_FACTORIES: dict[str, dict[str, float]] = {
    "person": {"person": 1.0, "kperson": 1000.0},
    "room_night": {"room_night": 1.0},
    "seat": {"seat": 1.0},
    "cover": {"cover": 1.0},
    "visit": {"visit": 1.0},
    "shift": {"shift": 1.0},
}

_BUILTIN: tuple[Metric, ...] = (
    Metric("attendance", "观赛人次", "person", True, ("person", "kperson")),
    Metric("hotel_demand", "酒店需求（房晚）", "room_night", True, ("room_night",)),
    Metric("transit_demand", "交通出行需求（人次）", "person", True, ("person", "kperson")),
    Metric("transit_capacity", "交通运力（座位）", "seat", True, ("seat",)),
    Metric("catering_demand", "餐饮需求（餐次/桌餐）", "cover", True, ("cover",)),
    Metric("attraction_demand", "景区到访（人次）", "person", True, ("person", "kperson")),
    Metric("labor_supply", "加班/排班（班次）", "shift", False, ("shift",)),
)


class MetricRegistry:
    def __init__(self, metrics: tuple[Metric, ...] = _BUILTIN) -> None:
        self._metrics = {m.key: m for m in metrics}

    def register(self, metric: Metric) -> None:
        if metric.key in self._metrics:
            raise ValidationError(f"指标已存在: {metric.key}")
        if metric.standard_unit not in _UNIT_FACTORIES and not metric.units:
            raise ValidationError(f"指标 {metric.key} 缺少可用单位")
        self._metrics[metric.key] = metric

    def get(self, key: str) -> Metric:
        try:
            return self._metrics[key]
        except KeyError as exc:
            raise ValidationError(
                f"未知指标: {key}", details={"known": sorted(self._metrics)}
            ) from exc

    def normalize(self, key: str, value: float, unit: str) -> float:
        """把按 ``unit`` 报送的数值换算为标准单位。"""

        metric = self.get(key)
        return value * metric.unit_factor(unit)

    def standard_unit(self, key: str) -> str:
        return self.get(key).standard_unit

    def is_additive(self, key: str) -> bool:
        return self.get(key).additive
