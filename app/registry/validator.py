"""Registry Deployment의 Backend 설정 계약을 검증합니다."""

from __future__ import annotations

from app.backends.backend_registry import (
    BackendRegistry,
    create_default_backend_registry,
)
from app.schemas.registry import RegistryConfig


class RegistryValidator:
    """등록된 Deployment의 Adapter 설정 계약을 검증합니다."""

    def __init__(
        self,
        backend_registry: BackendRegistry | None = None,
    ) -> None:
        self.backend_registry = (
            backend_registry
            if backend_registry is not None
            else create_default_backend_registry()
        )

    def validate_registry(self, registry: RegistryConfig) -> None:
        """모든 Deployment의 Adapter 설정 계약을 검증합니다."""

        self.backend_registry.validate_registry(registry)


__all__ = [
    "RegistryValidator",
]
