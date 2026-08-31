"""등록된 Backend Adapter 계약을 조회하는 서비스입니다."""

from __future__ import annotations

from app.backends.backend_registry import (
    BackendRegistry,
)
from app.backends.provider_registry import BackendProviderRegistry
from app.schemas.adapters import AdapterListResponse
from app.schemas.registry import DeploymentKind


class AdapterCatalogService:
    """설정 계약과 구현체가 모두 등록된 Adapter 이름을 조회합니다."""

    def __init__(
        self,
        backend_registry: BackendRegistry,
        backend_provider_registry: BackendProviderRegistry,
    ) -> None:
        self._backend_registry = backend_registry
        self._backend_provider_registry = backend_provider_registry

    def list_adapters(
        self,
        kind: DeploymentKind,
    ) -> AdapterListResponse:
        """지정한 종류에서 실제 선택 가능한 Adapter 이름을 반환합니다."""

        contract_keys = self._backend_registry.registrations
        provider_keys = self._backend_provider_registry.registrations
        adapter_types = sorted(
            adapter_type
            for registered_kind, adapter_type in contract_keys
            if registered_kind == kind
            and (registered_kind, adapter_type) in provider_keys
        )
        return AdapterListResponse(adapters=tuple(adapter_types))


__all__ = ["AdapterCatalogService"]
