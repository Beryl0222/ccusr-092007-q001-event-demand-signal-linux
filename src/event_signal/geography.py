"""地域粒度与商圈层级。

信号按 ``region`` 发布，层级固定为
``city / district / business_circle / venue``。低粒度信号可以向上汇总
（多个 venue 属于同一商圈），反之绝不向下拆分——没有数据支持的拆分本身
就会泄露单个商户/订单。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError

CITY = "city"
DISTRICT = "district"
BUSINESS_CIRCLE = "business_circle"
VENUE = "venue"

LEVEL_ORDER = (CITY, DISTRICT, BUSINESS_CIRCLE, VENUE)
_LEVEL_RANK = {name: i for i, name in enumerate(LEVEL_ORDER)}


@dataclass(frozen=True)
class Region:
    code: str
    name: str
    level: str
    parent: str | None = None

    def __post_init__(self) -> None:
        if self.level not in _LEVEL_RANK:
            raise ValidationError(
                f"未知地域粒度: {self.level}", details={"allowed": list(LEVEL_ORDER)}
            )


class RegionTree:
    """登记地域及父子关系，回答“某区域包含哪些下级区域”。"""

    def __init__(self) -> None:
        self._regions: dict[str, Region] = {}

    def add(self, region: Region) -> Region:
        if region.code in self._regions:
            raise ValidationError(f"地域编码已存在: {region.code}")
        if region.parent is not None and region.parent not in self._regions:
            # 允许先子后父批量导入时的宽容：父稍后必须补齐，取用时再校验。
            pass
        self._regions[region.code] = region
        return region

    def get(self, code: str) -> Region:
        try:
            return self._regions[code]
        except KeyError as exc:
            raise ValidationError(f"未知地域: {code}") from exc

    def exists(self, code: str) -> bool:
        return code in self._regions

    def is_within(self, child_code: str, ancestor_code: str) -> bool:
        """``child_code`` 是否就是 ``ancestor_code`` 或位于其下级。"""

        if child_code == ancestor_code:
            return True
        cur = self._regions.get(child_code)
        seen: set[str] = set()
        while cur is not None and cur.code not in seen:
            if cur.parent == ancestor_code:
                return True
            seen.add(cur.code)
            cur = self._regions.get(cur.parent) if cur.parent else None
        return False

    def descendants(self, code: str) -> list[str]:
        """所有直接/间接下级（不含自身）。"""

        out: list[str] = []
        for other in self._regions:
            if other != code and self.is_within(other, code):
                out.append(other)
        return out

    def covers(self, signal_region: str, query_region: str) -> bool:
        """信号地域是否能服务于对 ``query_region`` 的统计。

        信号粒度更粗（如 city 级）时不能拿来冒充商圈级；信号落在查询区域
        内部或与其相同则可计入。
        """

        if signal_region == query_region:
            return True
        return self.is_within(signal_region, query_region)

    def rank(self, level: str) -> int:
        return _LEVEL_RANK[level]

    def common_rollup(self, codes: list[str]) -> str | None:
        """一组地域编码可安全汇总到的最细公共祖先（含全相同情形）。"""

        if not codes:
            return None
        chains = []
        for code in codes:
            chain = [code]
            cur = self._regions.get(code)
            while cur and cur.parent:
                chain.append(cur.parent)
                cur = self._regions.get(cur.parent)
            chains.append(chain)
        common = set(chains[0])
        for chain in chains[1:]:
            common &= set(chain)
        if not common:
            return None
        # 选链条中最靠下级（最深）的公共节点
        return min(common, key=lambda c: len(self._regions) - self._depth(c))

    def _depth(self, code: str) -> int:
        depth, cur = 0, self._regions.get(code)
        while cur and cur.parent and (parent := self._regions.get(cur.parent)):
            depth += 1
            cur = parent
        return depth
