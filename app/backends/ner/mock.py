"""개발과 Pipeline 조립 테스트에 사용하는 빈 NER Backend입니다."""

from __future__ import annotations

from app.backends.errors import BackendConfigurationError
from app.backends.validation import (
    BackendDeploymentValidationError,
    validate_runnable_deployment,
)
from app.schemas.detection import Detection
from app.schemas.registry import DeploymentConfig


class MockNerConfigurationError(BackendConfigurationError):
    """Mock NER에 전달된 Deployment가 실행 계약과 다르면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("MOCK_NER_CONFIG_INVALID", detail)


class MockNerBackend:
    """입력 원문을 저장하지 않고 빈 Detection 목록을 반환합니다."""

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """Mock NER Deployment를 검증하고 빈 탐지 결과를 반환합니다."""

        if not isinstance(text, str):
            raise MockNerConfigurationError("text는 문자열이어야 합니다")
        try:
            validate_runnable_deployment(
                deployment,
                expected_kind="ner",
                expected_adapter_type="mock",
            )
        except BackendDeploymentValidationError as error:
            raise MockNerConfigurationError(str(error)) from None
        return []


__all__ = [
    "MockNerBackend",
    "MockNerConfigurationError",
]
