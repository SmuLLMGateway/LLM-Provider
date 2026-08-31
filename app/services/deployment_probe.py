"""최소 실제 요청으로 NER·LLM Deployment의 실행 가능 여부를 검사합니다."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

from app.backends.provider_registry import BackendProviderRegistry
from app.policies.detection_result_validator import DetectionResultValidator
from app.registry.deployment_resolver import DeploymentResolutionError
from app.schemas.deployments import DeploymentProbeResponse
from app.schemas.registry import (
    DeploymentConfig,
    DeploymentKind,
)
from app.services.llm_result_validator import (
    LlmResultValidationError,
    validate_llm_result,
)
from app.services.registry_snapshot_provider import RegistrySnapshotProvider


NER_PROBE_TEXT = "A"
LLM_PROBE_TEXT = "A"
LLM_PROBE_MAX_TOKENS = 1
MonotonicClock = Callable[[], float]


class LlmProbeBackendResultError(TypeError):
    """LLM Backend가 Probe에서 공통 결과 계약을 지키지 않으면 발생합니다."""

    def __init__(
        self,
        *,
        deployment_id: str,
        actual_type: type[object],
    ) -> None:
        self.deployment_id = deployment_id
        self.actual_type = actual_type
        super().__init__(
            "LLM Probe Backend는 LlmResult를 반환해야 합니다: "
            f"deployment={deployment_id}, "
            f"actual={actual_type.__name__}"
        )


class DeploymentProbeService:
    """고정 최소 입력으로 Backend의 전체 요청·응답 계약을 점검합니다."""

    def __init__(
        self,
        *,
        registry_manager: RegistrySnapshotProvider,
        backend_providers: BackendProviderRegistry,
        result_validator: DetectionResultValidator | None = None,
        clock: MonotonicClock = perf_counter,
    ) -> None:
        self._registry_manager = registry_manager
        self._backend_providers = backend_providers
        self._result_validator = (
            result_validator
            if result_validator is not None
            else DetectionResultValidator()
        )
        self._clock = clock

    async def probe_ner(
        self,
        *,
        deployment_id: str,
    ) -> DeploymentProbeResponse:
        """현재 Snapshot의 NER Deployment에 최소 탐지 요청을 한 번 보냅니다."""

        configured = self._resolve_probe_config(
            deployment_id,
            expected_kind="ner",
        )
        backend = self._backend_providers.require_ner(
            deployment_id=deployment_id,
            deployment=configured,
        )
        probe_config = _as_probe_call_config(configured)

        started_at = self._clock()
        result = await backend.detect(
            NER_PROBE_TEXT,
            probe_config,
        )
        self._result_validator.validate(
            NER_PROBE_TEXT,
            result,
            expected_source="ner",
        )
        finished_at = self._clock()

        return DeploymentProbeResponse(
            deployment_id=deployment_id,
            status="available",
            latency_ms=_elapsed_milliseconds(
                started_at,
                finished_at,
            ),
        )

    async def probe_llm(
        self,
        *,
        deployment_id: str,
    ) -> DeploymentProbeResponse:
        """현재 Snapshot의 LLM Deployment에 최소 생성 요청을 한 번 보냅니다."""

        configured = self._resolve_probe_config(
            deployment_id,
            expected_kind="llm",
        )
        backend = self._backend_providers.require_llm(
            deployment_id=deployment_id,
            deployment=configured,
        )
        probe_config = _as_probe_call_config(configured)

        started_at = self._clock()
        result = await backend.generate(
            messages=[
                {
                    "role": "user",
                    "content": LLM_PROBE_TEXT,
                }
            ],
            deployment=probe_config,
            parameters={
                "max_tokens": LLM_PROBE_MAX_TOKENS,
            },
            output_schema=None,
        )
        try:
            validate_llm_result(result)
        except LlmResultValidationError as error:
            raise LlmProbeBackendResultError(
                deployment_id=deployment_id,
                actual_type=error.actual_type,
            ) from None
        finished_at = self._clock()

        return DeploymentProbeResponse(
            deployment_id=deployment_id,
            status="available",
            latency_ms=_elapsed_milliseconds(
                started_at,
                finished_at,
            ),
        )

    def _resolve_probe_config(
        self,
        deployment_id: str,
        *,
        expected_kind: DeploymentKind,
    ) -> DeploymentConfig:
        """Probe 대상은 존재 여부와 경로 종류만 검증해 조회합니다."""

        snapshot = self._registry_manager.capture()
        configured = snapshot.deployments.get(deployment_id)
        if configured is None or configured.kind != expected_kind:
            raise DeploymentResolutionError(
                "DEPLOYMENT_NOT_FOUND",
                deployment_id=deployment_id,
                expected_kind=expected_kind,
            )
        return configured


def _as_probe_call_config(
    deployment: DeploymentConfig,
) -> DeploymentConfig:
    """비활성 설정을 Snapshot 변경 없이 Probe 실행용으로 복사합니다."""

    if deployment.enabled:
        return deployment
    return deployment.model_copy(update={"enabled": True})


def _elapsed_milliseconds(
    started_at: float,
    finished_at: float,
) -> float:
    """단조 시계 차이를 음수가 아닌 밀리초 값으로 변환합니다."""

    return round(max(0.0, finished_at - started_at) * 1_000, 3)


__all__ = [
    "DeploymentProbeService",
    "LLM_PROBE_MAX_TOKENS",
    "LLM_PROBE_TEXT",
    "LlmProbeBackendResultError",
    "NER_PROBE_TEXT",
]
