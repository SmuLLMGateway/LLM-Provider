"""고정 Prompt 기반 대화 제목 생성 파이프라인을 검증합니다."""

from __future__ import annotations

import pytest

from app.backends.provider_registry import (
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.registry.deployment_resolver import DeploymentResolver
from app.registry.execution_plan import TitleGenerationExecutionPlan
from app.registry.snapshot import ActiveRegistrySnapshot
from app.schemas.generation import LlmResult
from app.schemas.registry import DeploymentConfig, RegistryConfig
from app.schemas.title_generation import GenerateTitleResponse
from app.services.title_generation_pipeline import (
    TITLE_MAX_TOKENS,
    TITLE_REASONING_EFFORT,
    TITLE_TEMPERATURE,
    TitleBackendResultError,
    TitleGenerationPipeline,
)
from app.services.title_output_validator import (
    TitleOutputValidationError,
)


class RecordingSnapshotManager:
    """캡처한 Snapshot과 호출 횟수를 기록합니다."""

    def __init__(self, snapshot: ActiveRegistrySnapshot) -> None:
        self.snapshot = snapshot
        self.capture_calls = 0

    def capture(self) -> ActiveRegistrySnapshot:
        """동일한 활성 Snapshot을 반환합니다."""

        self.capture_calls += 1
        return self.snapshot


class RecordingTitleResolver(DeploymentResolver):
    """제목 실행 계획을 반환하며 선택 인자를 기록합니다."""

    def __init__(self, plan: TitleGenerationExecutionPlan) -> None:
        self.plan = plan
        self.calls: list[tuple[str, ActiveRegistrySnapshot]] = []

    def resolve_title_generation(
        self,
        *,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> TitleGenerationExecutionPlan:
        """Pipeline이 넘긴 Deployment ID와 Snapshot을 기록합니다."""

        self.calls.append((llm_deployment_id, snapshot))
        return self.plan


class RecordingLlmBackend:
    """LLM 공통 호출 인자와 예약된 결과 또는 오류를 보관합니다."""

    def __init__(
        self,
        result: object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[
            tuple[
                list[dict[str, object]],
                DeploymentConfig,
                dict[str, object],
                dict[str, object] | None,
            ]
        ] = []

    async def generate(
        self,
        messages: list[dict[str, object]],
        deployment: DeploymentConfig,
        parameters: dict[str, object],
        output_schema: dict[str, object] | None = None,
    ) -> object:
        """호출을 기록한 뒤 지정한 동작을 수행합니다."""

        self.calls.append(
            (messages, deployment, parameters, output_schema)
        )
        if self.error is not None:
            raise self.error
        return self.result


def _snapshot() -> ActiveRegistrySnapshot:
    """제목 생성용 LLM과 두 고정 Prompt가 있는 Snapshot을 만듭니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "llm-title": {
                    "kind": "llm",
                    "adapterType": "test_title_llm",
                    "baseUrl": "http://localhost:9000/v1",
                    "modelName": "title-model",
                    "timeoutMs": 5000,
                    "enabled": True,
                }
            }
        }
    )
    renderer = PromptRenderer()
    return ActiveRegistrySnapshot(
        snapshot_id="snapshot-title",
        deployments=registry.deployments,
        detection_prompt=PromptArtifact.compile(
            "{{ text }} / {{ existing_detections }}",
            renderer=renderer,
            template_path="<detection-test>",
        ),
        mask_prompt=MaskPromptArtifact.compile(
            "탐지 구간을 마스킹하십시오.",
            renderer=renderer,
            template_path="<mask-test>",
        ),
        title_prompt=TitlePromptArtifact.compile(
            "제목만 한 줄로 생성하십시오.",
            renderer=renderer,
            template_path="<title-test>",
        ),
    )


def _pipeline(
    backend: object,
    *,
    resolver: DeploymentResolver | None = None,
) -> tuple[TitleGenerationPipeline, RecordingSnapshotManager]:
    """실제 Provider Registry를 사용하는 제목 Pipeline을 만듭니다."""

    snapshot = _snapshot()
    manager = RecordingSnapshotManager(snapshot)
    providers = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="test_title_llm",
                provider=backend,
            )
        ]
    )
    return (
        TitleGenerationPipeline(
            registry_manager=manager,
            deployment_resolver=resolver or DeploymentResolver(),
            backend_providers=providers,
        ),
        manager,
    )


@pytest.mark.asyncio
async def test_generate_title_uses_fixed_system_prompt_and_raw_user_text() -> None:
    """고정 지시와 사용자 원문을 서로 다른 메시지로 전달합니다."""

    backend = RecordingLlmBackend(
        LlmResult(
            text="  FastAPI 제목 생성  ",
            model_name="title-model",
        )
    )
    pipeline, manager = _pipeline(backend)

    response = await pipeline.generate_title(
        text="FastAPI에 자동 제목 기능을 추가해 주세요",
        llm_deployment_id="llm-title",
    )

    assert response == GenerateTitleResponse(
        title="FastAPI 제목 생성"
    )
    assert manager.capture_calls == 1
    assert len(backend.calls) == 1
    messages, deployment, parameters, output_schema = backend.calls[0]
    assert messages == [
        {
            "role": "system",
            "content": "제목만 한 줄로 생성하십시오.",
        },
        {
            "role": "user",
            "content": "FastAPI에 자동 제목 기능을 추가해 주세요",
        },
    ]
    assert deployment is manager.snapshot.deployments["llm-title"]
    assert parameters == {
        "max_tokens": TITLE_MAX_TOKENS,
        "temperature": TITLE_TEMPERATURE,
        "reasoning_effort": TITLE_REASONING_EFFORT,
    }
    assert output_schema is None


@pytest.mark.asyncio
async def test_generate_title_uses_complete_resolver_plan() -> None:
    """Pipeline이 Snapshot을 한 번 캡처하고 완성된 실행 계획만 사용합니다."""

    snapshot = _snapshot()
    plan = DeploymentResolver().resolve_title_generation(
        llm_deployment_id="llm-title",
        snapshot=snapshot,
    )
    resolver = RecordingTitleResolver(plan)
    backend = RecordingLlmBackend(LlmResult(text="제목 생성"))
    manager = RecordingSnapshotManager(snapshot)
    providers = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="test_title_llm",
                provider=backend,
            )
        ]
    )
    pipeline = TitleGenerationPipeline(
        registry_manager=manager,
        deployment_resolver=resolver,
        backend_providers=providers,
    )

    await pipeline.generate_title(
        text="원문",
        llm_deployment_id="llm-title",
    )

    assert manager.capture_calls == 1
    assert resolver.calls == [("llm-title", snapshot)]
    assert backend.calls[0][0][0]["content"] == plan.title_prompt.render()


@pytest.mark.asyncio
async def test_generate_title_rejects_invalid_backend_result_type() -> None:
    """Backend가 LlmResult가 아닌 값을 반환하면 내부 계약 오류로 처리합니다."""

    backend = RecordingLlmBackend({"text": "제목"})
    pipeline, _ = _pipeline(backend)

    with pytest.raises(TitleBackendResultError) as error_info:
        await pipeline.generate_title(
            text="원문",
            llm_deployment_id="llm-title",
        )

    assert error_info.value.deployment_id == "llm-title"
    assert error_info.value.actual_type is dict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_output",
    [
        "",
        "제목\n부연 설명",
        "가" * 31,
        "제목: 자동 제목",
        "```제목```",
    ],
)
async def test_generate_title_rejects_untrusted_model_output(
    invalid_output: str,
) -> None:
    """비어 있거나 여러 줄인 모델 출력을 제목으로 반환하지 않습니다."""

    backend = RecordingLlmBackend(LlmResult(text=invalid_output))
    pipeline, _ = _pipeline(backend)

    with pytest.raises(TitleOutputValidationError) as error_info:
        await pipeline.generate_title(
            text="원문",
            llm_deployment_id="llm-title",
        )

    assert error_info.value.code == "TITLE_OUTPUT_INVALID"
    if invalid_output:
        assert invalid_output not in error_info.value.detail


@pytest.mark.asyncio
async def test_generate_title_propagates_backend_error() -> None:
    """모델 호출 오류를 삼키거나 다른 오류로 바꾸지 않습니다."""

    expected = RuntimeError("테스트 Backend 오류")
    backend = RecordingLlmBackend(
        LlmResult(text="사용되지 않음"),
        error=expected,
    )
    pipeline, _ = _pipeline(backend)

    with pytest.raises(RuntimeError) as error_info:
        await pipeline.generate_title(
            text="원문",
            llm_deployment_id="llm-title",
        )

    assert error_info.value is expected
