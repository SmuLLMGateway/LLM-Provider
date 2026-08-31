"""요청이 선택한 LLM Deployment로 Option A 생성을 수행합니다."""

from __future__ import annotations

from app.backends.provider_registry import BackendProviderRegistry
from app.registry.deployment_resolver import DeploymentResolver
from app.schemas.generation import LlmResult, PreviousTextMessage
from app.services.llm_result_validator import (
    LlmResultValidationError,
    validate_llm_result,
)
from app.services.registry_snapshot_provider import RegistrySnapshotProvider


class GenerationBackendResultError(TypeError):
    """LLM Backend가 공통 생성 결과 계약을 지키지 않으면 발생합니다."""

    def __init__(
        self,
        *,
        deployment_id: str,
        actual_type: type[object],
    ) -> None:
        self.deployment_id = deployment_id
        self.actual_type = actual_type
        super().__init__(
            "LLM Backend는 LlmResult를 반환해야 합니다: "
            f"deployment={deployment_id}, "
            f"actual={actual_type.__name__}"
        )


class GenerationPipeline:
    """Deployment 해석부터 사용자 입력을 이용한 LLM 호출까지 조립합니다."""

    def __init__(
        self,
        *,
        registry_manager: RegistrySnapshotProvider,
        deployment_resolver: DeploymentResolver,
        backend_providers: BackendProviderRegistry,
    ) -> None:
        self._registry_manager = registry_manager
        self._deployment_resolver = deployment_resolver
        self._backend_providers = backend_providers

    async def generate(
        self,
        *,
        text: str,
        llm_deployment_id: str,
        previous_text: tuple[PreviousTextMessage, ...] = (),
    ) -> LlmResult:
        """현재 입력과 순서가 보존된 이전 응답을 로컬 LLM으로 생성합니다."""

        snapshot = self._registry_manager.capture()
        plan = self._deployment_resolver.resolve_generation(
            llm_deployment_id=llm_deployment_id,
            snapshot=snapshot,
        )

        deployment = plan.llm_deployment
        backend = self._backend_providers.require_llm(
            deployment_id=deployment.id,
            deployment=deployment.config,
        )
        messages: list[dict[str, object]] = []
        for previous_item in previous_text:
            messages.append(
                {
                    "role": previous_item.role,
                    "content": previous_item.content,
                }
            )
        messages.append(
            {
                "role": "user",
                "content": text,
            }
        )
        result = await backend.generate(
            messages=messages,
            deployment=deployment.config,
            parameters={},
            output_schema=None,
        )

        try:
            return validate_llm_result(result)
        except LlmResultValidationError as error:
            raise GenerationBackendResultError(
                deployment_id=deployment.id,
                actual_type=error.actual_type,
            ) from None
__all__ = [
    "GenerationBackendResultError",
    "GenerationPipeline",
    "RegistrySnapshotProvider",
]
