"""서로 다른 Backend 구현체가 공유하는 실행 오류 계약입니다."""

from __future__ import annotations


class BackendError(RuntimeError):
    """Backend 실행 실패를 Adapter 독립적인 형태로 표현합니다."""

    def __init__(self, code: str, detail: str) -> None:
        if type(code) is not str or not code:
            raise ValueError("Backend 오류 code는 비어 있지 않은 문자열이어야 합니다")
        if type(detail) is not str or not detail:
            raise ValueError("Backend 오류 detail은 비어 있지 않은 문자열이어야 합니다")

        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class BackendConfigurationError(BackendError):
    """Deployment 또는 Backend 호출 설정이 실행 계약과 다릅니다."""


class BackendTimeoutError(BackendError):
    """Backend 호출이 설정된 제한시간 안에 끝나지 않았습니다."""


class BackendTransportError(BackendError):
    """네트워크 등의 문제로 Backend에 요청을 전달하지 못했습니다."""


class BackendResponseError(BackendError):
    """Backend 응답 상태나 본문을 공통 결과로 변환할 수 없습니다."""


class BackendInputTooLargeError(BackendError):
    """Backend가 요청 원문 전체를 안전하게 처리할 수 없습니다."""


__all__ = [
    "BackendConfigurationError",
    "BackendError",
    "BackendInputTooLargeError",
    "BackendResponseError",
    "BackendTimeoutError",
    "BackendTransportError",
]
