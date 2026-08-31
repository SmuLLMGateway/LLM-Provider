"""Deployment 설정 저장과 Active Snapshot 활성화를 함께 관리합니다."""

from __future__ import annotations

from functools import partial
from threading import Lock
from typing import Literal

from app.registry.manager import (
    RegistryManager,
    ReloadResult,
    ReloadStatus,
)
from app.registry.mutator import RegistryMutator
from app.registry.store import RegistryStore, RegistryUpdater
from app.schemas.registry import (
    DeploymentConfig,
    DeploymentKind,
    RegistryConfig,
)


DeploymentManagementOperation = Literal[
    "add",
    "update",
    "set_enabled",
    "delete",
]


class DeploymentManagementError(RuntimeError):
    """Deployment 변경을 안전하게 완료하지 못한 경우의 공통 오류입니다."""

    code: str


class DeploymentActivationError(DeploymentManagementError):
    """저장한 Deployment 설정을 Active Snapshot으로 활성화하지 못했습니다."""

    code = "DEPLOYMENT_ACTIVATION_FAILED"

    def __init__(
        self,
        *,
        deployment_id: str,
        operation: DeploymentManagementOperation,
        activation_error: Exception,
    ) -> None:
        self.deployment_id = deployment_id
        self.operation = operation
        self.activation_error = activation_error
        super().__init__(
            "Deployment 설정을 활성화할 수 없어 이전 설정으로 복원했습니다."
        )


class DeploymentRollbackError(DeploymentManagementError):
    """활성화 실패 뒤 이전 Deployment 설정 복원도 완료하지 못했습니다."""

    code = "DEPLOYMENT_ROLLBACK_FAILED"

    def __init__(
        self,
        *,
        deployment_id: str,
        operation: DeploymentManagementOperation,
        activation_error: Exception,
        rollback_error: Exception,
    ) -> None:
        self.deployment_id = deployment_id
        self.operation = operation
        self.activation_error = activation_error
        self.rollback_error = rollback_error
        super().__init__(
            "Deployment 설정 활성화 실패 후 이전 설정 복원에도 실패했습니다."
        )


class DeploymentStorageError(DeploymentManagementError):
    """Deployment 설정 저장소를 읽거나 쓸 수 없는 경우 발생합니다."""

    code = "DEPLOYMENT_STORAGE_FAILED"

    def __init__(
        self,
        *,
        deployment_id: str,
        operation: DeploymentManagementOperation,
        storage_error: Exception,
    ) -> None:
        self.deployment_id = deployment_id
        self.operation = operation
        self.storage_error = storage_error
        super().__init__(
            "Deployment 설정 저장소 작업을 완료하지 못했습니다."
        )


