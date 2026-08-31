"""BackendRegistry의 Adapter 등록과 Deployment 계약 검증을 확인합니다."""

from dataclasses import FrozenInstanceError

import pytest

from app.backends.backend_registry import (
    BackendRegistration,
    BackendRegistry,
    BackendValidationError,
    DuplicateBackendRegistrationError,
    create_default_backend_registry,
)
from app.registry.validator import RegistryValidator
from app.schemas.registry import DeploymentConfig, RegistryConfig


def _deployment(
    *,
    kind: str = "llm",
    adapter_type: str = "custom_http",
    base_url: str | None = "http://localhost:9000",
    model_name: str | None = "model-a",
    timeout_ms: int | None = 5000,
) -> DeploymentConfig:
    """테스트에 필요한 Deployment 설정을 생성합니다."""

    data: dict[str, object] = {
        "kind": kind,
        "adapterType": adapter_type,
        "enabled": True,
    }
    if base_url is not None:
        data["baseUrl"] = base_url
    if model_name is not None:
        data["modelName"] = model_name
    if timeout_ms is not None:
        data["timeoutMs"] = timeout_ms
    return DeploymentConfig.model_validate(data)


def test_registered_backend_can_be_required() -> None:
    """등록한 kind와 adapterType 조합으로 같은 계약을 조회합니다."""

    registration = BackendRegistration(
        adapter_type="custom_http",
        kind="llm",
        required_fields=frozenset({"base_url"}),
    )
    registry = BackendRegistry([registration])

    resolved = registry.require(
        deployment_id="llm-custom-a",
        adapter_type="custom_http",
        kind="llm",
    )

    assert resolved is registration


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:9000/v1?token=secret",
        "http://localhost:9000/v1#fragment",
        "http://user:password@localhost:9000/v1",
    ],
)
def test_registry_rejects_base_url_that_cannot_be_safely_exposed(
    base_url: str,
) -> None:
    """모든 Adapter의 baseUrl에서 query, fragment와 인증정보를 거부합니다."""

    registry = BackendRegistry(
        [
            BackendRegistration(
                adapter_type="custom_http",
                kind="llm",
                required_fields=frozenset({"base_url"}),
            )
        ]
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment(
            "llm-custom-a",
            _deployment(base_url=base_url),
        )

    assert error_info.value.code == "ADAPTER_CONFIG_INVALID"
    assert "secret" not in str(error_info.value)
    assert "password" not in str(error_info.value)


def test_duplicate_backend_registration_is_rejected() -> None:
    """같은 kind와 adapterType 계약을 중복 등록하지 못하게 합니다."""

    first = BackendRegistration(
        adapter_type="custom_http",
        kind="llm",
    )
    second = BackendRegistration(
        adapter_type="custom_http",
        kind="llm",
    )

    with pytest.raises(DuplicateBackendRegistrationError) as error_info:
        BackendRegistry([first, second])

    assert error_info.value.adapter_type == "custom_http"
    assert error_info.value.kind == "llm"


def test_unregistered_adapter_is_rejected() -> None:
    """어떤 kind에도 등록되지 않은 Adapter를 구분하여 거부합니다."""

    registry = BackendRegistry()

    with pytest.raises(BackendValidationError) as error_info:
        registry.require(
            deployment_id="llm-unknown-a",
            adapter_type="unknown_adapter",
            kind="llm",
        )

    assert error_info.value.code == "ADAPTER_NOT_REGISTERED"
    assert error_info.value.deployment_id == "llm-unknown-a"
    assert error_info.value.adapter_type == "unknown_adapter"
    assert error_info.value.kind == "llm"


def test_registered_adapter_with_wrong_kind_is_rejected() -> None:
    """Adapter가 등록됐더라도 지원하지 않는 Deployment kind를 거부합니다."""

    registry = BackendRegistry(
        [
            BackendRegistration(
                adapter_type="http_ner",
                kind="ner",
            )
        ]
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.require(
            deployment_id="llm-http-a",
            adapter_type="http_ner",
            kind="llm",
        )

    assert error_info.value.code == "ADAPTER_KIND_MISMATCH"
    assert error_info.value.field_names == ()


def test_required_deployment_fields_are_validated() -> None:
    """Adapter 계약에 지정된 필수 Deployment 필드 누락을 모두 보고합니다."""

    registry = BackendRegistry(
        [
            BackendRegistration(
                adapter_type="custom_http",
                kind="llm",
                required_fields=frozenset(
                    {"base_url", "model_name", "timeout_ms"}
                ),
            )
        ]
    )
    deployment = _deployment(
        base_url=None,
        model_name=None,
        timeout_ms=None,
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment("llm-custom-a", deployment)

    assert error_info.value.code == "ADAPTER_REQUIRED_FIELD_MISSING"
    assert error_info.value.field_names == (
        "base_url",
        "model_name",
        "timeout_ms",
    )


def test_forbidden_deployment_fields_are_validated() -> None:
    """Adapter 계약에서 금지한 Deployment 필드 입력을 거부합니다."""

    registry = BackendRegistry(
        [
            BackendRegistration(
                adapter_type="inprocess_ner",
                kind="ner",
                forbidden_fields=frozenset(
                    {"base_url", "timeout_ms"}
                ),
            )
        ]
    )
    deployment = _deployment(
        kind="ner",
        adapter_type="inprocess_ner",
        model_name="ner-model-a",
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment("ner-local-a", deployment)

    assert error_info.value.code == "ADAPTER_FIELD_NOT_ALLOWED"
    assert error_info.value.field_names == ("base_url", "timeout_ms")


def test_custom_validator_value_error_is_wrapped() -> None:
    """사용자 정의 계약의 ValueError를 공통 Backend 오류로 감쌉니다."""

    def reject_model(
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> None:
        """허용하지 않는 모델 이름을 사용자 정의 규칙으로 거부합니다."""

        assert deployment_id == "llm-custom-a"
        if deployment.model_name == "model-a":
            raise ValueError("model-a는 사용할 수 없습니다")

    registry = BackendRegistry(
        [
            BackendRegistration(
                adapter_type="custom_http",
                kind="llm",
                config_validator=reject_model,
            )
        ]
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment(
            "llm-custom-a",
            _deployment(),
        )

    assert error_info.value.code == "ADAPTER_CONFIG_INVALID"
    assert error_info.value.detail == "model-a는 사용할 수 없습니다"
    assert isinstance(error_info.value.__cause__, ValueError)


def test_default_registry_contains_and_validates_builtin_adapters() -> None:
    """기본 Registry는 단일 NER 계약과 기본 LLM 계약만 제공합니다."""

    registry = create_default_backend_registry()

    assert set(registry.registrations) == {
        ("ner", "mock"),
        ("ner", "http_ner"),
        ("llm", "mock"),
        ("llm", "openai_compatible"),
    }
    registry.validate_deployment(
        "ner-mock-a",
        _deployment(
            kind="ner",
            adapter_type="mock",
            base_url=None,
            model_name=None,
            timeout_ms=None,
        ),
    )
    registry.validate_deployment(
        "ner-http-a",
        _deployment(
            kind="ner",
            adapter_type="http_ner",
            base_url="http://localhost:9100/v1/ner/detect",
            model_name=None,
            timeout_ms=5000,
        ),
    )
    registry.validate_deployment(
        "llm-mock-a",
        _deployment(
            adapter_type="mock",
            base_url=None,
            model_name=None,
            timeout_ms=None,
        ),
    )
    registry.validate_deployment(
        "llm-openai-a",
        _deployment(adapter_type="openai_compatible"),
    )


@pytest.mark.parametrize(
    ("missing_field", "base_url", "timeout_ms"),
    [
        ("base_url", None, 5000),
        (
            "timeout_ms",
            "http://localhost:9100/v1/ner/detect",
            None,
        ),
    ],
)
def test_default_http_ner_contract_requires_server_fields(
    missing_field: str,
    base_url: str | None,
    timeout_ms: int | None,
) -> None:
    """HTTP NER 계약은 전체 Endpoint와 제한시간을 필수로 요구합니다."""

    registry = create_default_backend_registry()
    deployment = _deployment(
        kind="ner",
        adapter_type="http_ner",
        base_url=base_url,
        model_name=None,
        timeout_ms=timeout_ms,
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment("ner-http-a", deployment)

    assert error_info.value.code == "ADAPTER_REQUIRED_FIELD_MISSING"
    assert error_info.value.field_names == (missing_field,)


def test_default_http_ner_contract_forbids_model_name() -> None:
    """공통 HTTP NER 요청에 사용하지 않는 modelName을 거부합니다."""

    registry = create_default_backend_registry()
    deployment = _deployment(
        kind="ner",
        adapter_type="http_ner",
        base_url="http://localhost:9100/v1/ner/detect",
        model_name="unused-model",
        timeout_ms=5000,
    )

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment("ner-http-a", deployment)

    assert error_info.value.code == "ADAPTER_FIELD_NOT_ALLOWED"
    assert error_info.value.field_names == ("model_name",)


@pytest.mark.parametrize(
    ("field_name", "deployment"),
    [
        (
            "base_url",
            _deployment(
                adapter_type="mock",
                base_url="http://localhost:9000",
                model_name=None,
                timeout_ms=None,
            ),
        ),
        (
            "model_name",
            _deployment(
                adapter_type="mock",
                base_url=None,
                model_name="mock-model",
                timeout_ms=None,
            ),
        ),
        (
            "timeout_ms",
            _deployment(
                adapter_type="mock",
                base_url=None,
                model_name=None,
                timeout_ms=5000,
            ),
        ),
    ],
)
def test_default_mock_llm_contract_forbids_server_fields(
    field_name: str,
    deployment: DeploymentConfig,
) -> None:
    """인메모리 Mock LLM에는 서버 연결 설정을 입력할 수 없습니다."""

    registry = create_default_backend_registry()

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment("llm-mock-a", deployment)

    assert error_info.value.code == "ADAPTER_FIELD_NOT_ALLOWED"
    assert error_info.value.field_names == (field_name,)


def test_registry_validator_accepts_registered_custom_adapter() -> None:
    """사용자 정의 Adapter 등록만으로 중심 스키마 수정 없이 전체 검증합니다."""

    backend_registry = create_default_backend_registry(
        [
            BackendRegistration(
                adapter_type="ollama",
                kind="llm",
                required_fields=frozenset(
                    {"base_url", "model_name", "timeout_ms"}
                ),
            )
        ]
    )
    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "ner-local-a": {
                    "kind": "ner",
                    "adapterType": "http_ner",
                    "baseUrl": (
                        "http://localhost:9100/v1/ner/detect"
                    ),
                    "timeoutMs": 5000,
                    "enabled": True,
                },
                "llm-ollama-a": {
                    "kind": "llm",
                    "adapterType": "ollama",
                    "baseUrl": "http://localhost:11434",
                    "modelName": "qwen3:8b",
                    "timeoutMs": 10000,
                    "enabled": True,
                },
            },
        }
    )

    RegistryValidator(backend_registry).validate_registry(registry)


def test_registrations_and_registration_objects_are_immutable() -> None:
    """외부에서 등록 Mapping과 개별 계약을 변경하지 못하게 합니다."""

    registration = BackendRegistration(
        adapter_type="custom_http",
        kind="llm",
    )
    source = [registration]
    registry = BackendRegistry(source)
    source.clear()

    assert registry.require(
        deployment_id="llm-custom-a",
        adapter_type="custom_http",
        kind="llm",
    ) is registration
    with pytest.raises(TypeError):
        registry.registrations[("llm", "other")] = registration  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        registration.adapter_type = "other"  # type: ignore[misc]
