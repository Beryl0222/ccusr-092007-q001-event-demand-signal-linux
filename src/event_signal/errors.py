"""领域错误类型。

所有错误都带稳定 ``code``，供接口层映射为 HTTP 状态码，
也便于参与方按编号处理而不是解析中文文案。
"""

from __future__ import annotations


class SignalError(Exception):
    """全部业务异常的基类。"""

    code = "signal_error"
    http_status = 400

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.__class__.__doc__ or self.code)
        self.message = message or self.__class__.__doc__ or self.code
        self.details = details or {}


class NotFound(SignalError):
    """对象不存在或对当前接收方不可见。"""

    code = "not_found"
    http_status = 404


class ValidationError(SignalError):
    """载荷未通过合同校验。"""

    code = "validation_error"
    http_status = 422


class ConflictError(SignalError):
    """并发/版本冲突，例如依据了已失效版本。"""

    code = "conflict"
    http_status = 409


class DuplicateSubmission(ConflictError):
    """同源同引用已入库，返回既有记录而非重复计数。"""

    code = "duplicate_submission"

    def __init__(self, existing_id: str, *, existing_revision: int) -> None:
        super().__init__(
            "同源数据已存在",
            details={"existing_id": existing_id, "existing_revision": existing_revision},
        )
        self.existing_id = existing_id


class StaleRevision(ConflictError):
    """修订/撤回基于的 revision 不是最新版本。"""

    code = "stale_revision"


class PermissionDenied(SignalError):
    """参与方无权查看该信号或该粒度的数据。"""

    code = "permission_denied"
    http_status = 403


class SuppressedAggregate(SignalError):
    """可见样本不足隐私阈值，聚合结果被抑制。"""

    code = "suppressed_aggregate"
    http_status = 409

    def __init__(self, *, cohort_count: int, min_cohorts: int) -> None:
        super().__init__(
            "聚合基数不足，结果已抑制",
            details={"cohort_count": cohort_count, "min_cohorts": min_cohorts},
        )
        self.cohort_count = cohort_count
        self.min_cohorts = min_cohorts


class WindowError(ValidationError):
    """时间窗非法（结束不晚于开始、跨日场次无法解析等）。"""

    code = "window_error"


class PolicyError(SignalError):
    """可见级别或权限状态不允许该操作。"""

    code = "policy_error"
    http_status = 403
