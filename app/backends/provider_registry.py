"""Adapter 종류별 실제 Backend 구현체를 불변 Registry로 관리합니다."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

from pydantic import TypeAdapter, ValidationError

from app.backends.llm.base import LlmBackend
from app.backends.ner.base import NerBackend
from app.schemas.registry import (
    AdapterType,
    DeploymentConfig,
    DeploymentKind,
    RegistryConfig,
)

if TYPE_CHECKING:
    from app.backends.backend_registry import BackendRegistry


BackendProvider = NerBackend | LlmBackend
BackendProviderLookupErrorCode = Literal[
    "BACKEND_PROVIDER_NOT_REGISTERED",
    "BACKEND_PROVIDER_KIND_MISMATCH",
]

_ADAPTER_TYPE_ADAPTER = TypeAdapter(AdapterType)


class DuplicateBackendProviderRegistrationError(ValueError):
    """같은 kind와 adapterType 구현체가 두 번 등록되면 발생합니다."""

    def __init__(
        self,
        adapter_type: str,
        kind: DeploymentKind,
    ) -> None:
        self.adapter_type = adapter_type
        self.kind = kind
        super().__init__(
            "이미 등록된 Backend Provider입니다: "
            f"{kind}/{adapter_type}"
        )


class InvalidBackendProviderError(TypeError):
    """Provider가 선택한 kind의 공통 인터페이스를 제공하지 않으면 발생합니다."""

    def __init__(
        self,
        *,
        adapter_type: str,
        kind: DeploymentKind,
        required_method: str,
    ) -> None:
        self.adapter_type = adapter_type
        self.kind = kind
        self.required_method = required_method
        super().__init__(
            f"{kind}/{adapter_type} Provider에는 호출 가능한 "
            f"{required_method} 메서드가 필요합니다"
        )


class BackendProviderLookupError(LookupError):
    """Deployment를 실행할 Backend Provider를 찾을 수 없으면 발생합니다."""

    def __init__(
        self,
        code: BackendProviderLookupErrorCode,
        *,
        adapter_type: str,
        kind: DeploymentKind,
        deployment_id: str | None = None,
        available_kinds: Iterable[DeploymentKind] = (),
    ) -> None:
        self.code = code
        self.adapter_type = adapter_type
        self.kind = kind
        self.deployment_id = deployment_id
        self.available_kinds = tuple(sorted(available_kinds))

        target = (
            f"{deployment_id} ({kind}/{adapter_type})"
            if deployment_id is not None
            else f"{kind}/{adapter_type}"
        )
        message = f"{code}: {target}"
        if self.available_kinds:
            message = (
                f"{message}: available="
                f"{', '.join(self.available_kinds)}"
            )
        super().__init__(message)


class BackendProviderContractError(ValueError):
    """Provider에 대응하는 Deployment 설정 계약이 없으면 발생합니다."""

    def __init__(
        self,
        *,
        adapter_type: str,
        kind: DeploymentKind,
    ) -> None:
        self.adapter_type = adapter_type
        self.kind = kind
        super().__init__(
            "Backend Provider에 대응하는 설정 계약이 없습니다: "
            f"{kind}/{adapter_type}"
        )


@dataclass(frozen=True, slots=True)
class BackendProviderRegistration:
    """한 Adapter와 kind 조합에 연결할 실제 Backend 인스턴스입니다."""

    kind: DeploymentKind
    adapter_type: str
    provider: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """식별자와 Provider의 최소 실행 인터페이스를 검증합니다."""

        try:
            validated_adapter_type = (
                _ADAPTER_TYPE_ADAPTER.validate_python(
                    self.adapter_type,
                    strict=True,
                )
            )
        except ValidationError as error:
            raise ValueError(
                "유효하지 않은 Backend Provider adapterType입니다: "
                f"{self.adapter_type}"
            ) from error

        if self.kind not in {"ner", "llm"}:
            raise ValueError(
                f"지원하지 않는 Deployment kind입니다: {self.kind}"
            )

        required_method = (
            "detect" if self.kind == "ner" else "generate"
        )
        if (
            isinstance(self.provider, type)
            or not callable(
                getattr(self.provider, required_method, None)
            )
        ):
            raise InvalidBackendProviderError(
                adapter_type=validated_adapter_type,
                kind=self.kind,
                required_method=required_method,
            )

        object.__setattr__(
            self,
            "adapter_type",
            validated_adapter_type,
        )


class BackendProviderRegistry:
    """프로세스 수명 동안 재사용할 실제 Backend 구현체를 선택합니다."""

    def __init__(
        self,
        registrations: Iterable[
            BackendProviderRegistration
        ] = (),
    ) -> None:
        registration_map: dict[
            tuple[DeploymentKind, str],
            BackendProviderRegistration,
        ] = {}
        ner_backends: dict[str, NerBackend] = {}
        llm_backends: dict[str, LlmBackend] = {}
        kinds_by_adapter: dict[str, set[DeploymentKind]] = {}

        for registration in registrations:
            key = (registration.kind, registration.adapter_type)
            if key in registration_map:
                raise DuplicateBackendProviderRegistrationError(
                    registration.adapter_type,
                    registration.kind,
                )

            registration_map[key] = registration
            kinds_by_adapter.setdefault(
                registration.adapter_type,
                set(),
            ).add(registration.kind)
            if registration.kind == "ner":
                ner_backends[registration.adapter_type] = cast(
                    NerBackend,
                    registration.provider,
                )
            else:
                llm_backends[registration.adapter_type] = cast(
                    LlmBackend,
                    registration.provider,
                )

        self._registrations: Mapping[
            tuple[DeploymentKind, str],
            BackendProviderRegistration,
        ] = MappingProxyType(registration_map)
        self._ner_backends: Mapping[str, NerBackend] = MappingProxyType(
            ner_backends
        )
        self._llm_backends: Mapping[str, LlmBackend] = (
            MappingProxyType(llm_backends)
        )
        self._kinds_by_adapter: Mapping[
            str,
            frozenset[DeploymentKind],
        ] = MappingProxyType(
            {
                adapter_type: frozenset(kinds)
                for adapter_type, kinds in kinds_by_adapter.items()
            }
        )

    @property
    def registrations(
        self,
    ) -> Mapping[
        tuple[DeploymentKind, str],
        BackendProviderRegistration,
    ]:
        """등록된 Provider 정보를 읽기 전용 Mapping으로 반환합니다."""

        return self._registrations

    @property
    def ner_backends(self) -> Mapping[str, NerBackend]:
        """등록된 NER Backend의 읽기 전용 Mapping을 반환합니다."""

        return self._ner_backends

    @property
    def llm_backends(self) -> Mapping[str, LlmBackend]:
        """등록된 LLM Backend의 읽기 전용 Mapping을 반환합니다."""

        return self._llm_backends

    def require_ner(
        self,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> NerBackend:
        """NER Deployment에 대응하는 실제 Backend를 반환합니다."""

        if deployment.kind != "ner":
            raise BackendProviderLookupError(
                "BACKEND_PROVIDER_KIND_MISMATCH",
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind="ner",
                available_kinds=(deployment.kind,),
            )
        return cast(
            NerBackend,
            self._require(
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind="ner",
            ),
        )

    def require_llm(
        self,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> LlmBackend:
        """LLM Deployment에 대응하는 실제 Backend를 반환합니다."""

        if deployment.kind != "llm":
            raise BackendProviderLookupError(
                "BACKEND_PROVIDER_KIND_MISMATCH",
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind="llm",
                available_kinds=(deployment.kind,),
            )
        return cast(
            LlmBackend,
            self._require(
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind="llm",
            ),
        )

    def validate_contracts(
        self,
        backend_registry: BackendRegistry,
    ) -> None:
        """모든 Provider가 대응하는 Deployment 설정 계약을 갖는지 확인합니다."""

        for kind, adapter_type in self._registrations:
            if (kind, adapter_type) not in backend_registry.registrations:
                raise BackendProviderContractError(
                    adapter_type=adapter_type,
                    kind=kind,
                )

    def validate_deployments(
        self,
        deployments: Mapping[str, DeploymentConfig],
        *,
        enabled_only: bool = True,
    ) -> None:
        """실행 대상 Deployment에 대응하는 Provider가 모두 있는지 확인합니다."""

        for deployment_id, deployment in sorted(
            deployments.items()
        ):
            if enabled_only and not deployment.enabled:
                continue
            self._require(
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind=deployment.kind,
            )

    def validate_registry(
        self,
        registry: RegistryConfig,
        *,
        enabled_only: bool = True,
    ) -> None:
        """Registry의 실행 대상 Deployment Provider 가용성을 검증합니다."""

        self.validate_deployments(
            registry.deployments,
            enabled_only=enabled_only,
        )

    def _require(
        self,
        *,
        deployment_id: str,
        adapter_type: str,
        kind: DeploymentKind,
    ) -> BackendProvider:
        """등록 키로 Provider를 조회하고 실패 원인을 구분합니다."""

        registration = self._registrations.get(
            (kind, adapter_type)
        )
        if registration is not None:
            return cast(BackendProvider, registration.provider)

        available_kinds = self._kinds_by_adapter.get(adapter_type)
        if available_kinds is None:
            raise BackendProviderLookupError(
                "BACKEND_PROVIDER_NOT_REGISTERED",
                deployment_id=deployment_id,
                adapter_type=adapter_type,
                kind=kind,
            )

        raise BackendProviderLookupError(
            "BACKEND_PROVIDER_KIND_MISMATCH",
            deployment_id=deployment_id,
            adapter_type=adapter_type,
            kind=kind,
            available_kinds=available_kinds,
        )


__all__ = [
    "BackendProvider",
    "BackendProviderContractError",
    "BackendProviderLookupError",
    "BackendProviderLookupErrorCode",
    "BackendProviderRegistration",
    "BackendProviderRegistry",
    "DuplicateBackendProviderRegistrationError",
    "InvalidBackendProviderError",
]
