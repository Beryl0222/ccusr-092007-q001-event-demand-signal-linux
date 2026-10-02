"""审计访问日志。

谁在何时以什么身份查了什么聚合、是否越过隐私阈值、结果是否被抑制，
全部追加落盘。聚合结果本身不含单条订单，但“谁能看到稀疏聚合”本身
是敏感能力，必须可追责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutils import to_iso


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor: str
    action: str
    target: str
    granted: bool
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": to_iso(self.at),
            "actor": self.actor,
            "action": self.action,
            "target": self.target,
            "granted": self.granted,
            "detail": self.detail,
        }


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self._entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False,
                                    sort_keys=True) + "\n")

    def entries(self) -> list[AuditEntry]:
        return list(self._entries)
