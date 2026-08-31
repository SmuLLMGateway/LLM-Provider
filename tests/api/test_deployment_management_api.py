"""Deployment 추가·전체 수정 HTTP API 계약을 검증합니다."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_deployment_management
from app.backends.backend_registry import (
    BackendValidationError,
    BackendValidationErrorCode,
)
from app.backends.provider_registry import BackendProviderRegistry
from app.core.application_runtime import (
    ApplicationRuntime,
    application_runtime,
)
from app.main import create_app
from app.prompts.policy_prompt_loader import (
    DEFAULT_POLICY_PROMPTS_PATH,
    POLICY_PROMPTS_FILENAME,
)
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.registry.mutator import (
    DeploymentAlreadyExistsError,
    DeploymentMustBeDisabledError,
    DeploymentNotFoundError,
)
from app.schemas.registry import DeploymentConfig
from app.services.adapter_catalog import AdapterCatalogService
from app.services.deployment_management import (
    DeploymentActivationError,
    DeploymentManagementService,
    DeploymentRollbackError,
)
from app.services.detection_pipeline import DetectionPipeline
from app.services.generation_pipeline import GenerationPipeline
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


@dataclass(slots=True)
class RecordingDeploymentManagementService:
    """관리 API가 전달한 작업과 Deployment 설정을 기록합니다."""

    error: Exception | None = None
    calls: list[tuple[str, str, DeploymentConfig]] = field(
        default_factory=list
    )
    delete_calls: list[tuple[str, str]] = field(default_factory=list)

    def add_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """추가 인자를 기록한 뒤 결과 또는 예약된 예외를 반환합니다."""

        return self._record("add", deployment_id, deployment)

    def update_deployment(
        self,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """수정 인자를 기록한 뒤 결과 또는 예약된 예외를 반환합니다."""

        return self._record("update", deployment_id, deployment)

    def set_deployment_enabled(
        self,
        deployment_id: str,
        *,
        kind: str,
        enabled: bool,
    ) -> DeploymentConfig:
        """활성 상태 변경 인자와 변경 뒤 전체 설정을 기록합니다."""

        deployment: dict[str, object]
        if kind == "ner":
            deployment = {
                "kind": "ner",
                "adapterType": "http_ner",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": enabled,
            }
        else:
            deployment = {
                "kind": "llm",
                "adapterType": "openai_compatible",
                "baseUrl": "http://10.0.0.8:11434/v1",
                "modelName": "qwen3:8b",
                "timeoutMs": 10000,
                "enabled": enabled,
            }
        return self._record("set_enabled", deployment_id, deployment)

    def delete_deployment(
        self,
        deployment_id: str,
        *,
        kind: str,
    ) -> None:
        """삭제할 ID와 경로 종류를 기록하고 예약된 오류를 전달합니다."""

        self.delete_calls.append((deployment_id, kind))
        if self.error is not None:
            raise self.error

    def _record(
        self,
        operation: str,
        deployment_id: str,
        deployment: DeploymentConfig | dict[str, object],
    ) -> DeploymentConfig:
        """내부 설정 모델로 통일해서 호출을 기록합니다."""

        validated = (
            deployment
            if isinstance(deployment, DeploymentConfig)
            else DeploymentConfig.model_validate(deployment)
        )
        self.calls.append((operation, deployment_id, validated))
        if self.error is not None:
            raise self.error
        return validated


@asynccontextmanager
async def _runtime_factory() -> AsyncIterator[ApplicationRuntime]:
    """의존성 교체가 가능한 최소 테스트 Runtime을 제공합니다."""

    yield ApplicationRuntime(
        http_client=cast(httpx.AsyncClient, object()),
        registry_manager=cast(RegistryManager, object()),
        backend_providers=cast(BackendProviderRegistry, object()),
        adapter_catalog_service=cast(AdapterCatalogService, object()),
        detection_pipeline=cast(DetectionPipeline, object()),
        generation_pipeline=cast(GenerationPipeline, object()),
        masking_pipeline=object(),  # type: ignore[arg-type]
        title_generation_pipeline=cast(
            TitleGenerationPipeline,
            object(),
        ),
        deployment_management_service=cast(
            DeploymentManagementService,
            object(),
        ),
    )


def _request(
    method: str,
    path: str,
    payload: object | None,
    service: RecordingDeploymentManagementService,
) -> httpx.Response:
    """주입한 관리 서비스를 사용하는 HTTP 요청을 보냅니다."""

    application = create_app(runtime_factory=_runtime_factory)
    application.dependency_overrides[get_deployment_management] = (
        lambda: cast(DeploymentManagementService, service)
    )
    try:
        with TestClient(application) as client:
            if payload is None:
                return client.request(method, path)
            return client.request(method, path, json=payload)
    finally:
        application.dependency_overrides.clear()


def _assert_error(
    response: httpx.Response,
    *,
    status_code: int,
    code: str,
) -> None:
    """공통 오류 응답의 상태와 안전한 필드 집합을 검증합니다."""

    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"detail"}
    assert set(body["detail"]) == {"code", "message"}
    assert body["detail"]["code"] == code
    assert isinstance(body["detail"]["message"], str)
    assert body["detail"]["message"]


def _ner_create_payload() -> dict[str, object]:
    """유효한 공통 HTTP NER 추가 요청 본문을 만듭니다."""

    return {
        "deploymentId": "ner-standard-new",
        "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
        "timeoutMs": 5000,
        "enabled": True,
    }


def _llm_create_payload() -> dict[str, object]:
    """유효한 OpenAI 호환 LLM 추가 요청 본문을 만듭니다."""

    return {
        "deploymentId": "llm-local-new",
        "adapterType": "openai_compatible",
        "baseUrl": "http://10.0.0.8:11434/v1",
        "modelName": "qwen3:8b",
        "timeoutMs": 10000,
        "enabled": True,
    }


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        (
            "ner",
            _ner_create_payload(),
            {
                "deploymentId": "ner-standard-new",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ),
        (
            "llm",
            _llm_create_payload(),
            {
                "deploymentId": "llm-local-new",
                "adapterType": "openai_compatible",
                "baseUrl": "http://10.0.0.8:11434/v1",
                "modelName": "qwen3:8b",
                "timeoutMs": 10000,
                "enabled": True,
            },
        ),
    ],
)
def test_post_adds_path_kind_and_returns_safe_detail(
    kind: str,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    """POST는 경로 kind를 주입하고 안전한 상세를 201로 반환합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "POST",
        f"/deployments/{kind}",
        payload,
        service,
    )

    assert response.status_code == 201
    body = response.json()
    assert body == expected
    assert len(service.calls) == 1
    operation, deployment_id, deployment = service.calls[0]
    assert operation == "add"
    assert deployment_id == expected["deploymentId"]
    assert deployment.kind == kind
    if kind == "ner":
        assert deployment.adapter_type == "http_ner"
        assert deployment.model_name is None
    for field_name in (
        "kind",
        "adapterConfig",
        "modelInfo",
        "displayName",
        "modelId",
        "description",
    ):
        assert f'"{field_name}"' not in response.text
    assert "labels" not in response.text


