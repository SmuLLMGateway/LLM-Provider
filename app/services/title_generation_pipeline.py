"""고정 Prompt와 요청별 LLM Deployment로 대화 제목을 생성합니다."""

from __future__ import annotations

from app.backends.provider_registry import BackendProviderRegistry
from app.registry.deployment_resolver import DeploymentResolver
from app.schemas.title_generation import GenerateTitleResponse
from app.services.llm_result_validator import (
    LlmResultValidationError,
    validate_llm_result,
)
from app.services.registry_snapshot_provider import RegistrySnapshotProvider
from app.services.title_output_validator import TitleOutputValidator


TITLE_MAX_TOKENS = 32
TITLE_TEMPERATURE = 0
TITLE_REASONING_EFFORT = "none"


class TitleBackendResultError(TypeError):
    """LLM Backend가 공통 LlmResult 계약을 지키지 않으면 발생합니다."""

    def __init__(
        self,
        *,
        deployment_id: str,
        actual_type: type[object],
    ) -> None:
        self.deployment_id = deployment_id
        self.actual_type = actual_type
        super().__init__(
            "제목 생성 Backend는 LlmResult를 반환해야 합니다. "
            f"deployment={deployment_id}, "
            f"actual={actual_type.__name__}"
        )


class TitleGenerationPipeline:
    """제목 실행 계획 조립부터 출력 검증까지 순서대로 수행합니다."""

    def __init__(
        self,
        *,
        registry_manager: RegistrySnapshotProvider,
        deployment_resolver: DeploymentResolver,
        backend_providers: BackendProviderRegistry,
        output_validator: TitleOutputValidator | None = None,
    ) -> None:
        self._registry_manager = registry_manager
        self._deployment_resolver = deployment_resolver
        self._backend_providers = backend_providers
        self._output_validator = (
            output_validator
            if output_validator is not None
            else TitleOutputValidator()
        )

    async def generate_title(
        self,
        *,
        text: str,
        llm_deployment_id: str,
    ) -> GenerateTitleResponse:
        """같은 Snapshot의 LLM과 제목 Prompt로 한 줄 제목을 생성합니다."""

        snapshot = self._registry_manager.capture()
        plan = self._deployment_resolver.resolve_title_generation(
            llm_deployment_id=llm_deployment_id,
            snapshot=snapshot,
        )

        deployment = plan.llm_deployment
        backend = self._backend_providers.require_llm(
            deployment_id=deployment.id,
            deployment=deployment.config,
        )
        result = await backend.generate(
            messages=[
                {
                    "role": "system",
                    "content": plan.title_prompt.render(),
                },
                {
                    "role": "user",
                    "content": text,
                },
            ],
            deployment=deployment.config,
            parameters={
                "max_tokens": TITLE_MAX_TOKENS,
                "temperature": TITLE_TEMPERATURE,
                "reasoning_effort": TITLE_REASONING_EFFORT,
            },
            output_schema=None,
        )

        try:
            validated_result = validate_llm_result(result)
        except LlmResultValidationError as error:
            raise TitleBackendResultError(
                deployment_id=deployment.id,
                actual_type=error.actual_type,
            ) from None

        title = self._output_validator.validate(
            validated_result.text
        )
        return GenerateTitleResponse(title=title)


__all__ = [
    "TITLE_MAX_TOKENS",
    "TITLE_REASONING_EFFORT",
    "TITLE_TEMPERATURE",
    "TitleBackendResultError",
    "TitleGenerationPipeline",
]
