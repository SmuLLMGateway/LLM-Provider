"""모델 서버 없이 결정적인 결과를 반환하는 Mock LLM Backend입니다."""

from __future__ import annotations

from app.backends.errors import BackendConfigurationError
from app.backends.llm.base import (
    LlmMessages,
    LlmOutputSchema,
    LlmParameters,
)
from app.backends.llm.input_validation import (
    LlmInputValidationError,
    normalize_llm_inputs,
)
from app.backends.validation import (
    BackendDeploymentValidationError,
    validate_runnable_deployment,
)
from app.schemas.generation import LlmResult
from app.schemas.registry import DeploymentConfig


_ADAPTER_TYPE = "mock"
DEFAULT_MOCK_LLM_RESULT = LlmResult(
    text='{"candidateDecisions":[],"newDetections":[]}',
    model_name="mock-llm",
    finish_reason="stop",
)


class MockLlmConfigurationError(BackendConfigurationError):
    """Mock LLM의 Deployment 또는 호출 인자가 잘못되면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("MOCK_LLM_CONFIG_INVALID", detail)


class MockLlmBackend:
    """요청 원문을 저장하지 않고 미리 정한 불변 결과를 반환합니다."""

    def __init__(self, result: LlmResult | None = None) -> None:
        if result is not None and not isinstance(result, LlmResult):
            raise TypeError("result는 LlmResult이거나 None이어야 합니다")
        self._result = (
            result if result is not None else DEFAULT_MOCK_LLM_RESULT
        )

    @property
    def result(self) -> LlmResult:
        """모든 호출에 반환할 불변 결과를 제공합니다."""

        return self._result

    async def generate(
        self,
        messages: LlmMessages,
        deployment: DeploymentConfig,
        parameters: LlmParameters,
        output_schema: LlmOutputSchema | None = None,
    ) -> LlmResult:
        """공통 입력 계약을 검증하고 설정된 결과를 그대로 반환합니다."""

        validation_error_detail: str | None = None
        try:
            validate_runnable_deployment(
                deployment,
                expected_kind="llm",
                expected_adapter_type=_ADAPTER_TYPE,
            )
            normalize_llm_inputs(
                messages,
                parameters,
                output_schema,
            )
        except (
            BackendDeploymentValidationError,
            LlmInputValidationError,
        ) as error:
            validation_error_detail = str(error)

        if validation_error_detail is not None:
            raise MockLlmConfigurationError(validation_error_detail)

        return self._result


__all__ = [
    "DEFAULT_MOCK_LLM_RESULT",
    "MockLlmBackend",
    "MockLlmConfigurationError",
]
