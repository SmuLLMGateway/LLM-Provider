"""Deployment 전용 Registry Pydantic 모델의 계약을 검증합니다."""

import pytest
from pydantic import ValidationError

from app.schemas.registry import (
    DeploymentConfig,
    LlmDeploymentRegistryFile,
    NerDeploymentRegistryFile,
    RegistryConfig,
)


def _registry_data() -> dict[str, object]:
    """NER와 OpenAI 호환 LLM을 포함한 Registry 입력을 만듭니다."""

    return {
        "deployments": {
            "ner-local-a": {
                "kind": "ner",
                "adapterType": "mock",
                "enabled": True,
            },
            "llm-local-a": {
                "kind": "llm",
                "adapterType": "openai_compatible",
                "baseUrl": "http://localhost:9000/v1",
                "modelName": "model-a",
                "timeoutMs": 5000,
                "enabled": True,
            },
        }
    }


def test_registry_config_parses_deployment_map() -> None:
    """RegistryConfig가 별도 Profile 없이 Deployment만 조립합니다."""

    registry = RegistryConfig.model_validate(_registry_data())

    assert set(registry.deployments) == {
        "ner-local-a",
        "llm-local-a",
    }
    assert registry.deployments["ner-local-a"].kind == "ner"
    assert (
        registry.deployments["llm-local-a"].adapter_type
        == "openai_compatible"
    )


def _file_entry(kind: str) -> dict[str, object]:
    """종류별 물리 파일의 최소 정상 항목을 반환합니다."""

    if kind == "ner":
        return {
            "baseUrl": "http://localhost:8008/v1/ner/detect",
            "timeoutMs": 5000,
            "enabled": True,
        }
    return {"adapterType": "mock", "enabled": True}


def test_ner_registry_file_uses_raw_object_map_without_adapter() -> None:
    """NER 파일은 래퍼와 adapterType 없는 공통 서버 설정만 받습니다."""

    parsed = NerDeploymentRegistryFile.model_validate(
        {"ner-local-a": _file_entry("ner")}
    )

    assert parsed.root["ner-local-a"].base_url == (
        "http://localhost:8008/v1/ner/detect"
    )


@pytest.mark.parametrize(
    ("model_type", "kind"),
    [
        (NerDeploymentRegistryFile, "ner"),
        (LlmDeploymentRegistryFile, "llm"),
    ],
)
def test_kind_specific_registry_file_injects_runtime_kind(
    model_type: (
        type[NerDeploymentRegistryFile]
        | type[LlmDeploymentRegistryFile]
    ),
    kind: str,
) -> None:
    """kind 없는 파일 항목을 파일명에 해당하는 내부 kind로 변환합니다."""

    parsed = model_type.model_validate(
        {f"{kind}-a": _file_entry(kind)}
    )
    runtime = parsed.to_runtime_map()
    dumped = parsed.model_dump(
        by_alias=True,
        mode="json",
    )

    assert runtime[f"{kind}-a"].kind == kind
    assert "kind" not in dumped[f"{kind}-a"]
    if kind == "ner":
        assert runtime[f"{kind}-a"].adapter_type == "http_ner"
        assert "adapterType" not in dumped[f"{kind}-a"]


@pytest.mark.parametrize(
    ("model_type", "explicit_kind"),
    [
        (NerDeploymentRegistryFile, "ner"),
        (NerDeploymentRegistryFile, "llm"),
        (LlmDeploymentRegistryFile, "llm"),
        (LlmDeploymentRegistryFile, "ner"),
    ],
)
def test_kind_specific_registry_file_rejects_explicit_kind(
    model_type: (
        type[NerDeploymentRegistryFile]
        | type[LlmDeploymentRegistryFile]
    ),
    explicit_kind: str,
) -> None:
    """물리 파일의 kind는 파일명과 값이 일치하더라도 거부합니다."""

    with pytest.raises(ValidationError):
        entry = _file_entry(
            "ner" if model_type is NerDeploymentRegistryFile else "llm"
        )
        entry["kind"] = explicit_kind
        model_type.model_validate(
            {"explicit-kind": entry}
        )


