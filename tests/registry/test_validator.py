"""RegistryValidator의 Deployment Backend 계약 검증을 확인합니다."""

import pytest

from app.backends.backend_registry import (
    BackendRegistration,
    BackendValidationError,
    create_default_backend_registry,
)
from app.registry.validator import RegistryValidator
from app.schemas.registry import RegistryConfig


def test_validator_accepts_builtin_deployments() -> None:
    """기본 NER Adapter와 OpenAI 호환 LLM 설정을 검증합니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "ner-a": {
                    "kind": "ner",
                    "adapterType": "mock",
                    "enabled": True,
                },
                "ner-http-a": {
                    "kind": "ner",
                    "adapterType": "http_ner",
                    "baseUrl": "http://localhost:9100/v1/ner/detect",
                    "timeoutMs": 5000,
                    "enabled": True,
                },
                "llm-a": {
                    "kind": "llm",
                    "adapterType": "openai_compatible",
                    "baseUrl": "http://localhost:9000/v1",
                    "modelName": "model-a",
                    "timeoutMs": 5000,
                    "enabled": True,
                },
            }
        }
    )

    RegistryValidator().validate_registry(registry)


def test_validator_rejects_unknown_adapter() -> None:
    """계약이 등록되지 않은 Adapter의 Deployment를 거부합니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "llm-a": {
                    "kind": "llm",
                    "adapterType": "unknown",
                    "enabled": True,
                }
            }
        }
    )

    with pytest.raises(BackendValidationError) as error_info:
        RegistryValidator().validate_registry(registry)

    assert error_info.value.code == "ADAPTER_NOT_REGISTERED"
    assert error_info.value.deployment_id == "llm-a"


def test_validator_checks_all_deployments_without_profile_references() -> None:
    """요청 참조와 무관하게 등록 파일의 모든 Deployment를 검증합니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "invalid-unused": {
                    "kind": "llm",
                    "adapterType": "openai_compatible",
                    "enabled": False,
                }
            }
        }
    )

    with pytest.raises(BackendValidationError) as error_info:
        RegistryValidator().validate_registry(registry)

    assert error_info.value.code == "ADAPTER_REQUIRED_FIELD_MISSING"
    assert error_info.value.deployment_id == "invalid-unused"


def test_validator_accepts_registered_custom_adapter() -> None:
    """중심 스키마 변경 없이 추가한 Adapter 계약을 사용할 수 있습니다."""

    backend_registry = create_default_backend_registry(
        [
            BackendRegistration(
                kind="llm",
                adapter_type="ollama",
                required_fields=frozenset(
                    {"base_url", "model_name", "timeout_ms"}
                ),
            )
        ]
    )
    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "ollama-a": {
                    "kind": "llm",
                    "adapterType": "ollama",
                    "baseUrl": "http://localhost:11434",
                    "modelName": "qwen3:8b",
                    "timeoutMs": 10000,
                    "enabled": True,
                }
            }
        }
    )

    RegistryValidator(backend_registry).validate_registry(registry)
