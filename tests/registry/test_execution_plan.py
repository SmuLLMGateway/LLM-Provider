"""Deployment 기반 탐지·생성 실행 계획의 불변식을 검증합니다."""

from dataclasses import FrozenInstanceError
import pytest

from app.registry.execution_plan import (
    DetectionExecutionPlan,
    GenerationExecutionPlan,
    MaskingExecutionPlan,
    ResolvedDeployment,
    TitleGenerationExecutionPlan,
)
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.schemas.registry import DeploymentConfig


def _deployment(
    kind: str,
    *,
    enabled: bool = True,
) -> DeploymentConfig:
    """실행 계획 테스트용 NER 또는 LLM 설정을 만듭니다."""

    data: dict[str, object] = {
        "kind": kind,
        "adapterType": "mock",
        "enabled": enabled,
    }
    return DeploymentConfig.model_validate(data)


def _resolved(
    deployment_id: str,
    kind: str,
    *,
    enabled: bool = True,
) -> ResolvedDeployment:
    """ID와 Deployment 설정을 묶은 실행 리소스를 만듭니다."""

    return ResolvedDeployment(
        id=deployment_id,
        config=_deployment(kind, enabled=enabled),
    )


def _prompt() -> PromptArtifact:
    """컴파일된 고정 탐지 Prompt Artifact를 만듭니다."""

    return PromptArtifact.compile(
        "{{ text }} / {{ existing_detections }}",
        renderer=PromptRenderer(),
        template_path="test",
    )


def _title_prompt() -> TitlePromptArtifact:
    """컴파일된 고정 제목 Prompt Artifact를 만듭니다."""

    return TitlePromptArtifact.compile(
        "제목만 생성하십시오.",
        renderer=PromptRenderer(),
        template_path="title-test",
    )


def _mask_prompt() -> MaskPromptArtifact:
    """컴파일된 고정 마스킹 Prompt Artifact를 만듭니다."""

    return MaskPromptArtifact.compile(
        "탐지 구간을 마스킹하십시오.",
        renderer=PromptRenderer(),
        template_path="mask-test",
    )


def test_detection_plan_keeps_resolved_resources_and_prompt() -> None:
    """탐지 계획이 요청에 필요한 NER·LLM·Prompt 객체를 그대로 보관합니다."""

    ner = _resolved("ner-a", "ner")
    llm = _resolved("llm-a", "llm")
    prompt = _prompt()

    plan = DetectionExecutionPlan(
        ner_deployment=ner,
        llm_deployment=llm,
        detection_prompt=prompt,
    )

    assert plan.ner_deployment is ner
    assert plan.llm_deployment is llm
    assert plan.detection_prompt is prompt


def test_generation_plan_keeps_only_selected_llm() -> None:
    """생성 계획은 선택된 LLM 실행 리소스만 보관합니다."""

    llm = _resolved("llm-a", "llm")

    plan = GenerationExecutionPlan(llm_deployment=llm)

    assert plan.llm_deployment is llm


def test_masking_plan_keeps_llm_and_static_prompt() -> None:
    """마스킹 계획은 선택 LLM과 같은 Snapshot의 고정 Prompt를 보관합니다."""

    llm = _resolved("llm-a", "llm")
    prompt = _mask_prompt()

    plan = MaskingExecutionPlan(
        llm_deployment=llm,
        mask_prompt=prompt,
    )

    assert plan.llm_deployment is llm
    assert plan.mask_prompt is prompt


def test_title_generation_plan_keeps_llm_and_prompt() -> None:
    """제목 생성 계획은 선택 LLM과 고정 제목 Prompt를 보관합니다."""

    llm = _resolved("llm-a", "llm")
    prompt = _title_prompt()

    plan = TitleGenerationExecutionPlan(
        llm_deployment=llm,
        title_prompt=prompt,
    )

    assert plan.llm_deployment is llm
    assert plan.title_prompt is prompt


