"""요청이 전달한 Deployment ID를 같은 Snapshot의 실행 계획으로 해석합니다."""

from __future__ import annotations

from typing import Literal

from app.registry.execution_plan import (
    DetectionExecutionPlan,
    GenerationExecutionPlan,
    MaskingExecutionPlan,
    ResolvedDeployment,
    TitleGenerationExecutionPlan,
)
from app.registry.snapshot import ActiveRegistrySnapshot
from app.schemas.registry import DeploymentKind


DeploymentResolutionErrorCode = Literal[
    "DEPLOYMENT_NOT_FOUND",
    "DEPLOYMENT_DISABLED",
    "DEPLOYMENT_KIND_MISMATCH",
]


class DeploymentResolutionError(LookupError):
    """요청이 선택한 Deployment를 해당 역할로 실행할 수 없으면 발생합니다."""

    def __init__(
        self,
        code: DeploymentResolutionErrorCode,
        *,
        deployment_id: str,
        expected_kind: DeploymentKind,
    ) -> None:
        self.code = code
        self.deployment_id = deployment_id
        self.expected_kind = expected_kind
        super().__init__(
            f"{code}: {deployment_id} (expected={expected_kind})"
        )


class DeploymentResolver:
    """한 Snapshot에서 요청별 NER·LLM 실행 계획을 조립합니다."""

    def resolve_detection(
        self,
        *,
        ner_deployment_id: str,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> DetectionExecutionPlan:
        """NER와 탐지 LLM을 검증해 탐지 실행 계획을 반환합니다."""

        return DetectionExecutionPlan(
            ner_deployment=self.resolve(
                ner_deployment_id,
                expected_kind="ner",
                snapshot=snapshot,
            ),
            llm_deployment=self.resolve(
                llm_deployment_id,
                expected_kind="llm",
                snapshot=snapshot,
            ),
            detection_prompt=snapshot.detection_prompt,
        )

    def resolve_generation(
        self,
        *,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> GenerationExecutionPlan:
        """생성 LLM을 검증해 생성 실행 계획을 반환합니다."""

        return GenerationExecutionPlan(
            llm_deployment=self.resolve(
                llm_deployment_id,
                expected_kind="llm",
                snapshot=snapshot,
            )
        )

    def resolve_title_generation(
        self,
        *,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> TitleGenerationExecutionPlan:
        """제목 생성 LLM과 같은 Snapshot의 고정 Prompt를 조립합니다."""

        return TitleGenerationExecutionPlan(
            llm_deployment=self.resolve(
                llm_deployment_id,
                expected_kind="llm",
                snapshot=snapshot,
            ),
            title_prompt=snapshot.title_prompt,
        )

    def resolve_masking(
        self,
        *,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> MaskingExecutionPlan:
        """마스킹 LLM과 같은 Snapshot의 고정 Prompt를 조립합니다."""

        return MaskingExecutionPlan(
            llm_deployment=self.resolve(
                llm_deployment_id,
                expected_kind="llm",
                snapshot=snapshot,
            ),
            mask_prompt=snapshot.mask_prompt,
        )

    def resolve(
        self,
        deployment_id: str,
        *,
        expected_kind: DeploymentKind,
        snapshot: ActiveRegistrySnapshot,
    ) -> ResolvedDeployment:
        """Deployment 존재 여부, 활성 상태와 종류를 검증합니다."""

        deployment = snapshot.deployments.get(deployment_id)
        if deployment is None:
            raise DeploymentResolutionError(
                "DEPLOYMENT_NOT_FOUND",
                deployment_id=deployment_id,
                expected_kind=expected_kind,
            )
        if not deployment.enabled:
            raise DeploymentResolutionError(
                "DEPLOYMENT_DISABLED",
                deployment_id=deployment_id,
                expected_kind=expected_kind,
            )
        if deployment.kind != expected_kind:
            raise DeploymentResolutionError(
                "DEPLOYMENT_KIND_MISMATCH",
                deployment_id=deployment_id,
                expected_kind=expected_kind,
            )
        return ResolvedDeployment(
            id=deployment_id,
            config=deployment,
        )


__all__ = [
    "DeploymentResolutionError",
    "DeploymentResolutionErrorCode",
    "DeploymentResolver",
]
