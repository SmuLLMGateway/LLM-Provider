"""RegistryConfig의 Deployment를 추가·교체·삭제합니다."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from app.registry.validator import RegistryValidator
from app.schemas.registry import (
    DeploymentConfig,
    DeploymentKind,
    RegistryConfig,
    ResourceId,
)


_RESOURCE_ID_ADAPTER = TypeAdapter(ResourceId)


class DeploymentAlreadyExistsError(ValueError):
    """같은 ID의 Deployment가 이미 등록된 경우 발생합니다."""

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(
            f"이미 등록된 Deployment ID입니다: {deployment_id}"
        )


class DeploymentNotFoundError(ValueError):
    """변경할 Deployment ID가 등록되어 있지 않으면 발생합니다."""

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(
            f"등록되지 않은 Deployment ID입니다: {deployment_id}"
        )


class DeploymentMustBeDisabledError(ValueError):
    """활성 Deployment 삭제 요청을 거부할 때 발생합니다."""

    code = "DEPLOYMENT_MUST_BE_DISABLED"

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(
            f"비활성화한 뒤 삭제해야 하는 Deployment입니다: {deployment_id}"
        )


class RegistryMutator:
    """기존 설정을 변경하지 않고 새로운 RegistryConfig를 만듭니다."""

    def __init__(
        self,
        validator: RegistryValidator | None = None,
    ) -> None:
        self.validator = validator or RegistryValidator()

    def add_deployment(
        self,
        registry: RegistryConfig,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> RegistryConfig:
        """Deployment가 추가된 새로운 RegistryConfig를 만듭니다."""

        deployment_id = self.validate_deployment_id(deployment_id)
        if deployment_id in registry.deployments:
            raise DeploymentAlreadyExistsError(deployment_id)

        self.validator.backend_registry.validate_deployment(
            deployment_id,
            deployment,
        )
        registry_data = self._dump_registry(registry)
        registry_data["deployments"][deployment_id] = deployment.model_dump(
            by_alias=True,
            mode="json",
            exclude_unset=True,
        )
        updated_registry = RegistryConfig.model_validate(registry_data)
        self.validator.validate_registry(updated_registry)
        return updated_registry

    def update_deployment(
        self,
        registry: RegistryConfig,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> RegistryConfig:
        """Deployment가 교체된 새로운 RegistryConfig를 만듭니다."""

        deployment_id = self.validate_deployment_id(deployment_id)
        existing_deployment = registry.deployments.get(deployment_id)
        if (
            existing_deployment is None
            or existing_deployment.kind != deployment.kind
        ):
            raise DeploymentNotFoundError(deployment_id)

        self.validator.backend_registry.validate_deployment(
            deployment_id,
            deployment,
        )
        registry_data = self._dump_registry(registry)
        registry_data["deployments"][deployment_id] = deployment.model_dump(
            by_alias=True,
            mode="json",
            exclude_unset=True,
        )
        updated_registry = RegistryConfig.model_validate(registry_data)
        self.validator.validate_registry(updated_registry)
        return updated_registry

    def set_deployment_enabled(
        self,
        registry: RegistryConfig,
        *,
        deployment_id: str,
        kind: DeploymentKind,
        enabled: bool,
    ) -> RegistryConfig:
        """기존 Deployment의 다른 설정을 보존하고 활성 상태만 교체합니다."""

        deployment_id = self.validate_deployment_id(deployment_id)
        existing_deployment = registry.deployments.get(deployment_id)
        if (
            existing_deployment is None
            or existing_deployment.kind != kind
        ):
            raise DeploymentNotFoundError(deployment_id)

        deployment_data = existing_deployment.model_dump(
            by_alias=True,
            mode="python",
            exclude_unset=True,
        )
        deployment_data["enabled"] = enabled
        updated_deployment = DeploymentConfig.model_validate(
            deployment_data,
            by_alias=True,
            by_name=False,
        )
        return self.update_deployment(
            registry,
            deployment_id=deployment_id,
            deployment=updated_deployment,
        )

    def delete_deployment(
        self,
        registry: RegistryConfig,
        *,
        deployment_id: str,
        kind: DeploymentKind,
    ) -> RegistryConfig:
        """비활성 Deployment가 제거된 새로운 RegistryConfig를 만듭니다."""

        deployment_id = self.validate_deployment_id(deployment_id)
        existing_deployment = registry.deployments.get(deployment_id)
        if (
            existing_deployment is None
            or existing_deployment.kind != kind
        ):
            raise DeploymentNotFoundError(deployment_id)
        if existing_deployment.enabled:
            raise DeploymentMustBeDisabledError(deployment_id)

        registry_data = self._dump_registry(registry)
        del registry_data["deployments"][deployment_id]
        updated_registry = RegistryConfig.model_validate(registry_data)
        self.validator.validate_registry(updated_registry)
        return updated_registry

    @staticmethod
    def validate_deployment_id(deployment_id: str) -> str:
        """Deployment ID를 Registry의 공통 리소스 ID 계약으로 검증합니다."""

        return _RESOURCE_ID_ADAPTER.validate_python(
            deployment_id,
            strict=True,
        )

    @staticmethod
    def _dump_registry(registry: RegistryConfig) -> dict[str, Any]:
        """새 설정을 만들기 위해 Registry를 JSON 호환 객체로 복사합니다."""

        return registry.model_dump(
            by_alias=True,
            mode="json",
            exclude_unset=True,
        )


__all__ = [
    "DeploymentAlreadyExistsError",
    "DeploymentMustBeDisabledError",
    "DeploymentNotFoundError",
    "RegistryMutator",
]
