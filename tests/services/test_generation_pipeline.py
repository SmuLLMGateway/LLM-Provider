"""직접 선택한 LLM Deployment 생성 파이프라인을 검증합니다."""

from __future__ import annotations

import pytest

from app.backends.provider_registry import (
    BackendProviderLookupError,
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.registry.deployment_resolver import (
    DeploymentResolutionError,
    DeploymentResolver,
)
from app.registry.execution_plan import GenerationExecutionPlan
from app.registry.snapshot import ActiveRegistrySnapshot
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.schemas.generation import LlmResult, PreviousTextMessage
from app.schemas.registry import (
    DeploymentConfig,
    RegistryConfig,
)
from app.services.generation_pipeline import (
    GenerationBackendResultError,
    GenerationPipeline,
)


class SwappingSnapshotManager:
    """Snapshot을 반환한 직후 활성 Snapshot을 교체하는 테스트 대역입니다."""

    def __init__(
        self,
        captured: ActiveRegistrySnapshot,
        replacement: ActiveRegistrySnapshot | None = None,
    ) -> None:
        self.active_snapshot = captured
        self.replacement = replacement or captured
        self.capture_calls = 0

    def capture(self) -> ActiveRegistrySnapshot:
        """현재 Snapshot을 반환하고 곧바로 다음 Snapshot을 활성화합니다."""

        self.capture_calls += 1
        captured = self.active_snapshot
        self.active_snapshot = self.replacement
        return captured


class StaticExecutionPlanResolver(DeploymentResolver):
    """미리 조립한 실행 계획을 반환하고 Resolver 호출을 기록합니다."""

    def __init__(self, plan: GenerationExecutionPlan) -> None:
        self.plan = plan
        self.calls: list[tuple[str, ActiveRegistrySnapshot]] = []

    def resolve_generation(
        self,
        *,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> GenerationExecutionPlan:
        """Pipeline이 전달한 LLM Deployment ID와 Snapshot을 기록합니다."""

        self.calls.append((llm_deployment_id, snapshot))
        return self.plan


class RecordingLlmBackend:
    """LLM 호출 인자를 기록하고 예약된 결과나 예외를 반환합니다."""

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
        """호출 내용을 저장한 뒤 테스트에서 지정한 동작을 수행합니다."""

        self.calls.append(
            (messages, deployment, parameters, output_schema)
        )
        if self.error is not None:
            raise self.error
        return self.result


def _prompt_artifact(
    *,
    source: str,
    renderer: PromptRenderer,
) -> PromptArtifact:
    """검증된 Snapshot에 넣을 컴파일된 Prompt Artifact를 만듭니다."""

    return PromptArtifact.compile(
        source,
        renderer=renderer,
        template_path="<test>",
    )


def _title_prompt_artifact(
    renderer: PromptRenderer,
) -> TitlePromptArtifact:
    """Snapshot에 넣을 정적 제목 Prompt Artifact를 만듭니다."""

    return TitlePromptArtifact.compile(
        "제목만 생성하십시오.",
        renderer=renderer,
        template_path="<title-test>",
    )


def _mask_prompt_artifact(renderer: PromptRenderer) -> MaskPromptArtifact:
    """Snapshot에 넣을 정적 마스킹 Prompt Artifact를 만듭니다."""

    return MaskPromptArtifact.compile(
        "탐지 구간을 마스킹하십시오.",
        renderer=renderer,
        template_path="<mask-test>",
    )


def _snapshot(
    *,
    snapshot_id: str = "snapshot-v1",
    model_name: str = "generation-model-v1",
) -> ActiveRegistrySnapshot:
    """GenerationPipeline 테스트에 필요한 완전한 Snapshot을 만듭니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "llm-generation": {
                    "kind": "llm",
                    "adapterType": "test_llm",
                    "baseUrl": "http://localhost:9000/v1",
                    "modelName": model_name,
                    "timeoutMs": 5000,
                    "enabled": True,
                },
            },
        }
    )
    renderer = PromptRenderer()
    detection_prompt = _prompt_artifact(
        source="원문: {{ text }} / {{ existing_detections }}",
        renderer=renderer,
    )
    return ActiveRegistrySnapshot(
        snapshot_id=snapshot_id,
        deployments=registry.deployments,
        detection_prompt=detection_prompt,
        mask_prompt=_mask_prompt_artifact(renderer),
        title_prompt=_title_prompt_artifact(renderer),
    )


def _providers(backend: object) -> BackendProviderRegistry:
    """테스트 LLM Backend가 등록된 Provider Registry를 만듭니다."""

    return BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="test_llm",
                provider=backend,
            )
        ]
    )


def _pipeline(
    manager: SwappingSnapshotManager,
    providers: BackendProviderRegistry,
    *,
    deployment_resolver: DeploymentResolver | None = None,
) -> GenerationPipeline:
    """실제 Resolver와 Provider Registry를 사용하는 Pipeline을 만듭니다."""

    return GenerationPipeline(
        registry_manager=manager,
        deployment_resolver=(
            deployment_resolver
            if deployment_resolver is not None
            else DeploymentResolver()
        ),
        backend_providers=providers,
    )


@pytest.mark.asyncio
async def test_generate_forwards_user_text_to_selected_backend() -> None:
    """요청한 Deployment를 골라 사용자 입력을 그대로 전달합니다."""

    snapshot = _snapshot()
    manager = SwappingSnapshotManager(snapshot)
    expected = LlmResult(
        text="생성 결과",
        modelName="generation-model-v1",
        finishReason="stop",
    )
    backend = RecordingLlmBackend(expected)
    user_text = "그대로 전달할 입력\n{{ 렌더링하지_않음 }}"

    result = await _pipeline(manager, _providers(backend)).generate(
        text=user_text,
        llm_deployment_id="llm-generation",
    )

    assert result is expected
    assert manager.capture_calls == 1
    assert backend.calls == [
        (
            [
                {
                    "role": "user",
                    "content": user_text,
                }
            ],
            snapshot.deployments["llm-generation"],
            {},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_generate_forwards_previous_text_as_assistant_context() -> None:
    """이전 생성 결과 배열을 순서대로 assistant 메시지로 전달합니다."""

    snapshot = _snapshot()
    manager = SwappingSnapshotManager(snapshot)
    expected = LlmResult(text="후속 생성 결과")
    backend = RecordingLlmBackend(expected)

    result = await _pipeline(manager, _providers(backend)).generate(
        text="이어서 자세히 설명해 주세요.",
        previous_text=(
            PreviousTextMessage(
                role="user",
                content="첫 번째 사용자 입력입니다.",
            ),
            PreviousTextMessage(
                role="assistant",
                content="첫 번째 모델 답변입니다.",
            ),
        ),
        llm_deployment_id="llm-generation",
    )

    assert result is expected
    assert backend.calls[0][0] == [
        {
            "role": "user",
            "content": "첫 번째 사용자 입력입니다.",
        },
        {
            "role": "assistant",
            "content": "첫 번째 모델 답변입니다.",
        },
        {
            "role": "user",
            "content": "이어서 자세히 설명해 주세요.",
        },
    ]


@pytest.mark.asyncio
async def test_generate_uses_one_captured_snapshot_during_activation_swap() -> None:
    """호출 중 활성 Snapshot이 바뀌어도 처음 캡처한 구성만 사용합니다."""

    captured = _snapshot(
        snapshot_id="snapshot-old",
        model_name="old-model",
    )
    replacement = _snapshot(
        snapshot_id="snapshot-new",
        model_name="new-model",
    )
    manager = SwappingSnapshotManager(captured, replacement)
    backend = RecordingLlmBackend(LlmResult(text="완료"))

    await _pipeline(manager, _providers(backend)).generate(
        text="Snapshot 교체 중 사용자 입력",
        llm_deployment_id="llm-generation",
    )

    assert manager.capture_calls == 1
    assert manager.active_snapshot is replacement
    assert backend.calls[0][0] == [
        {"role": "user", "content": "Snapshot 교체 중 사용자 입력"}
    ]
    assert backend.calls[0][1] is captured.deployments["llm-generation"]
    assert backend.calls[0][1].model_name == "old-model"


@pytest.mark.asyncio
async def test_generate_uses_deployment_from_execution_plan() -> None:
    """Pipeline은 Snapshot을 재조회하지 않고 실행 계획의 생성 Deployment를 사용합니다."""

    captured = _snapshot(
        snapshot_id="snapshot-captured",
        model_name="captured-model",
    )
    plan_snapshot = _snapshot(
        snapshot_id="snapshot-plan",
        model_name="plan-model",
    )
    plan = DeploymentResolver().resolve_generation(
        llm_deployment_id="llm-generation",
        snapshot=plan_snapshot,
    )
    resolver = StaticExecutionPlanResolver(plan)
    manager = SwappingSnapshotManager(captured)
    backend = RecordingLlmBackend(LlmResult(text="완료"))

    await _pipeline(
        manager,
        _providers(backend),
        deployment_resolver=resolver,
    ).generate(
        text="실행 계획의 Deployment를 사용합니다.",
        llm_deployment_id="llm-generation",
    )

    assert manager.capture_calls == 1
    assert resolver.calls == [("llm-generation", captured)]
    assert backend.calls[0][1] is plan.llm_deployment.config
    assert backend.calls[0][1] is not (
        captured.deployments["llm-generation"]
    )
    assert backend.calls[0][1].model_name == "plan-model"


def test_generate_requires_llm_deployment_id_argument() -> None:
    """Generation Pipeline 직접 호출에서도 LLM ID를 생략할 수 없습니다."""

    pipeline = _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(RecordingLlmBackend(LlmResult(text="완료"))),
    )

    with pytest.raises(TypeError):
        pipeline.generate(text="LLM ID 누락 질문")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_generate_propagates_unknown_deployment_error() -> None:
    """존재하지 않는 Deployment 오류를 변경하지 않고 전달합니다."""

    manager = SwappingSnapshotManager(_snapshot())
    backend = RecordingLlmBackend(LlmResult(text="호출되면 안 됨"))

    with pytest.raises(DeploymentResolutionError) as error_info:
        await _pipeline(manager, _providers(backend)).generate(
            text="알 수 없는 Deployment 테스트",
            llm_deployment_id="unknown-llm",
        )

    assert error_info.value.code == "DEPLOYMENT_NOT_FOUND"
    assert error_info.value.deployment_id == "unknown-llm"
    assert manager.capture_calls == 1
    assert backend.calls == []


@pytest.mark.asyncio
async def test_generate_propagates_provider_lookup_error() -> None:
    """등록되지 않은 Provider 조회 오류를 Pipeline 밖으로 전달합니다."""

    manager = SwappingSnapshotManager(_snapshot())

    with pytest.raises(BackendProviderLookupError) as error_info:
        await _pipeline(manager, BackendProviderRegistry()).generate(
            text="Provider 조회 오류 테스트",
            llm_deployment_id="llm-generation",
        )

    assert error_info.value.code == "BACKEND_PROVIDER_NOT_REGISTERED"
    assert error_info.value.deployment_id == "llm-generation"


@pytest.mark.asyncio
async def test_generate_propagates_backend_execution_error() -> None:
    """Backend 실행 예외를 다른 예외로 감싸지 않고 그대로 전달합니다."""

    expected_error = RuntimeError("모델 서버 연결 실패")
    backend = RecordingLlmBackend(
        LlmResult(text="사용되지 않음"),
        error=expected_error,
    )
    manager = SwappingSnapshotManager(_snapshot())

    with pytest.raises(RuntimeError) as error_info:
        await _pipeline(manager, _providers(backend)).generate(
            text="Backend 실행 오류 테스트",
            llm_deployment_id="llm-generation",
        )

    assert error_info.value is expected_error
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_generate_rejects_non_llm_result_from_backend() -> None:
    """Backend가 LlmResult 외 값을 반환하면 명시적인 계약 오류를 냅니다."""

    backend = RecordingLlmBackend({"text": "잘못된 결과"})
    manager = SwappingSnapshotManager(_snapshot())

    with pytest.raises(GenerationBackendResultError) as error_info:
        await _pipeline(manager, _providers(backend)).generate(
            text="Backend 결과 계약 테스트",
            llm_deployment_id="llm-generation",
        )

    assert error_info.value.deployment_id == "llm-generation"
    assert error_info.value.actual_type is dict
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_generate_revalidates_constructed_llm_result() -> None:
    """Pydantic 검증을 우회해 만든 LlmResult의 필드도 다시 검증합니다."""

    backend = RecordingLlmBackend(
        LlmResult.model_construct(text=123)
    )
    manager = SwappingSnapshotManager(_snapshot())

    with pytest.raises(GenerationBackendResultError) as error_info:
        await _pipeline(manager, _providers(backend)).generate(
            text="Backend 결과 재검증 테스트",
            llm_deployment_id="llm-generation",
        )

    assert error_info.value.deployment_id == "llm-generation"
    assert error_info.value.actual_type is int
    assert error_info.value.__cause__ is None
