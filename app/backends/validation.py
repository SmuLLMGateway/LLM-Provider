"""Backend 직접 호출에서 공통으로 확인할 Deployment 상태를 검증합니다."""

from __future__ import annotations

from app.schemas.registry import DeploymentConfig, DeploymentKind


class BackendDeploymentValidationError(ValueError):
    """Deployment가 특정 Backend의 실행 조건과 다르면 발생합니다."""


def validate_runnable_deployment(
    deployment: DeploymentConfig,
    *,
    expected_kind: DeploymentKind,
    expected_adapter_type: str,
) -> None:
    """Deployment의 종류, Adapter와 활성화 상태를 공통으로 검증합니다."""

    if deployment.kind != expected_kind:
        raise BackendDeploymentValidationError(
            f"Deployment kind는 {expected_kind}이어야 합니다"
        )
    if deployment.adapter_type != expected_adapter_type:
        raise BackendDeploymentValidationError(
            "Deployment adapterType은 "
            f"{expected_adapter_type}이어야 합니다"
        )
    if not deployment.enabled:
        raise BackendDeploymentValidationError(
            "비활성화된 Deployment는 호출할 수 없습니다"
        )


__all__ = [
    "BackendDeploymentValidationError",
    "validate_runnable_deployment",
]
