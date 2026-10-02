"""赛事需求信号协作后端。

模块划分：

* :mod:`event_signal.contracts` —— v1 最小合同（fixtures 样例读取）；
* :mod:`event_signal.models` / :mod:`event_signal.timeutils` —— 信号、决定、
  实绩的不可变模型与跨午夜时间窗；
* :mod:`event_signal.store` —— 只追加版本链与双时态 as-of；
* :mod:`event_signal.parties` / :mod:`event_signal.geography` /
  :mod:`event_signal.metrics` —— 授权、地域、计量；
* :mod:`event_signal.aggregation` —— 同源去重与隐私安全聚合；
* :mod:`event_signal.evaluation` —— 误差、损失与决策链重建；
* :mod:`event_signal.service` / :mod:`event_signal.wsgi` —— 应用门面与 HTTP。
"""

from .contracts import DomainRecord, load_record
from .errors import (
    ConflictError,
    DuplicateSubmission,
    NotFound,
    PermissionDenied,
    SignalError,
    StaleRevision,
    SuppressedAggregate,
    ValidationError,
)
from .geography import RegionTree
from .metrics import MetricRegistry
from .parties import Party, PartyDirectory
from .service import DemandSignalService
from .store import AppendLog, SignalLedger

__all__ = [
    "DomainRecord",
    "load_record",
    "SignalError",
    "NotFound",
    "ValidationError",
    "ConflictError",
    "DuplicateSubmission",
    "StaleRevision",
    "PermissionDenied",
    "SuppressedAggregate",
    "RegionTree",
    "MetricRegistry",
    "Party",
    "PartyDirectory",
    "AppendLog",
    "SignalLedger",
    "DemandSignalService",
]
