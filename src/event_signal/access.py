"""可见级别与授权判定(按 as-of 时刻求值,权限变化可追溯)。"""

from __future__ import annotations

from datetime import datetime

from .errors import PermissionDenied
from .models import GrantKind, Role, SignalRevision, Visibility
from .store import EventStore


def effective_grant(
    store: EventStore, signal_id: str, participant_id: str, at: datetime
) -> GrantKind | None:
    """at 时刻生效的最近一次授权事件;无事件返回 None。"""
    best = None
    for event in store.grant_events(signal_id=signal_id, participant_id=participant_id):
        if event.effective_at > at:
            continue
        if best is None or (event.effective_at, event.recorded_at) > (
            best.effective_at,
            best.recorded_at,
        ):
            best = event
    return best.kind if best is not None else None


def can_view(
    store: EventStore, viewer_id: str, revision: SignalRevision, at: datetime
) -> bool:
    """viewer 在 at 时刻是否可见该信号版本(按该版本的可见级别求值)。"""
    viewer = store.participants.get(viewer_id)
    if viewer is None:
        return False
    if viewer.role == Role.OPS_CENTER or revision.publisher_id == viewer_id:
        return True
    if revision.visibility in (Visibility.PUBLIC, Visibility.NETWORK):
        return True
    if revision.visibility == Visibility.RESTRICTED:
        if viewer_id in revision.allowlist:
            return True
        return (
            effective_grant(store, revision.signal_id, viewer_id, at) == GrantKind.GRANT
        )
    return False  # PRIVATE


def require_view(
    store: EventStore, viewer_id: str, revision: SignalRevision, at: datetime
) -> None:
    if not can_view(store, viewer_id, revision, at):
        raise PermissionDenied(
            f"{viewer_id} 无权查看信号 {revision.signal_id} (revision {revision.revision})"
        )
