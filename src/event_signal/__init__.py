"""赛事需求信号协作后端。

在既有最小合同(contracts.DomainRecord)之上提供完整的信号生命周期、
供给登记、实绩回填、权限治理、聚合与审计能力。
"""

from .contracts import DomainRecord, load_record
from .errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .models import (
    DOMAIN,
    SCHEMA_VERSION,
    ActualRecord,
    Commitment,
    GeoLevel,
    GeoRef,
    GrantEvent,
    GrantKind,
    LifecycleStatus,
    Participant,
    Purpose,
    QuantityRange,
    Role,
    SignalRevision,
    Visibility,
)
from .service import DemandSignalService
from .timeutil import Window, format_instant, parse_instant

__all__ = [
    "DOMAIN",
    "SCHEMA_VERSION",
    "ActualRecord",
    "Commitment",
    "ConflictError",
    "DemandSignalService",
    "DomainError",
    "DomainRecord",
    "GeoLevel",
    "GeoRef",
    "GrantEvent",
    "GrantKind",
    "LifecycleStatus",
    "NotFoundError",
    "Participant",
    "PermissionDenied",
    "Purpose",
    "QuantityRange",
    "Role",
    "SignalRevision",
    "ValidationError",
    "Visibility",
    "Window",
    "format_instant",
    "load_record",
    "parse_instant",
]