@pytest.mark.parametrize(
    ("model_type", "runtime_kind"),
    [
        (NerDeploymentRegistryFile, "llm"),
        (LlmDeploymentRegistryFile, "ner"),
    ],
)
def test_kind_specific_registry_file_rejects_runtime_kind_mismatch(
    model_type: (
        type[NerDeploymentRegistryFile]
        | type[LlmDeploymentRegistryFile]
    ),
    runtime_kind: str,
) -> None:
    """반대 종류의 내부 Deployment를 잘못된 물리 파일로 직렬화하지 않습니다."""

    deployment = DeploymentConfig.model_validate(
        {
            "kind": runtime_kind,
            "adapterType": "mock",
            "enabled": True,
        }
    )

    with pytest.raises(ValueError):
        model_type.from_runtime_map(
            {"runtime-a": deployment}
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("adapterType", "http_ner"),
        ("modelName", "gliner"),
    ],
)
def test_ner_registry_file_rejects_backend_selection_fields(
    field_name: str,
    value: object,
) -> None:
    """NER 물리 설정에서는 Adapter와 모델 선택을 허용하지 않습니다."""

    entry = _file_entry("ner")
    entry[field_name] = value

    with pytest.raises(ValidationError):
        NerDeploymentRegistryFile.model_validate({"ner-a": entry})


def test_ner_registry_file_rejects_nonstandard_runtime_backend() -> None:
    """다른 NER Adapter를 저장하면서 http_ner로 조용히 바꾸지 않습니다."""

    deployment = DeploymentConfig.model_validate(
        {
            "kind": "ner",
            "adapterType": "gliner_http",
            "baseUrl": "http://localhost:8008/ner",
            "timeoutMs": 5000,
            "enabled": True,
        }
    )

    with pytest.raises(ValueError, match="공통 HTTP NER"):
        NerDeploymentRegistryFile.from_runtime_map({"ner-a": deployment})


def test_registry_dump_uses_json_aliases() -> None:
    """저장용 직렬화가 camelCase 필드명을 사용합니다."""

    dumped = RegistryConfig.model_validate(_registry_data()).model_dump(
        by_alias=True,
        mode="json",
        exclude_none=True,
    )

    llm = dumped["deployments"]["llm-local-a"]
    assert llm["adapterType"] == "openai_compatible"
    assert llm["baseUrl"] == "http://localhost:9000/v1"
    assert llm["modelName"] == "model-a"
    assert llm["timeoutMs"] == 5000


def test_llm_registry_round_trips_context_limit() -> None:
    """LLM 컨텍스트 한도를 camelCase 저장 계약으로 보존합니다."""

    deployment = DeploymentConfig.model_validate(
        {
            "kind": "llm",
            "adapterType": "openai_compatible",
            "baseUrl": "http://localhost:9000/v1",
            "modelName": "model-a",
            "timeoutMs": 5000,
            "contextWindowTokens": 32768,
            "enabled": True,
        }
    )

    stored = LlmDeploymentRegistryFile.from_runtime_map(
        {"llm-a": deployment}
    )
    restored = stored.to_runtime_map()["llm-a"]

    assert restored.context_window_tokens == 32768
    assert stored.model_dump(by_alias=True, mode="json") == {
        "llm-a": {
            "adapterType": "openai_compatible",
            "enabled": True,
            "baseUrl": "http://localhost:9000/v1",
            "modelName": "model-a",
            "timeoutMs": 5000,
            "contextWindowTokens": 32768,
        }
    }


@pytest.mark.parametrize("context_tokens", [0, True, 1.5, 100_000_001])
def test_llm_deployment_rejects_invalid_context_limits(
    context_tokens: object,
) -> None:
    """허용 범위의 엄격한 양의 정수 컨텍스트만 저장합니다."""

    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate(
            {
                "kind": "llm",
                "adapterType": "openai_compatible",
                "baseUrl": "http://localhost:9000/v1",
                "modelName": "model-a",
                "timeoutMs": 5000,
                "contextWindowTokens": context_tokens,
                "enabled": True,
            }
        )


