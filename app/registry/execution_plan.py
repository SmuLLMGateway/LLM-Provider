"""요청이 직접 선택한 Deployment의 역할별 실행 계획을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.schemas.registry import DeploymentConfig, ResourceId


_RESOURCE_ID_ADAPTER = TypeAdapter(ResourceId)


def _validate_resource_id(value: object, *, label: str) -> None:
    """Registry와 같은 형식으로 실행 계획의 리소스 ID를 검증합니다."""

    try:
        _RESOURCE_ID_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        raise ValueError(
            f"{label}는 Registry 리소스 ID 형식이어야 합니다"
        ) from None


@dataclass(frozen=True, slots=True)
class ResolvedDeployment:
    """Deployment ID와 검증된 실행 설정을 함께 보관합니다."""

    id: str
    config: DeploymentConfig

    def __post_init__(self) -> None:
        """직접 조립한 Deployment도 실행 가능한 상태인지 확인합니다."""

        _validate_resource_id(self.id, label="Deployment ID")
        if not isinstance(self.config, DeploymentConfig):
            raise TypeError("config는 DeploymentConfig여야 합니다")
        if not self.config.enabled:
            raise ValueError("비활성화된 Deployment는 실행할 수 없습니다")


@dataclass(frozen=True, slots=True)
class DetectionExecutionPlan:
    """탐지 요청이 사용할 NER, LLM과 고정 Prompt를 보관합니다."""

    ner_deployment: ResolvedDeployment
    llm_deployment: ResolvedDeployment
    detection_prompt: PromptArtifact

    def __post_init__(self) -> None:
        """NER·LLM 종류와 고정 Prompt 역할을 방어적으로 검증합니다."""

        deployments = (
            self.ner_deployment,
            self.llm_deployment,
        )
        if any(
            not isinstance(deployment, ResolvedDeployment)
            for deployment in deployments
        ):
            raise TypeError("실행 계획에는 ResolvedDeployment가 필요합니다")
        if not isinstance(self.detection_prompt, PromptArtifact):
            raise TypeError("detectionPrompt는 PromptArtifact여야 합니다")

        if self.ner_deployment.config.kind != "ner":
            raise ValueError("NER Deployment의 kind는 ner여야 합니다")
        if self.llm_deployment.config.kind != "llm":
            raise ValueError(
                "Detection LLM Deployment의 kind는 llm이어야 합니다"
            )


@dataclass(frozen=True, slots=True)
class GenerationExecutionPlan:
    """생성 요청이 사용할 LLM Deployment를 보관합니다."""

    llm_deployment: ResolvedDeployment

    def __post_init__(self) -> None:
        """생성 실행 계획의 Deployment 종류를 검증합니다."""

        if not isinstance(self.llm_deployment, ResolvedDeployment):
            raise TypeError("실행 계획에는 ResolvedDeployment가 필요합니다")
        if self.llm_deployment.config.kind != "llm":
            raise ValueError(
                "Generation LLM Deployment의 kind는 llm이어야 합니다"
            )


@dataclass(frozen=True, slots=True)
class MaskingExecutionPlan:
    """마스킹 요청에 사용할 LLM과 고정 System Prompt를 보관합니다."""

    llm_deployment: ResolvedDeployment
    mask_prompt: MaskPromptArtifact

    def __post_init__(self) -> None:
        """마스킹 실행 계획의 Deployment 종류와 Prompt 타입을 검증합니다."""

        if not isinstance(self.llm_deployment, ResolvedDeployment):
            raise TypeError("실행 계획에는 ResolvedDeployment가 필요합니다")
        if self.llm_deployment.config.kind != "llm":
            raise ValueError("Masking LLM Deployment의 kind는 llm이어야 합니다")
        if not isinstance(self.mask_prompt, MaskPromptArtifact):
            raise TypeError("maskPrompt는 MaskPromptArtifact여야 합니다")


@dataclass(frozen=True, slots=True)
class TitleGenerationExecutionPlan:
    """제목 생성 요청에 사용할 LLM과 고정 System Prompt를 보관합니다."""

    llm_deployment: ResolvedDeployment
    title_prompt: TitlePromptArtifact

    def __post_init__(self) -> None:
        """제목 생성 계획의 Deployment 종류와 Prompt 타입을 검증합니다."""

        if not isinstance(self.llm_deployment, ResolvedDeployment):
            raise TypeError(
                "실행 계획에는 ResolvedDeployment가 필요합니다"
            )
        if self.llm_deployment.config.kind != "llm":
            raise ValueError(
                "Title LLM Deployment의 kind는 llm이어야 합니다"
            )
        if not isinstance(self.title_prompt, TitlePromptArtifact):
            raise TypeError(
                "titlePrompt는 TitlePromptArtifact여야 합니다"
            )


__all__ = [
    "DetectionExecutionPlan",
    "GenerationExecutionPlan",
    "MaskingExecutionPlan",
    "ResolvedDeployment",
    "TitleGenerationExecutionPlan",
]