class DeploymentManagementService:
    """Deployment 파일 변경과 실행 Snapshot 교체를 하나의 작업으로 처리합니다."""

    def __init__(
        self,
        *,
        store: RegistryStore,
        registry_manager: RegistryManager,
        mutator: RegistryMutator | None = None,
    ) -> None:
        store_validator = getattr(store, "validator", None)
        if (
            mutator is not None
            and store_validator is not None
            and mutator.validator is not store_validator
        ):
            raise ValueError(
                "RegistryStore와 DeploymentManagementService는 같은 "
                "RegistryValidator를 사용해야 합니다."
            )

        self.store = store
        self.registry_manager = registry_manager
        if mutator is not None:
            self.mutator = mutator
        elif store_validator is not None:
            self.mutator = RegistryMutator(
                validator=store_validator
            )
        else:
            self.mutator = RegistryMutator()
        self._mutation_lock = Lock()

    def add_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """Deployment를 저장하고 정상 후보일 때 Active Snapshot에 반영합니다."""

        validated_id = self.mutator.validate_deployment_id(
            deployment_id
        )
        validated_deployment = self._validate_deployment(deployment)
        updated_registry = self._mutate_and_activate(
            deployment_id=validated_id,
            operation="add",
            kind=validated_deployment.kind,
            updater=partial(
                self.mutator.add_deployment,
                deployment_id=validated_id,
                deployment=validated_deployment,
            ),
        )
        return updated_registry.deployments[validated_id]

    def update_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """Deployment 전체 설정을 교체하고 Active Snapshot에 반영합니다."""

        validated_id = self.mutator.validate_deployment_id(
            deployment_id
        )
        validated_deployment = self._validate_deployment(deployment)
        updated_registry = self._mutate_and_activate(
            deployment_id=validated_id,
            operation="update",
            kind=validated_deployment.kind,
            updater=partial(
                self.mutator.update_deployment,
                deployment_id=validated_id,
                deployment=validated_deployment,
            ),
        )
        return updated_registry.deployments[validated_id]

    def set_deployment_enabled(
        self,
        deployment_id: str,
        *,
        kind: DeploymentKind,
        enabled: bool,
    ) -> DeploymentConfig:
        """Deployment의 활성 상태만 저장하고 Active Snapshot에 반영합니다."""

        validated_id = self.mutator.validate_deployment_id(
            deployment_id
        )
        updated_registry = self._mutate_and_activate(
            deployment_id=validated_id,
            operation="set_enabled",
            kind=kind,
            updater=partial(
                self.mutator.set_deployment_enabled,
                deployment_id=validated_id,
                kind=kind,
                enabled=enabled,
            ),
        )
        return updated_registry.deployments[validated_id]

    def delete_deployment(
        self,
        deployment_id: str,
        *,
        kind: DeploymentKind,
    ) -> None:
        """비활성 Deployment를 삭제하고 Active Snapshot에 반영합니다."""

        validated_id = self.mutator.validate_deployment_id(
            deployment_id
        )
        self._mutate_and_activate(
            deployment_id=validated_id,
            operation="delete",
            kind=kind,
            updater=partial(
                self.mutator.delete_deployment,
                deployment_id=validated_id,
                kind=kind,
            ),
        )

    def _mutate_and_activate(
        self,
        *,
        deployment_id: str,
        operation: DeploymentManagementOperation,
        kind: DeploymentKind,
        updater: RegistryUpdater,
    ) -> RegistryConfig:
        """변경 작업 전체를 직렬화하고 활성화 실패 시 이전 파일로 복구합니다."""

        with self._mutation_lock:
            try:
                previous_registry = self.store.load()
            except Exception as storage_error:
                raise DeploymentStorageError(
                    deployment_id=deployment_id,
                    operation=operation,
                    storage_error=storage_error,
                ) from storage_error

            previous_kind_deployments = {
                resource_id: deployment
                for resource_id, deployment
                in previous_registry.deployments.items()
                if deployment.kind == kind
            }
            try:
                updated_registry = self.store.update(updater)
            except OSError as storage_error:
                raise DeploymentStorageError(
                    deployment_id=deployment_id,
                    operation=operation,
                    storage_error=storage_error,
                ) from storage_error
            activation_error = self._activate_candidate()

            if activation_error is None:
                return updated_registry

            self._restore_previous_kind(
                kind=kind,
                previous_kind_deployments=previous_kind_deployments,
                deployment_id=deployment_id,
                operation=operation,
                activation_error=activation_error,
            )
            raise DeploymentActivationError(
                deployment_id=deployment_id,
                operation=operation,
                activation_error=activation_error,
            ) from activation_error

    def _activate_candidate(self) -> Exception | None:
        """저장된 후보를 Reload하고 실패 원인만 반환합니다."""

        try:
            result = self.registry_manager.try_reload()
        except Exception as error:
            return error

        if result.status in {
            ReloadStatus.APPLIED,
            ReloadStatus.NO_CHANGE,
        }:
            return None
        return self._reload_failure(
            result,
            fallback_message="후보 Registry Snapshot이 거부되었습니다.",
        )

    def _restore_previous_kind(
        self,
        *,
        kind: DeploymentKind,
        previous_kind_deployments: dict[str, DeploymentConfig],
        deployment_id: str,
        operation: DeploymentManagementOperation,
        activation_error: Exception,
    ) -> None:
        """변경한 종류만 이전 값으로 복원하고 Snapshot을 다시 검증합니다."""

        try:
            self.store.update(
                lambda current_registry: RegistryConfig(
                    deployments={
                        resource_id: deployment
                        for resource_id, deployment
                        in current_registry.deployments.items()
                        if deployment.kind != kind
                    }
                    | previous_kind_deployments
                )
            )
            rollback_result = self.registry_manager.try_reload()
        except Exception as rollback_error:
            raise DeploymentRollbackError(
                deployment_id=deployment_id,
                operation=operation,
                activation_error=activation_error,
                rollback_error=rollback_error,
            ) from rollback_error

        if rollback_result.status in {
            ReloadStatus.APPLIED,
            ReloadStatus.NO_CHANGE,
        }:
            return

        rollback_error = self._reload_failure(
            rollback_result,
            fallback_message="복원한 Registry Snapshot이 거부되었습니다.",
        )
        raise DeploymentRollbackError(
            deployment_id=deployment_id,
            operation=operation,
            activation_error=activation_error,
            rollback_error=rollback_error,
        ) from rollback_error

    @staticmethod
    def _reload_failure(
        result: ReloadResult,
        *,
        fallback_message: str,
    ) -> Exception:
        """Reload 결과에 포함된 원래 오류 또는 안전한 대체 오류를 반환합니다."""

        return result.error or RuntimeError(fallback_message)

    @staticmethod
    def _validate_deployment(
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """파일을 읽기 전에 변경할 Deployment의 구조를 검증합니다."""

        if isinstance(deployment, DeploymentConfig):
            return deployment
        return DeploymentConfig.model_validate(deployment)


__all__ = [
    "DeploymentActivationError",
    "DeploymentManagementError",
    "DeploymentManagementOperation",
    "DeploymentManagementService",
    "DeploymentRollbackError",
    "DeploymentStorageError",
]