def test_deployment_rejects_removed_model_info() -> None:
    """제거된 modelInfo 필드는 기존 형식이어도 명시적으로 거부합니다."""

    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate(
            {
                "kind": "ner",
                "adapterType": "mock",
                "modelInfo": {
                    "displayName": "개인정보 NER",
                    "description": "이 필드는 더 이상 지원하지 않습니다.",
                },
                "enabled": True,
            }
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("adapterConfig", {}),
        ("adapterConfig", None),
        ("adapter_config", {}),
    ],
)
def test_deployment_rejects_removed_adapter_config(
    field_name: str,
    value: object,
) -> None:
    """제거된 Adapter 전용 설정 필드를 별칭과 값에 관계없이 거부합니다."""

    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate(
            {
                "kind": "ner",
                "adapterType": "mock",
                field_name: value,
                "enabled": True,
            }
        )


def test_profiles_field_is_rejected() -> None:
    """제거된 Profile 맵을 RegistryConfig에 다시 넣지 못하게 합니다."""

    data = _registry_data()
    data["profiles"] = {}

    with pytest.raises(ValidationError):
        RegistryConfig.model_validate(data)


def test_registry_rejects_unknown_top_level_field() -> None:
    """알 수 없는 Registry 최상위 필드를 무시하지 않습니다."""

    data = _registry_data()
    data["revision"] = 1

    with pytest.raises(ValidationError):
        RegistryConfig.model_validate(data)


def test_deployment_rejects_unknown_field() -> None:
    """알 수 없는 Deployment 설정을 Adapter로 전달하지 않습니다."""

    data = {
        "kind": "ner",
        "adapterType": "mock",
        "enabled": True,
        "unexpected": "value",
    }

    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate(data)


@pytest.mark.parametrize("resource_id", ["", "Profile A", "UPPER", "_start"])
def test_deployment_map_rejects_invalid_resource_id(
    resource_id: str,
) -> None:
    """Deployment 키에 공통 ResourceId 문법을 적용합니다."""

    with pytest.raises(ValidationError):
        NerDeploymentRegistryFile.model_validate(
            {
                resource_id: _file_entry("ner")
            }
        )


@pytest.mark.parametrize(
    "url",
    ["http://", "localhost:9000", "ftp://localhost/model"],
)
def test_deployment_rejects_invalid_http_base_url(url: str) -> None:
    """LLM Endpoint에는 host가 있는 HTTP(S) URL만 허용합니다."""

    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate(
            {
                "kind": "llm",
                "adapterType": "openai_compatible",
                "baseUrl": url,
                "modelName": "model-a",
                "timeoutMs": 5000,
                "enabled": True,
            }
        )


@pytest.mark.parametrize("timeout_ms", [0, 300_001, True, 1.5])
def test_deployment_rejects_invalid_timeout(timeout_ms: object) -> None:
    """Timeout 범위와 엄격한 정수 자료형을 검증합니다."""

    with pytest.raises(ValidationError):
        DeploymentConfig.model_validate(
            {
                "kind": "llm",
                "adapterType": "openai_compatible",
                "baseUrl": "http://localhost:9000",
                "modelName": "model-a",
                "timeoutMs": timeout_ms,
                "enabled": True,
            }
        )


def test_deployment_model_is_frozen() -> None:
    """검증된 Deployment 필드를 직접 변경할 수 없습니다."""

    deployment = DeploymentConfig.model_validate(
        {
            "kind": "ner",
            "adapterType": "mock",
            "enabled": True,
        }
    )

    with pytest.raises(ValidationError):
        deployment.enabled = False


def test_registry_model_is_frozen() -> None:
    """RegistryConfig 필드 자체를 다른 맵으로 교체할 수 없습니다."""

    registry = RegistryConfig.model_validate(_registry_data())

    with pytest.raises(ValidationError):
        registry.deployments = {}
