"""요청의 Deployment ID를 역할별 실행 계획으로 해석하는지 검증합니다."""

import pytest

from app.registry.deployment_resolver import (
    DeploymentResolutionError,
    DeploymentResolver,
)
from app.registry.snapshot import ActiveRegistrySnapshot
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.schemas.registry import RegistryConfig


def _prompt() -> PromptArtifact:
    """Resolver가 실행 계획에 전달할 컴파일된 Prompt를 만듭니다."""

    return PromptArtifact.compile(
        "{{ text }} / {{ existing_detections }}",
        renderer=PromptRenderer(),
        template_path="test",
    )


def _title_prompt() -> TitlePromptArtifact:
    """Resolver가 제목 실행 계획에 전달할 정적 Prompt를 만듭니다."""

    return TitlePromptArtifact.compile(
        "제목만 생성하십시오.",
        renderer=PromptRenderer(),
        template_path="title-test",
    )


def _mask_prompt() -> MaskPromptArtifact:
    """Resolver가 마스킹 실행 계획에 전달할 정적 Prompt를 만듭니다."""

    return MaskPromptArtifact.compile(
        "탐지 구간을 마스킹하십시오.",
        renderer=PromptRenderer(),
        template_path="mask-test",
    )


def _snapshot() -> ActiveRegistrySnapshot:
    """활성·비활성 NER/LLM Deployment를 가진 Snapshot을 만듭니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "ner-a": {
                    "kind": "ner",
                    "adapterType": "mock",
                    "enabled": True,
                },
                "ner-disabled": {
                    "kind": "ner",
                    "adapterType": "mock",
                    "enabled": False,
                },
                "llm-a": {
                    "kind": "llm",
                    "adapterType": "openai_compatible",
                    "baseUrl": "http://localhost:9000/v1",
                    "modelName": "model-a",
                    "timeoutMs": 5000,
                    "enabled": True,
                },
            }
        }
    )
    return ActiveRegistrySnapshot(
        snapshot_id="snapshot-a",
        deployments=registry.deployments,
        detection_prompt=_prompt(),
        mask_prompt=_mask_prompt(),
        title_prompt=_title_prompt(),
    )


def test_resolve_detection_returns_complete_plan() -> None:
    """NER·LLM Deployment와 같은 Snapshot의 Prompt를 조립합니다."""

    snapshot = _snapshot()

    plan = DeploymentResolver().resolve_detection(
        ner_deployment_id="ner-a",
        llm_deployment_id="llm-a",
        snapshot=snapshot,
    )

    assert plan.ner_deployment.id == "ner-a"
    assert plan.ner_deployment.config is snapshot.deployments["ner-a"]
    assert plan.llm_deployment.id == "llm-a"
    assert plan.llm_deployment.config is snapshot.deployments["llm-a"]
    assert plan.detection_prompt is snapshot.detection_prompt


def test_resolve_generation_returns_llm_plan() -> None:
    """생성 요청에는 선택한 LLM Deployment만 담습니다."""

    snapshot = _snapshot()

    plan = DeploymentResolver().resolve_generation(
        llm_deployment_id="llm-a",
        snapshot=snapshot,
    )

    assert plan.llm_deployment.id == "llm-a"
    assert plan.llm_deployment.config is snapshot.deployments["llm-a"]


def test_resolve_masking_returns_llm_and_same_snapshot_prompt() -> None:
    """마스킹 LLM과 같은 Snapshot의 정적 Prompt를 하나의 Plan으로 만듭니다."""

    snapshot = _snapshot()

    plan = DeploymentResolver().resolve_masking(
        llm_deployment_id="llm-a",
        snapshot=snapshot,
    )

    assert plan.llm_deployment.id == "llm-a"
    assert plan.llm_deployment.config is snapshot.deployments["llm-a"]
    assert plan.mask_prompt is snapshot.mask_prompt


@pytest.mark.parametrize(
    ("deployment_id", "expected_code"),
    [
        ("missing", "DEPLOYMENT_NOT_FOUND"),
        ("ner-a", "DEPLOYMENT_KIND_MISMATCH"),
    ],
)
def test_resolve_masking_rejects_unusable_llm(
    deployment_id: str,
    expected_code: str,
) -> None:
    """마스킹 역할에 사용할 수 없는 ID로 부분 Plan을 만들지 않습니다."""

    with pytest.raises(DeploymentResolutionError) as error_info:
        DeploymentResolver().resolve_masking(
            llm_deployment_id=deployment_id,
            snapshot=_snapshot(),
        )

    assert error_info.value.code == expected_code
    assert error_info.value.expected_kind == "llm"


def test_resolve_title_generation_returns_complete_plan() -> None:
    """제목 생성 LLM과 같은 Snapshot의 제목 Prompt를 함께 반환합니다."""

    snapshot = _snapshot()

    plan = DeploymentResolver().resolve_title_generation(
        llm_deployment_id="llm-a",
        snapshot=snapshot,
    )

    assert plan.llm_deployment.id == "llm-a"
    assert plan.llm_deployment.config is snapshot.deployments["llm-a"]
    assert plan.title_prompt is snapshot.title_prompt


@pytest.mark.parametrize(
    ("deployment_id", "expected_kind", "code"),
    [
        ("missing", "ner", "DEPLOYMENT_NOT_FOUND"),
        ("ner-disabled", "ner", "DEPLOYMENT_DISABLED"),
        ("llm-a", "ner", "DEPLOYMENT_KIND_MISMATCH"),
        ("ner-a", "llm", "DEPLOYMENT_KIND_MISMATCH"),
    ],
)
def test_resolve_rejects_unusable_deployment(
    deployment_id: str,
    expected_kind: str,
    code: str,
) -> None:
    """존재 여부, 활성 상태와 요청 역할의 kind를 검증합니다."""

    with pytest.raises(DeploymentResolutionError) as error_info:
        DeploymentResolver().resolve(
            deployment_id,
            expected_kind=expected_kind,  # type: ignore[arg-type]
            snapshot=_snapshot(),
        )

    assert error_info.value.code == code
    assert error_info.value.deployment_id == deployment_id
    assert error_info.value.expected_kind == expected_kind


def test_detection_resolution_stops_at_invalid_ner() -> None:
    """첫 NER 선택이 잘못되면 부분적인 탐지 계획을 만들지 않습니다."""

    with pytest.raises(DeploymentResolutionError) as error_info:
        DeploymentResolver().resolve_detection(
            ner_deployment_id="missing",
            llm_deployment_id="llm-a",
            snapshot=_snapshot(),
        )

    assert error_info.value.code == "DEPLOYMENT_NOT_FOUND"
    assert error_info.value.deployment_id == "missing"