@pytest.mark.parametrize(
    ("kind", "deployment_id", "payload"),
    [
        (
            "ner",
            "ner-existing",
            {
                "baseUrl": "http://10.0.0.9:8008/v1/ner/detect",
                "timeoutMs": 15000,
                "enabled": False,
            },
        ),
        (
            "llm",
            "llm-existing",
            {
                "adapterType": "openai_compatible",
                "baseUrl": "http://10.0.0.9:8000/v1",
                "modelName": "updated-model",
                "timeoutMs": 20000,
                "enabled": True,
            },
        ),
    ],
)
def test_put_replaces_full_config(
    kind: str,
    deployment_id: str,
    payload: dict[str, object],
) -> None:
    """PUT은 경로 ID의 전체 설정과 kind를 관리 서비스에 전달합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "PUT",
        f"/deployments/{kind}/{deployment_id}",
        payload,
        service,
    )

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": deployment_id,
        **payload,
    }
    operation, recorded_id, deployment = service.calls[0]
    assert operation == "update"
    assert recorded_id == deployment_id
    assert deployment.kind == kind
    assert deployment.enabled == payload["enabled"]
    if kind == "ner":
        assert deployment.adapter_type == "http_ner"
        assert deployment.model_name is None


@pytest.mark.parametrize(
    ("kind", "deployment_id", "enabled", "expected"),
    [
        (
            "ner",
            "ner-existing",
            False,
            {
                "deploymentId": "ner-existing",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": False,
            },
        ),
        (
            "llm",
            "llm-existing",
            True,
            {
                "deploymentId": "llm-existing",
                "adapterType": "openai_compatible",
                "baseUrl": "http://10.0.0.8:11434/v1",
                "modelName": "qwen3:8b",
                "timeoutMs": 10000,
                "enabled": True,
            },
        ),
    ],
)
def test_patch_changes_only_enabled_and_returns_full_detail(
    kind: str,
    deployment_id: str,
    enabled: bool,
    expected: dict[str, object],
) -> None:
    """NER·LLM PATCH는 활성 상태만 전달하고 변경 뒤 상세를 반환합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "PATCH",
        f"/deployments/{kind}/{deployment_id}/enabled",
        {"enabled": enabled},
        service,
    )

    assert response.status_code == 200
    assert response.json() == expected
    operation, recorded_id, deployment = service.calls[0]
    assert operation == "set_enabled"
    assert recorded_id == deployment_id
    assert deployment.kind == kind
    assert deployment.enabled is enabled
    if kind == "ner":
        assert deployment.adapter_type == "http_ner"
        assert deployment.model_name is None


