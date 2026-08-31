"""Adapter별 Deployment 설정 계약과 등록 여부를 관리합니다."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

import httpx
from pydantic import TypeAdapter
from app.schemas.registry import (
    AdapterType,
    DeploymentConfig,
    DeploymentKind,
    RegistryConfig,
)


DeploymentField = Literal[
    "base_url",
    "model_name",
    "timeout_ms",
    "context_window_tokens",
]
DeploymentConfigValidator = Callable[[str, DeploymentConfig], None]
BackendValidationErrorCode = Literal[
    "ADAPTER_NOT_REGISTERED",
    "ADAPTER_KIND_MISMATCH",
    "ADAPTER_REQUIRED_FIELD_MISSING",
    "ADAPTER_FIELD_NOT_ALLOWED",
    "ADAPTER_CONFIG_INVALID",
]

_ADAPTER_TYPE_ADAPTER = TypeAdapter(AdapterType)
_DEPLOYMENT_FIELDS = frozenset(
    {
        "base_url",
        "model_name",
        "timeout_ms",
        "context_window_tokens",
    }
)


def _validate_http_endpoint_config(
    deployment_id: str,
    deployment: DeploymentConfig,
) -> None:
    """공개 가능한 고정 HTTP Endpoint URL인지 공통 검증합니다."""

    del deployment_id
    if deployment.base_url is None:
        return

    endpoint = httpx.URL(deployment.base_url)
    if endpoint.query or endpoint.fragment:
        raise ValueError(
            "baseUrl에는 query나 fragment를 사용할 수 없습니다"
        )
    if endpoint.username or endpoint.password:
        raise ValueError(
            "baseUrl에 인증정보를 포함할 수 없습니다"
        )


class DuplicateBackendRegistrationError(ValueError):
    """같은 kind와 adapterType 계약이 두 번 등록되면 발생합니다."""

    def __init__(
        self,
        adapter_type: str,
        kind: DeploymentKind,
    ) -> None:
        self.adapter_type = adapter_type
        self.kind = kind
        super().__init__(
            "이미 등록된 Backend 계약입니다: "
            f"{kind}/{adapter_type}"
        )


class BackendValidationError(ValueError):
    """Deployment가 등록된 Adapter 계약을 만족하지 않으면 발생합니다."""

    def __init__(
        self,
        code: BackendValidationErrorCode,
        *,
        deployment_id: str,
        adapter_type: str,
        kind: DeploymentKind,
        field_names: Iterable[str] = (),
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.deployment_id = deployment_id
        self.adapter_type = adapter_type
        self.kind = kind
        self.field_names = tuple(sorted(field_names))
        self.detail = detail

        message = (
            f"{code}: {deployment_id} "
            f"({kind}/{adapter_type})"
        )
        if self.field_names:
            message = f"{message}: {', '.join(self.field_names)}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BackendRegistration:
    """한 Adapter와 kind 조합의 Deployment 설정 계약입니다."""

    adapter_type: str
    kind: DeploymentKind
    required_fields: frozenset[DeploymentField] = frozenset()
    forbidden_fields: frozenset[DeploymentField] = frozenset()
    config_validator: DeploymentConfigValidator | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """등록 식별자와 필드 계약을 검증하고 불변 집합으로 복사합니다."""

        validated_adapter_type = _ADAPTER_TYPE_ADAPTER.validate_python(
            self.adapter_type,
            strict=True,
        )
        if self.kind not in {"ner", "llm"}:
            raise ValueError(
                f"지원하지 않는 Deployment kind입니다: {self.kind}"
            )

        required_fields = frozenset(self.required_fields)
        forbidden_fields = frozenset(self.forbidden_fields)
        unknown_fields = (
            required_fields | forbidden_fields
        ) - _DEPLOYMENT_FIELDS
        if unknown_fields:
            raise ValueError(
                "알 수 없는 Deployment 계약 필드입니다: "
                f"{', '.join(sorted(unknown_fields))}"
            )

        overlapping_fields = required_fields & forbidden_fields
        if overlapping_fields:
            raise ValueError(
                "같은 Deployment 필드를 필수이면서 금지할 수 없습니다: "
                f"{', '.join(sorted(overlapping_fields))}"
            )
        if (
            self.config_validator is not None
            and not callable(self.config_validator)
        ):
            raise TypeError("config_validator는 callable이어야 합니다.")

        object.__setattr__(
            self,
            "adapter_type",
            validated_adapter_type,
        )
        object.__setattr__(
            self,
            "required_fields",
            required_fields,
        )
        object.__setattr__(
            self,
            "forbidden_fields",
            forbidden_fields,
        )


class BackendRegistry:
    """불변 Adapter 계약 맵으로 모든 Deployment를 검증합니다."""

    def __init__(
        self,
        registrations: Iterable[BackendRegistration] = (),
    ) -> None:
        registration_map: dict[
            tuple[DeploymentKind, str],
            BackendRegistration,
        ] = {}
        kinds_by_adapter: dict[str, set[DeploymentKind]] = {}

        for registration in registrations:
            key = (registration.kind, registration.adapter_type)
            if key in registration_map:
                raise DuplicateBackendRegistrationError(
                    registration.adapter_type,
                    registration.kind,
                )
            registration_map[key] = registration
            kinds_by_adapter.setdefault(
                registration.adapter_type,
                set(),
            ).add(registration.kind)

        self._registrations: Mapping[
            tuple[DeploymentKind, str],
            BackendRegistration,
        ] = MappingProxyType(registration_map)
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
    ) -> Mapping[tuple[DeploymentKind, str], BackendRegistration]:
        """등록된 Adapter 계약의 읽기 전용 Mapping을 반환합니다."""

        return self._registrations

    def require(
        self,
        *,
        deployment_id: str,
        adapter_type: str,
        kind: DeploymentKind,
    ) -> BackendRegistration:
        """Deployment와 일치하는 Adapter 계약을 반환합니다."""

        registration = self._registrations.get((kind, adapter_type))
        if registration is not None:
            return registration

        if adapter_type not in self._kinds_by_adapter:
            raise BackendValidationError(
                "ADAPTER_NOT_REGISTERED",
                deployment_id=deployment_id,
                adapter_type=adapter_type,
                kind=kind,
            )

        raise BackendValidationError(
            "ADAPTER_KIND_MISMATCH",
            deployment_id=deployment_id,
            adapter_type=adapter_type,
            kind=kind,
        )

    def validate_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> None:
        """등록 여부, kind와 Adapter별 필드 계약을 검증합니다."""

        registration = self.require(
            deployment_id=deployment_id,
            adapter_type=deployment.adapter_type,
            kind=deployment.kind,
        )
        missing_fields = {
            field_name
            for field_name in registration.required_fields
            if getattr(deployment, field_name) is None
        }
        if missing_fields:
            raise BackendValidationError(
                "ADAPTER_REQUIRED_FIELD_MISSING",
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind=deployment.kind,
                field_names=missing_fields,
            )

        forbidden_fields = (
            registration.forbidden_fields
            & deployment.model_fields_set
        )
        if forbidden_fields:
            raise BackendValidationError(
                "ADAPTER_FIELD_NOT_ALLOWED",
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind=deployment.kind,
                field_names=forbidden_fields,
            )

        try:
            _validate_http_endpoint_config(
                deployment_id,
                deployment,
            )
            if registration.config_validator is not None:
                registration.config_validator(
                    deployment_id,
                    deployment,
                )
        except BackendValidationError:
            raise
        except ValueError as error:
            raise BackendValidationError(
                "ADAPTER_CONFIG_INVALID",
                deployment_id=deployment_id,
                adapter_type=deployment.adapter_type,
                kind=deployment.kind,
                detail=str(error),
            ) from error

    def validate_registry(self, registry: RegistryConfig) -> None:
        """참조 여부와 관계없이 등록된 모든 Deployment를 검증합니다."""

        for deployment_id, deployment in sorted(
            registry.deployments.items()
        ):
            self.validate_deployment(deployment_id, deployment)


DEFAULT_BACKEND_REGISTRATIONS = (
    # 단위 테스트 조립용 계약입니다. NER 파일·관리 API와 실제 Runtime은
    # 이 키를 저장하거나 Provider로 등록하지 않습니다.
    BackendRegistration(
        adapter_type="mock",
        kind="ner",
        forbidden_fields=frozenset(
            {
                "base_url",
                "model_name",
                "timeout_ms",
                "context_window_tokens",
            }
        ),
    ),
    BackendRegistration(
        adapter_type="mock",
        kind="llm",
        forbidden_fields=frozenset(
            {
                "base_url",
                "model_name",
                "timeout_ms",
            }
        ),
    ),
    BackendRegistration(
        adapter_type="http_ner",
        kind="ner",
        required_fields=frozenset(
            {"base_url", "timeout_ms"}
        ),
        forbidden_fields=frozenset(
            {
                "model_name",
                "context_window_tokens",
            }
        ),
    ),
    BackendRegistration(
        adapter_type="openai_compatible",
        kind="llm",
        required_fields=frozenset(
            {"base_url", "model_name", "timeout_ms"}
        ),
    ),
)


def create_default_backend_registry(
    extra_registrations: Iterable[BackendRegistration] = (),
) -> BackendRegistry:
    """기본 계약과 추가 Adapter 계약을 포함한 새 Registry를 만듭니다."""

    return BackendRegistry(
        (*DEFAULT_BACKEND_REGISTRATIONS, *tuple(extra_registrations))
    )


__all__ = [
    "BackendRegistration",
    "BackendRegistry",
    "BackendValidationError",
    "BackendValidationErrorCode",
    "DEFAULT_BACKEND_REGISTRATIONS",
    "DeploymentConfigValidator",
    "DeploymentField",
    "DeploymentKind",
    "DuplicateBackendRegistrationError",
    "create_default_backend_registry",
]
