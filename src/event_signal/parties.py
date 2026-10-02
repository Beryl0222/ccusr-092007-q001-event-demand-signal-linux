"""参与方与访问授权。

参与方（主办方、票务方、酒店/交通/餐饮/景区经营者、运营中心值班席）在协议中
是稳定身份。授权关系随时间变化——某商户本届赛事结束后被移出行业组，不能
影响它在赛时曾经可见这一事实：每条授权带生效/失效时间，查询按 as-of 还原，
而不是就地删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .errors import PolicyError, ValidationError
from .timeutils import parse_iso

#: 角色（协议用途，不直接等于可见级别）。
ROLE_ORGANIZER = "organizer"          # 主办方
ROLE_TICKETER = "ticketer"            # 票务方
ROLE_HOTEL = "hotel"
ROLE_TRANSIT = "transit"
ROLE_CATERING = "catering"
ROLE_ATTRACTION = "attraction"
ROLE_OPERATOR = "operator"            # 城市运营中心值班席

#: 行业分组，用于 industry 级信号。
SECTORS = (ROLE_HOTEL, ROLE_TRANSIT, ROLE_CATERING, ROLE_ATTRACTION)


@dataclass(frozen=True)
class Party:
    party_id: str
    name: str
    role: str
    #: 该方归属的行业组（票务/主办方可为空，集团型商户可属多个组）。
    sectors: tuple[str, ...] = ()
    #: 运营中心可越过聚合抑制阈值查看稀疏聚合（访问会被审计）。
    can_bypass_threshold: bool = False

    def __post_init__(self) -> None:
        valid = {ROLE_ORGANIZER, ROLE_TICKETER, ROLE_OPERATOR, *SECTORS}
        if self.role not in valid:
            raise ValidationError(f"未知角色: {self.role}", details={"allowed": sorted(valid)})
        bad = [s for s in self.sectors if s not in SECTORS]
        if bad:
            raise ValidationError(f"未知行业组: {bad}")


@dataclass(frozen=True)
class GrantEvent:
    """不可变授权事实。

    ``kind="grant"`` 开启一个区间，``kind="revoke"`` 在 ``at`` 时刻关闭
    同组当前仍开启的区间。重放事件即可还原任意时刻的授权状态，
    被撤销的历史区间原样可查。
    """

    kind: str             # "grant" | "revoke"
    grantee_id: str
    group: str
    at: datetime


class PartyDirectory:
    def __init__(self) -> None:
        self._parties: dict[str, Party] = {}
        # grantee -> list[GrantEvent]（只追加）
        self._events: dict[str, list[GrantEvent]] = {}

    def register(self, party: Party) -> Party:
        if party.party_id in self._parties:
            raise ValidationError(f"参与方已存在: {party.party_id}")
        self._parties[party.party_id] = party
        return party

    def get(self, party_id: str) -> Party:
        try:
            return self._parties[party_id]
        except KeyError as exc:
            raise ValidationError(f"未知参与方: {party_id}") from exc

    def exists(self, party_id: str) -> bool:
        return party_id in self._parties

    # ---- 授权变化（全部追加新事实） -------------------------------------

    def grant(self, grantee_id: str, group: str, *,
              effective_from: datetime | str) -> GrantEvent:
        self.get(grantee_id)
        ev = GrantEvent("grant", grantee_id, group, parse_iso(effective_from))
        self._events.setdefault(grantee_id, []).append(ev)
        return ev

    def revoke(self, grantee_id: str, group: str, *, at: datetime | str) -> GrantEvent:
        """关闭在 ``at`` 时刻仍开启的同组授权；历史区间保留。"""

        moment = parse_iso(at)
        active = self._active_groups(grantee_id, moment)
        if group not in active:
            raise PolicyError(f"没有可撤销的有效授权: {grantee_id} -> {group}")
        ev = GrantEvent("revoke", grantee_id, group, moment)
        self._events.setdefault(grantee_id, []).append(ev)
        return ev

    def _active_groups(self, grantee_id: str, moment: datetime) -> set[str]:
        """重放截至 ``moment`` 的事件，得到当时开启的组集合。"""

        open_groups: set[str] = set()
        for ev in self._events.get(grantee_id, []):
            if ev.at > moment:
                break
            if ev.kind == "grant":
                open_groups.add(ev.group)
            elif ev.kind == "revoke":
                open_groups.discard(ev.group)
        return open_groups

    def _suspended_sectors(self, grantee_id: str, moment: datetime) -> set[str]:
        """重放 suspend/restore 事件，得到当时被暂停的固有行业组。"""

        suspended: set[str] = set()
        for ev in self._events.get(grantee_id, []):
            if ev.at > moment:
                break
            if ev.kind == "suspend":
                suspended.add(ev.group.removeprefix("industry:"))
            elif ev.kind == "restore":
                suspended.discard(ev.group.removeprefix("industry:"))
        return suspended

    def suspend_sector(self, grantee_id: str, sector: str, *, at: datetime | str) -> GrantEvent:
        """暂停成员的固有行业组资格（如赛后被移出行业协作组）。历史可见性不变。"""

        party = self.get(grantee_id)
        if sector not in party.sectors:
            raise PolicyError(f"{grantee_id} 本就不属于行业组 industry:{sector}")
        moment = parse_iso(at)
        if sector in self._suspended_sectors(grantee_id, moment):
            raise PolicyError(f"{grantee_id} 的 industry:{sector} 已处于暂停状态")
        ev = GrantEvent("suspend", grantee_id, f"industry:{sector}", moment)
        self._events.setdefault(grantee_id, []).append(ev)
        return ev

    def restore_sector(self, grantee_id: str, sector: str, *, at: datetime | str) -> GrantEvent:
        moment = parse_iso(at)
        if sector not in self._suspended_sectors(grantee_id, moment):
            raise PolicyError(f"{grantee_id} 的 industry:{sector} 未被暂停")
        ev = GrantEvent("restore", grantee_id, f"industry:{sector}", parse_iso(at))
        self._events.setdefault(grantee_id, []).append(ev)
        return ev

    def groups_at(self, grantee_id: str, moment: datetime) -> set[str]:
        """该方在 ``moment`` 时刻所属的全部组（固有组扣除暂停，叠加授权）。"""

        party = self.get(grantee_id)
        suspended = self._suspended_sectors(grantee_id, moment)
        groups = {f"industry:{s}" for s in party.sectors if s not in suspended}
        groups |= self._active_groups(grantee_id, moment)
        return groups

    def can_see(self, viewer: Party, *, publisher: Party,
                visibility: str, audience: tuple[str, ...],
                industry_sectors: tuple[str, ...],
                moment: datetime) -> bool:
        """按信号的可见级别与 ``moment`` 时的授权状态判定可见性。

        - open：任何登记参与方可见；
        - industry：发布时指定行业组内的经营者可见；
        - parties：audience 白名单内的参与方可见；
        - private：仅发布方自己可见（运营中心也不可见明文）。
        """

        if visibility == "open":
            return True
        if viewer.party_id == publisher.party_id:
            return True
        if viewer.role == ROLE_OPERATOR and visibility != "private":
            # 运营中心为完成聚合值班可看非私有信号；私有信号只进阈值聚合。
            return True
        groups = self.groups_at(viewer.party_id, moment)
        if visibility == "industry":
            return any(f"industry:{s}" in groups for s in industry_sectors)
        if visibility == "parties":
            if viewer.party_id in audience:
                return True
            return any(f"signal:{sid}" in groups for sid in audience)
        if visibility == "private":
            return False
        raise PolicyError(f"未知可见级别: {visibility}")