@pytest.mark.parametrize("kind", ["ner", "llm"])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"enabled": "true"},
        {"enabled": 1},
        {"enabled": None},
        {"enabled": True, "adapterType": "mock"},
        {"enabled": True, "unknownField": True},
    ],
)
def test_patch_enabled_rejects_non_strict_request_body(
    kind: str,
    payload: dict[str, object],
) -> None:
    """활성 상태 요청은 정확한 JSON boolean 필드 하나만 허용합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "PATCH",
        f"/deployments/{kind}/{kind}-existing/enabled",
        payload,
        service,
    )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


@pytest.mark.parametrize(
    ("kind", "deployment_id"),
    [
        ("ner", "ner-inactive"),
        ("llm", "llm-inactive"),
    ],
)
def test_delete_inactive_deployment_returns_empty_204(
    kind: str,
    deployment_id: str,
) -> None:
    """NER와 LLM 삭제는 요청 본문 없이 빈 204 응답을 반환합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "DELETE",
        f"/deployments/{kind}/{deployment_id}",
        None,
        service,
    )

    assert response.status_code == 204
    assert response.content == b""
    assert service.delete_calls == [(deployment_id, kind)]
    assert service.calls == []


@pytest.mark.parametrize("kind", ["ner", "llm"])
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            DeploymentMustBeDisabledError("private-active-id"),
            409,
            "DEPLOYMENT_MUST_BE_DISABLED",
        ),
        (
            DeploymentNotFoundError("private-missing-id"),
            404,
            "DEPLOYMENT_NOT_FOUND",
        ),
    ],
)
def test_delete_policy_errors_are_mapped_safely(
    kind: str,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """활성 상태와 없는 ID 오류를 종류별 안전한 공통 응답으로 변환합니다."""

    service = RecordingDeploymentManagementService(error=error)

    response = _request(
        "DELETE",
        f"/deployments/{kind}/{kind}-target",
        None,
        service,
    )

    _assert_error(response, status_code=status_code, code=code)
    assert service.delete_calls == [(f"{kind}-target", kind)]
    assert "private-active-id" not in response.text
    assert "private-missing-id" not in response.text


@pytest.mark.parametrize("kind", ["ner", "llm"])
def test_delete_rejects_invalid_path_id_before_service_call(
    kind: str,
) -> None:
    """잘못된 Deployment ID는 관리 서비스를 호출하기 전에 422로 거부합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "DELETE",
        f"/deployments/{kind}/INVALID!",
        None,
        service,
    )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert response.json()["detail"]["message"] == (
        "요청 검증에 실패했습니다: path.deployment_id: "
        "소문자 또는 숫자로 시작하고 소문자, 숫자, '.', '_', '-'만 "
        "사용할 수 있습니다"
    )
    assert "INVALID!" not in response.text
    assert service.delete_calls == []


@pytest.mark.parametrize(
    ("snake_case_field", "camel_case_field", "value"),
    [
        ("deployment_id", "deploymentId", "llm-snake-case"),
        ("adapter_type", "adapterType", "openai_compatible"),
        ("base_url", "baseUrl", "http://10.0.0.8:11434/v1"),
        ("model_name", "modelName", "qwen3:8b"),
        ("timeout_ms", "timeoutMs", 10000),
    ],
)
def test_post_rejects_snake_case_request_fields(
    snake_case_field: str,
    camel_case_field: str,
    value: object,
) -> None:
    """외부 관리 요청은 Python 필드명이 아닌 camelCase만 허용합니다."""

    service = RecordingDeploymentManagementService()
    payload = _llm_create_payload()
    payload.pop(camel_case_field, None)
    payload[snake_case_field] = value

    response = _request(
        "POST",
        "/deployments/llm",
        payload,
        service,
    )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/deployments/ner"),
        ("PUT", "/deployments/ner/ner-existing"),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("adapterConfig", {}),
        ("adapterConfig", None),
        ("adapter_config", {}),
    ],
)
def test_management_rejects_removed_adapter_config_fields(
    method: str,
    path: str,
    field_name: str,
    value: object,
) -> None:
    """제거된 Adapter 설정을 POST·PUT 어느 표기로도 다시 받지 않습니다."""

    service = RecordingDeploymentManagementService()
    payload = _ner_create_payload()
    if method == "PUT":
        payload.pop("deploymentId")
    payload[field_name] = value

    response = _request(method, path, payload, service)

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/deployments/llm"),
        ("PUT", "/deployments/llm/llm-existing"),
    ],
)
def test_management_rejects_removed_model_info_before_service_call(
    method: str,
    path: str,
) -> None:
    """과거 modelInfo 설정은 POST·PUT 모두서 저장 전에 거부합니다."""

    service = RecordingDeploymentManagementService()
    payload = _llm_create_payload()
    if method == "PUT":
        payload.pop("deploymentId")
    payload["modelInfo"] = {
        "displayName": "로컬 생성 LLM",
        "modelId": "qwen3:8b",
        "description": "제거된 공개 모델 정보",
    }

    response = _request(
        method,
        path,
        payload,
        service,
    )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


def test_put_rejects_mixed_camel_and_snake_case_fields() -> None:
    """정상 alias와 Python 필드명을 함께 보내도 추가 필드로 거부합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(
        "PUT",
        "/deployments/ner/ner-a",
        {
            "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
            "base_url": "http://10.0.0.7:8008/v1/ner/detect",
            "timeoutMs": 5000,
            "enabled": True,
        },
        service,
    )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/deployments/ner"),
        ("PUT", "/deployments/ner/ner-existing"),
    ],
)
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("adapterType", "http_ner"),
        ("modelName", "private-ner-model"),
    ],
)
def test_ner_management_rejects_adapter_and_model_fields(
    method: str,
    path: str,
    field_name: str,
    value: str,
) -> None:
    """NER 계약은 서버 주소 외에 Adapter나 모델 선택 입력을 받지 않습니다."""

    service = RecordingDeploymentManagementService()
    payload = _ner_create_payload()
    if method == "PUT":
        payload.pop("deploymentId")
    payload[field_name] = value

    response = _request(method, path, payload, service)

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            "/deployments/ner",
            {
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ),
        (
            "POST",
            "/deployments/ner",
            {
                "deploymentId": "ner-a",
                "kind": "ner",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ),
        (
            "POST",
            "/deployments/ner",
            {
                "deploymentId": "ner-a",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": "true",
            },
        ),
        (
            "POST",
            "/deployments/ner",
            {
                "deploymentId": "ner-a",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
                "unknownField": True,
            },
        ),
        (
            "POST",
            "/deployments/ner",
            {
                "deploymentId": "INVALID!",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ),
        (
            "PUT",
            "/deployments/ner/ner-a",
            {
                "deploymentId": "ner-a",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ),
        (
            "PUT",
            "/deployments/ner/INVALID!",
            {
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ),
    ],
)
def test_invalid_request_is_rejected_before_service_call(
    method: str,
    path: str,
    payload: dict[str, object],
) -> None:
    """누락·kind·추가 필드·타입·ID 오류를 공통 422로 거부합니다."""

    service = RecordingDeploymentManagementService()

    response = _request(method, path, payload, service)

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


def test_llm_create_reports_safe_deployment_id_format_reason() -> None:
    """Provider 모델 이름 문법을 ID에 사용하면 허용 문자 규칙을 안내합니다."""

    service = RecordingDeploymentManagementService()
    payload = _llm_create_payload()
    invalid_deployment_id = "local-qwen3:14b"
    payload["deploymentId"] = invalid_deployment_id

    response = _request(
        "POST",
        "/deployments/llm",
        payload,
        service,
    )

    assert response.status_code == 422
    assert invalid_deployment_id not in response.text
    assert response.json() == {
        "detail": {
            "code": "REQUEST_VALIDATION_FAILED",
            "message": (
                "요청 검증에 실패했습니다: body.deploymentId: "
                "소문자 또는 숫자로 시작하고 소문자, 숫자, '.', '_', '-'만 "
                "사용할 수 있습니다"
            ),
        }
    }
    assert service.calls == []


@pytest.mark.parametrize(
    ("method", "path", "error", "status_code", "code"),
    [
        (
            "POST",
            "/deployments/ner",
            DeploymentAlreadyExistsError("internal-secret-id"),
            409,
            "DEPLOYMENT_ALREADY_EXISTS",
        ),
        (
            "PUT",
            "/deployments/ner/ner-a",
            DeploymentNotFoundError("internal-secret-id"),
            404,
            "DEPLOYMENT_NOT_FOUND",
        ),
        (
            "POST",
            "/deployments/ner",
            DeploymentActivationError(
                deployment_id="internal-secret-id",
                operation="add",
                activation_error=RuntimeError(
                    "http://private-model.local"
                ),
            ),
            503,
            "DEPLOYMENT_ACTIVATION_FAILED",
        ),
        (
            "PUT",
            "/deployments/ner/ner-a",
            DeploymentRollbackError(
                deployment_id="internal-secret-id",
                operation="update",
                activation_error=RuntimeError("private activation"),
                rollback_error=RuntimeError("private rollback"),
            ),
            500,
            "DEPLOYMENT_ROLLBACK_FAILED",
        ),
        (
            "PUT",
            "/deployments/ner/ner-a",
            RegistryManagerNotInitializedError("private registry"),
            503,
            "REGISTRY_NOT_INITIALIZED",
        ),
        (
            "PUT",
            "/deployments/ner/ner-a",
            OSError("C:/private/config/ner_deployments.json"),
            500,
            "DEPLOYMENT_STORAGE_FAILED",
        ),
    ],
)
def test_management_errors_are_mapped_without_internal_details(
    method: str,
    path: str,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """관리 실패를 공통 오류로 변환하고 원인 문자열을 노출하지 않습니다."""

    service = RecordingDeploymentManagementService(error=error)
    payload = (
        _ner_create_payload()
        if method == "POST"
        else {
            "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
            "timeoutMs": 5000,
            "enabled": True,
        }
    )

    response = _request(method, path, payload, service)

    _assert_error(
        response,
        status_code=status_code,
        code=code,
    )
    assert "internal-secret-id" not in response.text
    assert "private" not in response.text


@pytest.mark.parametrize(
    "error_code",
    [
        "ADAPTER_NOT_REGISTERED",
        "ADAPTER_KIND_MISMATCH",
        "ADAPTER_REQUIRED_FIELD_MISSING",
        "ADAPTER_FIELD_NOT_ALLOWED",
        "ADAPTER_CONFIG_INVALID",
    ],
)
def test_backend_contract_error_returns_safe_422(
    error_code: BackendValidationErrorCode,
) -> None:
    """Adapter 계약 코드는 유지하고 내부 설정 상세는 숨깁니다."""

    service = RecordingDeploymentManagementService(
        error=BackendValidationError(
            error_code,
            deployment_id="internal-secret-id",
            adapter_type="private-adapter",
            kind="ner",
            detail="http://private-model.local",
        )
    )

    response = _request(
        "POST",
        "/deployments/ner",
        _ner_create_payload(),
        service,
    )

    _assert_error(
        response,
        status_code=422,
        code=error_code,
    )
    assert "internal-secret-id" not in response.text
    assert "private-adapter" not in response.text
    assert "private-model" not in response.text


def test_openapi_exposes_create_replace_enabled_patch_and_delete() -> None:
    """OpenAPI에 쓰기, 상태 변경과 무본문 DELETE 계약을 구분해 공개합니다."""

    document = create_app(runtime_factory=_runtime_factory).openapi()
    paths = document["paths"]

    for kind in ("ner", "llm"):
        create_operation = paths[f"/deployments/{kind}"]["post"]
        detail_path = paths[
            f"/deployments/{kind}/{{deployment_id}}"
        ]
        assert "201" in create_operation["responses"]
        assert "requestBody" in create_operation
        assert "200" in detail_path["put"]["responses"]
        assert "requestBody" in detail_path["put"]
        assert "patch" not in detail_path
        delete_operation = detail_path["delete"]
        assert "204" in delete_operation["responses"]
        assert "requestBody" not in delete_operation
        enabled_operation = paths[
            f"/deployments/{kind}/{{deployment_id}}/enabled"
        ]["patch"]
        assert "200" in enabled_operation["responses"]
        assert enabled_operation["requestBody"]["required"] is True
        request_schema = enabled_operation["requestBody"][
            "content"
        ]["application/json"]["schema"]
        assert request_schema["$ref"].endswith(
            "/DeploymentEnabledUpdateRequest"
        )

        expected_prefix = "Ner" if kind == "ner" else "Llm"
        create_schema = create_operation["requestBody"]["content"][
            "application/json"
        ]["schema"]
        update_schema = detail_path["put"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert create_schema["$ref"].endswith(
            f"/{expected_prefix}DeploymentCreateRequest"
        )
        assert update_schema["$ref"].endswith(
            f"/{expected_prefix}DeploymentUpdateRequest"
        )

    schemas = document["components"]["schemas"]
    ner_create_schema = schemas["NerDeploymentCreateRequest"]
    ner_update_schema = schemas["NerDeploymentUpdateRequest"]
    llm_create_schema = schemas["LlmDeploymentCreateRequest"]
    llm_update_schema = schemas["LlmDeploymentUpdateRequest"]
    ner_create_properties = ner_create_schema[
        "properties"
    ]
    ner_update_properties = ner_update_schema[
        "properties"
    ]
    llm_create_properties = llm_create_schema[
        "properties"
    ]
    llm_update_properties = llm_update_schema[
        "properties"
    ]
    enabled_schema = schemas["DeploymentEnabledUpdateRequest"]
    enabled_properties = enabled_schema["properties"]
    summary_properties = schemas["DeploymentSummary"][
        "properties"
    ]
    ner_detail_properties = schemas["NerDeploymentDetail"][
        "properties"
    ]
    llm_detail_properties = schemas["LlmDeploymentDetail"][
        "properties"
    ]

    ner_write_properties = {
        "enabled",
        "baseUrl",
        "timeoutMs",
    }
    llm_write_properties = {
        "adapterType",
        "enabled",
        "baseUrl",
        "modelName",
        "timeoutMs",
    }
    assert set(ner_create_properties) == {
        *ner_write_properties,
        "deploymentId",
    }
    assert set(ner_update_properties) == ner_write_properties
    assert ner_create_schema["required"] == [
        "enabled",
        "baseUrl",
        "timeoutMs",
        "deploymentId",
    ]
    assert ner_update_schema["required"] == [
        "enabled",
        "baseUrl",
        "timeoutMs",
    ]
    assert ner_create_schema["additionalProperties"] is False
    assert ner_update_schema["additionalProperties"] is False
    assert set(llm_create_properties) == {
        *llm_write_properties,
        "deploymentId",
    }
    assert set(llm_update_properties) == llm_write_properties
    assert set(enabled_properties) == {"enabled"}
    assert enabled_schema["required"] == ["enabled"]
    assert enabled_schema["additionalProperties"] is False
    assert enabled_properties["enabled"]["type"] == "boolean"
    assert set(summary_properties) == {
        "deploymentId",
        "enabled",
    }
    assert set(ner_detail_properties) == {
        "deploymentId",
        "enabled",
        "baseUrl",
        "timeoutMs",
    }
    assert set(llm_detail_properties) == {
        "deploymentId",
        "enabled",
        "adapterType",
        "baseUrl",
        "modelName",
        "timeoutMs",
    }
    assert "DeploymentModelInfoRequest" not in schemas
    assert "DeploymentCreateRequest" not in schemas
    assert "DeploymentUpdateRequest" not in schemas
    assert "DeploymentDetail" not in schemas

    removed_properties = {
        "modelInfo",
        "model_info",
        "displayName",
        "display_name",
        "modelId",
        "model_id",
        "description",
        "adapterConfig",
        "adapter_config",
    }
    for properties in (
        ner_create_properties,
        ner_update_properties,
        llm_create_properties,
        llm_update_properties,
        enabled_properties,
        summary_properties,
        ner_detail_properties,
        llm_detail_properties,
    ):
        assert removed_properties.isdisjoint(properties)

    assert {"adapterType", "modelName"}.isdisjoint(
        ner_create_properties
    )
    assert {"adapterType", "modelName"}.isdisjoint(
        ner_update_properties
    )
    assert {"adapterType", "modelName"}.isdisjoint(
        ner_detail_properties
    )


def test_management_returns_503_without_runtime() -> None:
    """Lifespan 밖에서는 관리 서비스 없이 기존 Runtime 503을 반환합니다."""

    application = create_app(runtime_factory=_runtime_factory)
    client = TestClient(application)
    try:
        response = client.post(
            "/deployments/ner",
            json={
                "deploymentId": "ner-a",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        )
    finally:
        client.close()

    _assert_error(
        response,
        status_code=503,
        code="APPLICATION_RUNTIME_UNAVAILABLE",
    )


def _write_runtime_files(config_dir: Path) -> None:
    """실제 API 통합 테스트용 종류별 Registry와 고정 Prompt를 만듭니다."""

    config_dir.mkdir()
    (config_dir / "ner_deployments.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (config_dir / "llm_deployments.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (config_dir / "prompts.j2").write_text(
        "{{ text }}\n{{ existing_detections }}\n",
        encoding="utf-8",
    )
    (config_dir / "title_prompt.j2").write_text(
        "제목만 생성하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / "mask_prompt.j2").write_text(
        "탐지 구간을 마스킹하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / POLICY_PROMPTS_FILENAME).write_bytes(
        DEFAULT_POLICY_PROMPTS_PATH.read_bytes()
    )


def test_real_api_updates_split_files_and_active_snapshot(
    tmp_path: Path,
) -> None:
    """실제 POST·PUT이 해당 종류 파일과 Active Snapshot만 갱신합니다."""

    config_dir = tmp_path / "config"
    _write_runtime_files(config_dir)
    captured_runtime: list[ApplicationRuntime] = []

    @asynccontextmanager
    async def runtime_factory() -> AsyncIterator[ApplicationRuntime]:
        """테스트 config_dir로 실제 Runtime을 실행합니다."""

        async with application_runtime(
            config_dir=config_dir
        ) as runtime:
            captured_runtime.append(runtime)
            yield runtime

    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        created = client.post(
            "/deployments/ner",
            json={
                "deploymentId": "ner-managed",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        )
        llm_before_update = (
            config_dir / "llm_deployments.json"
        ).read_bytes()
        updated = client.put(
            "/deployments/ner/ner-managed",
            json={
                "baseUrl": "http://10.0.0.9:8008/v1/ner/detect",
                "timeoutMs": 15000,
                "enabled": False,
            },
        )
        llm_after_ner_update = (
            config_dir / "llm_deployments.json"
        ).read_bytes()
        llm_created = client.post(
            "/deployments/llm",
            json={
                "deploymentId": "llm-managed",
                "adapterType": "mock",
                "enabled": True,
            },
        )
        generation_before_wrong_kind = (
            captured_runtime[0].registry_manager.state.generation
        )
        wrong_kind = client.put(
            "/deployments/ner/llm-managed",
            json={
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": False,
            },
        )

        assert created.status_code == 201
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert llm_after_ner_update == llm_before_update
        assert llm_created.status_code == 201
        _assert_error(
            wrong_kind,
            status_code=404,
            code="DEPLOYMENT_NOT_FOUND",
        )
        assert (
            captured_runtime[0].registry_manager.state.generation
            == generation_before_wrong_kind
        )
        assert (
            config_dir / "llm_deployments.json"
        ).read_bytes() != llm_before_update

    ner_stored = json.loads(
        (config_dir / "ner_deployments.json").read_text(
            encoding="utf-8"
        )
    )
    llm_stored = json.loads(
        (config_dir / "llm_deployments.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = captured_runtime[0]
    assert ner_stored["ner-managed"]["enabled"] is False
    assert ner_stored["ner-managed"]["baseUrl"] == (
        "http://10.0.0.9:8008/v1/ner/detect"
    )
    assert ner_stored["ner-managed"]["timeoutMs"] == 15000
    assert "adapterType" not in ner_stored["ner-managed"]
    assert "modelName" not in ner_stored["ner-managed"]
    assert "kind" not in ner_stored["ner-managed"]
    assert "kind" not in llm_stored["llm-managed"]
    assert llm_stored["llm-managed"]["enabled"] is True
    assert (
        runtime.registry_manager.state.snapshot.deployments[
            "ner-managed"
        ].enabled
        is False
    )
    assert (
        runtime.registry_manager.state.snapshot.deployments[
            "ner-managed"
        ].adapter_type
        == "http_ner"
    )
    assert (
        runtime.registry_manager.state.snapshot.deployments[
            "llm-managed"
        ].kind
        == "llm"
    )


def test_real_enabled_patch_updates_only_target_file_and_snapshot(
    tmp_path: Path,
) -> None:
    """NER·LLM 활성 상태 변경을 해당 파일과 Active Snapshot에만 반영합니다."""

    config_dir = tmp_path / "config"
    _write_runtime_files(config_dir)
    captured_runtime: list[ApplicationRuntime] = []

    @asynccontextmanager
    async def runtime_factory() -> AsyncIterator[ApplicationRuntime]:
        """테스트 config_dir로 실제 Runtime을 실행합니다."""

        async with application_runtime(
            config_dir=config_dir
        ) as runtime:
            captured_runtime.append(runtime)
            yield runtime

    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        ner_created = client.post(
            "/deployments/ner",
            json={
                "deploymentId": "ner-patch",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": False,
            },
        )
        llm_created = client.post(
            "/deployments/llm",
            json={
                "deploymentId": "llm-patch",
                "adapterType": "mock",
                "enabled": False,
            },
        )
        assert ner_created.status_code == 201
        assert llm_created.status_code == 201

        llm_before_ner_patch = (
            config_dir / "llm_deployments.json"
        ).read_bytes()
        ner_enabled = client.patch(
            "/deployments/ner/ner-patch/enabled",
            json={"enabled": True},
        )
        assert ner_enabled.status_code == 200
        assert ner_enabled.json() == {
            "deploymentId": "ner-patch",
            "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
            "timeoutMs": 5000,
            "enabled": True,
        }
        assert (
            config_dir / "llm_deployments.json"
        ).read_bytes() == llm_before_ner_patch
        assert (
            captured_runtime[0]
            .registry_manager.state.snapshot.deployments[
                "ner-patch"
            ]
            .enabled
            is True
        )

        ner_before_llm_patch = (
            config_dir / "ner_deployments.json"
        ).read_bytes()
        llm_enabled = client.patch(
            "/deployments/llm/llm-patch/enabled",
            json={"enabled": True},
        )
        assert llm_enabled.status_code == 200
        assert llm_enabled.json() == {
            "deploymentId": "llm-patch",
            "adapterType": "mock",
            "enabled": True,
        }
        assert (
            config_dir / "ner_deployments.json"
        ).read_bytes() == ner_before_llm_patch
        assert (
            captured_runtime[0]
            .registry_manager.state.snapshot.deployments[
                "llm-patch"
            ]
            .enabled
            is True
        )

        generation_before_idempotent = (
            captured_runtime[0].registry_manager.state.generation
        )
        ner_file_before_idempotent = (
            config_dir / "ner_deployments.json"
        ).read_bytes()
        idempotent = client.patch(
            "/deployments/ner/ner-patch/enabled",
            json={"enabled": True},
        )
        assert idempotent.status_code == 200
        assert (
            config_dir / "ner_deployments.json"
        ).read_bytes() == ner_file_before_idempotent
        assert (
            captured_runtime[0].registry_manager.state.generation
            == generation_before_idempotent
        )

        generation_before_wrong_kind = (
            captured_runtime[0].registry_manager.state.generation
        )
        ner_file_before_wrong_kind = (
            config_dir / "ner_deployments.json"
        ).read_bytes()
        llm_file_before_wrong_kind = (
            config_dir / "llm_deployments.json"
        ).read_bytes()
        wrong_ner_path = client.patch(
            "/deployments/ner/llm-patch/enabled",
            json={"enabled": False},
        )
        wrong_llm_path = client.patch(
            "/deployments/llm/ner-patch/enabled",
            json={"enabled": False},
        )
        missing_ner = client.patch(
            "/deployments/ner/missing/enabled",
            json={"enabled": False},
        )
        missing_llm = client.patch(
            "/deployments/llm/missing/enabled",
            json={"enabled": False},
        )
        for response in (
            wrong_ner_path,
            wrong_llm_path,
            missing_ner,
            missing_llm,
        ):
            _assert_error(
                response,
                status_code=404,
                code="DEPLOYMENT_NOT_FOUND",
            )
        assert (
            config_dir / "ner_deployments.json"
        ).read_bytes() == ner_file_before_wrong_kind
        assert (
            config_dir / "llm_deployments.json"
        ).read_bytes() == llm_file_before_wrong_kind
        assert (
            captured_runtime[0].registry_manager.state.generation
            == generation_before_wrong_kind
        )

    ner_stored = json.loads(
        (config_dir / "ner_deployments.json").read_text(
            encoding="utf-8"
        )
    )
    llm_stored = json.loads(
        (config_dir / "llm_deployments.json").read_text(
            encoding="utf-8"
        )
    )
    assert ner_stored["ner-patch"] == {
        "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
        "timeoutMs": 5000,
        "enabled": True,
    }
    assert llm_stored["llm-patch"] == {
        "adapterType": "mock",
        "enabled": True,
    }


def test_real_delete_updates_target_file_and_active_snapshot_only(
    tmp_path: Path,
) -> None:
    """종류별 삭제를 해당 파일과 Active Snapshot에만 원자적으로 반영합니다."""

    config_dir = tmp_path / "config"
    _write_runtime_files(config_dir)
    captured_runtime: list[ApplicationRuntime] = []

    @asynccontextmanager
    async def runtime_factory() -> AsyncIterator[ApplicationRuntime]:
        """임시 설정 파일을 사용하는 실제 Runtime을 제공합니다."""

        async with application_runtime(
            config_dir=config_dir
        ) as runtime:
            captured_runtime.append(runtime)
            yield runtime

    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        assert client.post(
            "/deployments/ner",
            json={
                "deploymentId": "ner-delete",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": False,
            },
        ).status_code == 201
        assert client.post(
            "/deployments/llm",
            json={
                "deploymentId": "llm-delete",
                "adapterType": "mock",
                "enabled": False,
            },
        ).status_code == 201
        assert client.post(
            "/deployments/ner",
            json={
                "deploymentId": "ner-active",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        ).status_code == 201

        runtime = captured_runtime[0]
        ner_before_rejections = (
            config_dir / "ner_deployments.json"
        ).read_bytes()
        llm_before_rejections = (
            config_dir / "llm_deployments.json"
        ).read_bytes()
        generation_before_rejections = (
            runtime.registry_manager.state.generation
        )

        active = client.delete("/deployments/ner/ner-active")
        wrong_kind = client.delete("/deployments/ner/llm-delete")
        missing = client.delete("/deployments/llm/missing")

        _assert_error(
            active,
            status_code=409,
            code="DEPLOYMENT_MUST_BE_DISABLED",
        )
        for response in (wrong_kind, missing):
            _assert_error(
                response,
                status_code=404,
                code="DEPLOYMENT_NOT_FOUND",
            )
        assert (
            config_dir / "ner_deployments.json"
        ).read_bytes() == ner_before_rejections
        assert (
            config_dir / "llm_deployments.json"
        ).read_bytes() == llm_before_rejections
        assert (
            runtime.registry_manager.state.generation
            == generation_before_rejections
        )

        llm_before_ner_delete = (
            config_dir / "llm_deployments.json"
        ).read_bytes()
        ner_deleted = client.delete("/deployments/ner/ner-delete")
        assert ner_deleted.status_code == 204
        assert ner_deleted.content == b""
        assert (
            config_dir / "llm_deployments.json"
        ).read_bytes() == llm_before_ner_delete
        assert "ner-delete" not in (
            runtime.registry_manager.state.snapshot.deployments
        )
        assert "llm-delete" in (
            runtime.registry_manager.state.snapshot.deployments
        )

        ner_before_llm_delete = (
            config_dir / "ner_deployments.json"
        ).read_bytes()
        llm_deleted = client.delete("/deployments/llm/llm-delete")
        assert llm_deleted.status_code == 204
        assert llm_deleted.content == b""
        assert (
            config_dir / "ner_deployments.json"
        ).read_bytes() == ner_before_llm_delete
        assert "llm-delete" not in (
            runtime.registry_manager.state.snapshot.deployments
        )
        assert "ner-active" in (
            runtime.registry_manager.state.snapshot.deployments
        )

    ner_stored = json.loads(
        (config_dir / "ner_deployments.json").read_text(
            encoding="utf-8"
        )
    )
    llm_stored = json.loads(
        (config_dir / "llm_deployments.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(ner_stored) == {"ner-active"}
    assert llm_stored == {}
    assert list(config_dir.glob(".*_deployments.json.*.tmp")) == []


def test_corrupted_registry_returns_server_storage_error(
    tmp_path: Path,
) -> None:
    """금지된 kind가 든 Registry를 정상 요청 본문의 422로 오분류하지 않습니다."""

    config_dir = tmp_path / "config"
    _write_runtime_files(config_dir)
    captured_runtime: list[ApplicationRuntime] = []

    @asynccontextmanager
    async def runtime_factory() -> AsyncIterator[ApplicationRuntime]:
        """손상 전 정상 Snapshot을 가진 실제 Runtime을 실행합니다."""

        async with application_runtime(
            config_dir=config_dir
        ) as runtime:
            captured_runtime.append(runtime)
            yield runtime

    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        generation_before = (
            captured_runtime[0].registry_manager.state.generation
        )
        (config_dir / "ner_deployments.json").write_text(
            json.dumps(
                {
                    "explicit-kind": {
                        "kind": "llm",
                        "adapterType": "mock",
                        "enabled": True,
                    }
                }
            ),
            encoding="utf-8",
        )

        response = client.post(
            "/deployments/ner",
            json={
                "deploymentId": "ner-valid",
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        )

    _assert_error(
        response,
        status_code=500,
        code="DEPLOYMENT_STORAGE_FAILED",
    )
    assert (
        captured_runtime[0].registry_manager.state.generation
        == generation_before
    )
