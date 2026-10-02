"""领域异常类型。"""

from __future__ import annotations


class DomainError(Exception):
    """领域错误基类。"""


class ValidationError(DomainError):
    """输入不合法(时间、数值、枚举、窗口等)。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class ConflictError(DomainError):
    """版本冲突:revision 重复、跳跃,或与既有版本内容不一致。"""


class PermissionDenied(DomainError):
    """可见级别或授权不允许该操作/查询。"""