@pytest.mark.parametrize("deployment_id", ["", "Deployment A", "UPPER"])
def test_resolved_deployment_rejects_invalid_id(
    deployment_id: str,
) -> None:
    """Registry ResourceId 형식이 아닌 Deployment ID를 거부합니다."""

    with pytest.raises(ValueError):
        ResolvedDeployment(
            id=deployment_id,
            config=_deployment("ner"),
        )


def test_resolved_deployment_rejects_disabled_config() -> None:
    """요청 시점에 비활성화된 Deployment를 실행 리소스로 만들지 않습니다."""

    with pytest.raises(ValueError, match="비활성화"):
        _resolved("ner-disabled", "ner", enabled=False)


def test_resolved_deployment_rejects_non_config() -> None:
    """DeploymentConfig가 아닌 임의 객체를 실행 리소스로 받지 않습니다."""

    with pytest.raises(TypeError, match="DeploymentConfig"):
        ResolvedDeployment(id="ner-a", config=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("ner_kind", "llm_kind", "message"),
    [
        ("llm", "llm", "NER Deployment"),
        ("ner", "ner", "Detection LLM Deployment"),
    ],
)
def test_detection_plan_enforces_role_kinds(
    ner_kind: str,
    llm_kind: str,
    message: str,
) -> None:
    """탐지 계획의 NER와 LLM 역할에 맞는 kind를 강제합니다."""

    with pytest.raises(ValueError, match=message):
        DetectionExecutionPlan(
            ner_deployment=_resolved("first", ner_kind),
            llm_deployment=_resolved("second", llm_kind),
            detection_prompt=_prompt(),
        )


def test_generation_plan_requires_llm_kind() -> None:
    """생성 실행 계획에 NER Deployment를 넣지 못하게 합니다."""

    with pytest.raises(ValueError, match="Generation LLM"):
        GenerationExecutionPlan(
            llm_deployment=_resolved("ner-a", "ner")
        )


def test_masking_plan_requires_llm_kind_and_mask_prompt() -> None:
    """마스킹 계획에 NER Deployment나 다른 Prompt 타입을 넣지 못하게 합니다."""

    with pytest.raises(ValueError, match="Masking LLM"):
        MaskingExecutionPlan(
            llm_deployment=_resolved("ner-a", "ner"),
            mask_prompt=_mask_prompt(),
        )

    with pytest.raises(TypeError, match="MaskPromptArtifact"):
        MaskingExecutionPlan(
            llm_deployment=_resolved("llm-a", "llm"),
            mask_prompt=_prompt(),  # type: ignore[arg-type]
        )


def test_title_generation_plan_requires_llm_kind() -> None:
    """제목 생성 계획에 NER Deployment를 넣지 못하게 합니다."""

    with pytest.raises(ValueError, match="Title LLM"):
        TitleGenerationExecutionPlan(
            llm_deployment=_resolved("ner-a", "ner"),
            title_prompt=_title_prompt(),
        )


def test_execution_plans_are_frozen() -> None:
    """조립된 실행 계획을 요청 처리 중 변경할 수 없습니다."""

    detection = DetectionExecutionPlan(
        ner_deployment=_resolved("ner-a", "ner"),
        llm_deployment=_resolved("llm-a", "llm"),
        detection_prompt=_prompt(),
    )
    generation = GenerationExecutionPlan(
        llm_deployment=_resolved("llm-a", "llm")
    )
    masking = MaskingExecutionPlan(
        llm_deployment=_resolved("llm-a", "llm"),
        mask_prompt=_mask_prompt(),
    )
    title = TitleGenerationExecutionPlan(
        llm_deployment=_resolved("llm-a", "llm"),
        title_prompt=_title_prompt(),
    )

    with pytest.raises(FrozenInstanceError):
        detection.llm_deployment = _resolved("other", "llm")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        generation.llm_deployment = _resolved("other", "llm")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        masking.llm_deployment = _resolved("other", "llm")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        title.llm_deployment = _resolved("other", "llm")  # type: ignore[misc]
