"""사용자에게 보여줄 수 있는 오류 유형."""

from __future__ import annotations


class Gps2CctvError(Exception):
    """사용자 입력·상태 문제로 작업을 진행할 수 없을 때."""

    exit_code = 2


class NotFoundError(Gps2CctvError):
    """요청한 사건·소스·카메라 등이 없을 때."""


class PolicyDeniedError(Gps2CctvError):
    """정책 검증을 통과하지 못해 작업을 거부했을 때."""

    exit_code = 3

    def __init__(self, message: str, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
