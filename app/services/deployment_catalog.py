"""활성 Snapshot에서 안전한 Deployment 공개 정보를 조회합니다."""

from __future__ import annotations

from app.schemas.deployments import (
    DeploymentDetail,
    DeploymentSummary,
    LlmDeploymentDetail,
    NerDeploymentDetail,
)
from app.schemas.registry import (
    DeploymentConfig,
    DeploymentKind,
)
from app.services.registry_snapshot_provider import RegistrySnapshotProvider


class DeploymentCatalogNotFoundError(LookupError):
    """상세 조회 대상 Deployment가 현재 Snapshot에 없으면 발생합니다."""

    code = "DEPLOYMENT_NOT_FOUND"

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(f"{self.code}: {deployment_id}")


class DeploymentCatalogService:
    """Registry 내부 실행 설정을 조회 응답 모델로 투영합니다."""

    def __init__(
        self,
        registry_manager: RegistrySnapshotProvider,
    ) -> None:
        self._registry_manager = registry_manager

    def list_deployments(
        self,
        *,
        kind: DeploymentKind,
    ) -> tuple[DeploymentSummary, ...]:
        """현재 Snapshot에서 지정한 종류의 Deployment를 ID 순서로 요약합니다."""

        snapshot = self._registry_manager.capture()
        return tuple(
            self._to_summary(deployment_id, deployment)
            for deployment_id, deployment in sorted(
                snapshot.deployments.items()
            )
            if deployment.kind == kind
        )

    def get_deployment(
        self,
        deployment_id: str,
        *,
        kind: DeploymentKind,
    ) -> DeploymentDetail:
        """현재 Snapshot에서 ID와 종류가 일치하는 Deployment를 반환합니다."""

        snapshot = self._registry_manager.capture()
        deployment = snapshot.deployments.get(deployment_id)
        if deployment is None or deployment.kind != kind:
            raise DeploymentCatalogNotFoundError(deployment_id)
        return self.build_detail(deployment_id, deployment)

    @staticmethod
    def _to_summary(
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> DeploymentSummary:
        """내부 Endpoint 설정을 제외한 목록용 Deployment 상태를 만듭니다."""

        return DeploymentSummary(
            deployment_id=deployment_id,
            enabled=deployment.enabled,
        )

    @staticmethod
    def build_detail(
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> DeploymentDetail:
        """Gateway용 상세 Backend 실행 설정을 만듭니다."""

        if deployment.kind == "ner":
            if (
                deployment.base_url is None
                or deployment.timeout_ms is None
            ):
                raise RuntimeError(
                    "공통 NER Deployment 실행 설정이 완전하지 않습니다"
                )
            return NerDeploymentDetail(
                deployment_id=deployment_id,
                enabled=deployment.enabled,
                base_url=deployment.base_url,
                timeout_ms=deployment.timeout_ms,
            )
        return LlmDeploymentDetail(
            deployment_id=deployment_id,
            enabled=deployment.enabled,
            adapter_type=deployment.adapter_type,
            base_url=deployment.base_url,
            model_name=deployment.model_name,
            timeout_ms=deployment.timeout_ms,
        )


__all__ = [
    "DeploymentCatalogNotFoundError",
    "DeploymentCatalogService",
]
