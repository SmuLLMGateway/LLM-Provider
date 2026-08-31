"""중지된 애플리케이션의 Deployment Registry 파일을 편집합니다."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from app.registry.file_store import (
    DEFAULT_CONFIG_DIR,
    RegistryFileStore,
)
from app.registry.mutator import RegistryMutator
from app.registry.store import RegistryStore
from app.schemas.registry import DeploymentConfig


class OfflineRegistryEditor:
    """실행 Snapshot을 건드리지 않고 Registry 파일만 오프라인 편집합니다.

    이 객체는 LPL 프로세스가 완전히 중지된 상태의 초기 설정, 마이그레이션과
    수동 정비에만 사용합니다. 실행 중 온라인 변경은 반드시
    ``DeploymentManagementService``를 통해야 합니다.
    """

    def __init__(
        self,
        store: RegistryStore,
        mutator: RegistryMutator | None = None,
    ) -> None:
        self.store = store
        store_validator = getattr(store, "validator", None)
        if (
            mutator is not None
            and store_validator is not None
            and mutator.validator is not store_validator
        ):
            raise ValueError(
                "RegistryStore와 OfflineRegistryEditor는 같은 "
                "RegistryValidator를 사용해야 합니다."
            )
        if mutator is not None:
            self.mutator = mutator
        elif store_validator is not None:
            self.mutator = RegistryMutator(
                validator=store_validator
            )
        else:
            self.mutator = RegistryMutator()

    def add_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """검증한 Deployment를 Registry에 추가합니다."""

        validated_id = self.mutator.validate_deployment_id(
            deployment_id
        )
        validated_deployment = (
            deployment
            if isinstance(deployment, DeploymentConfig)
            else DeploymentConfig.model_validate(deployment)
        )

        self.store.update(
            partial(
                self.mutator.add_deployment,
                deployment_id=validated_id,
                deployment=validated_deployment,
            )
        )
        return validated_deployment

    def update_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """기존 Deployment 설정 전체를 검증하여 교체합니다."""

        validated_id = self.mutator.validate_deployment_id(
            deployment_id
        )
        validated_deployment = (
            deployment
            if isinstance(deployment, DeploymentConfig)
            else DeploymentConfig.model_validate(deployment)
        )

        self.store.update(
            partial(
                self.mutator.update_deployment,
                deployment_id=validated_id,
                deployment=validated_deployment,
            )
        )
        return validated_deployment


def add_deployment_offline(
    deployment_id: str,
    deployment: DeploymentConfig | dict[str, object],
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
) -> DeploymentConfig:
    """중지된 애플리케이션의 기본 파일 저장소에 Deployment를 추가합니다."""

    editor = OfflineRegistryEditor(RegistryFileStore(config_dir))
    return editor.add_deployment(deployment_id, deployment)


def update_deployment_offline(
    deployment_id: str,
    deployment: DeploymentConfig | dict[str, object],
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
) -> DeploymentConfig:
    """중지된 애플리케이션의 Deployment 전체 설정을 교체합니다."""

    editor = OfflineRegistryEditor(RegistryFileStore(config_dir))
    return editor.update_deployment(deployment_id, deployment)


__all__ = [
    "OfflineRegistryEditor",
    "add_deployment_offline",
    "update_deployment_offline",
]
