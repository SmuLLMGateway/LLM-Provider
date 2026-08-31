"""NER·LLM Deployment 실제 연결 Probe HTTP API 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_deployment_probe
from app.backends.errors import (
    BackendConfigurationError,
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
)
from app.backends.provider_registry import (
    BackendProviderLookupError,
    BackendProviderRegistry,
)
from app.core.application_runtime import ApplicationRuntime
from app.main import create_app
from app.policies.span_validator import DetectionSpanValidationError
from app.registry.deployment_resolver import DeploymentResolutionError
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.schemas.deployments import DeploymentProbeResponse
from app.services.adapter_catalog import AdapterCatalogService
from app.services.detection_pipeline import DetectionPipeline
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.deployment_probe import (
    DeploymentProbeService,
    LlmProbeBackendResultError,
)
from app.services.generation_pipeline import GenerationPipeline
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


@dataclass(slots=True)
class RecordingProbeService:
    """호출한 Deployment ID를 기록하고 예약된 Probe 결과를 반환합니다."""

    ner_result: DeploymentProbeResponse = field(
        default_factory=lambda: DeploymentProbeResponse(
            deployment_id="ner-a",
            status="available",
            latency_ms=12.345,
        )
    )
    llm_result: DeploymentProbeResponse = field(
        default_factory=lambda: DeploymentProbeResponse(
            deployment_id="llm-a",
            status="available",
            latency_ms=23.456,
        )
    )
    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def probe_ner(
        self,
        *,
        deployment_id: str,
    ) -> DeploymentProbeResponse:
        """NER Probe 호출 정보를 기록한 뒤 결과 또는 오류를 전달합니다."""

        self.calls.append(("ner", deployment_id))
        if self.error is not None:
            raise self.error
        return self.ner_result

    async def probe_llm(
        self,
        *,
        deployment_id: str,
    ) -> DeploymentProbeResponse:
        """LLM Probe 호출 정보를 기록한 뒤 결과 또는 오류를 전달합니다."""

        self.calls.append(("llm", deployment_id))
        if self.error is not None:
            raise self.error
        return self.llm_result


@asynccontextmanager
async def _runtime_factory() -> AsyncIterator[ApplicationRuntime]:
    """Probe 의존성을 교체할 수 있는 최소 테스트 Runtime을 제공합니다."""

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
    service: RecordingProbeService,
) -> httpx.Response:
    """주입한 Probe Service를 사용하는 HTTP 요청을 보냅니다."""

    application = create_app(runtime_factory=_runtime_factory)
    application.dependency_overrides[get_deployment_probe] = (
        lambda: cast(DeploymentProbeService, service)
    )
    try:
        with TestClient(application) as client:
            return client.request(method, path)
    finally:
        application.dependency_overrides.clear()


def _assert_error_response(
    response: httpx.Response,
    *,
    status_code: int,
    code: str,
) -> None:
    """오류 응답이 안전한 공통 필드만 포함하는지 확인합니다."""

    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"detail"}
    assert set(body["detail"]) == {"code", "message"}
    assert body["detail"]["code"] == code
    assert isinstance(body["detail"]["message"], str)
    assert body["detail"]["message"]


def test_post_ner_probe_returns_connection_status_and_latency() -> None:
    """성공한 실제 Probe 결과를 camelCase 응답으로 반환합니다."""

    service = RecordingProbeService()

    response = _request(
        "POST",
        "/deployments/ner/ner-a/probe",
        service,
    )

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": "ner-a",
        "status": "available",
        "latencyMs": 12.345,
    }
    assert service.calls == [("ner", "ner-a")]


@pytest.mark.parametrize(
    ("kind", "deployment_id"),
    [
        ("ner", "ner-a"),
        ("llm", "llm-a"),
    ],
)
def test_probe_is_post_only_and_has_no_request_body_contract(
    kind: str,
    deployment_id: str,
) -> None:
    """GET은 허용하지 않고 OpenAPI에도 Request Body를 만들지 않습니다."""

    service = RecordingProbeService()
    response = _request(
        "GET",
        f"/deployments/{kind}/{deployment_id}/probe",
        service,
    )

    assert response.status_code == 405
    assert service.calls == []

    application = create_app(runtime_factory=_runtime_factory)
    operation = application.openapi()["paths"][
        f"/deployments/{kind}/{{deployment_id}}/probe"
    ]["post"]
    assert "requestBody" not in operation


def test_post_llm_probe_returns_connection_status_and_latency() -> None:
    """LLM 실제 생성 Probe 결과도 같은 공통 응답으로 반환합니다."""

    service = RecordingProbeService()
    response = _request(
        "POST",
        "/deployments/llm/llm-a/probe",
        service,
    )

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": "llm-a",
        "status": "available",
        "latencyMs": 23.456,
    }
    assert service.calls == [("llm", "llm-a")]


@pytest.mark.parametrize("kind", ["ner", "llm"])
def test_probe_rejects_invalid_deployment_id_before_service_call(
    kind: str,
) -> None:
    """Registry ID 형식이 아닌 경로 값은 공통 요청 검증 오류로 거부합니다."""

    service = RecordingProbeService()

    response = _request(
        "POST",
        f"/deployments/{kind}/INVALID!/probe",
        service,
    )

    _assert_error_response(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert service.calls == []


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            RegistryManagerNotInitializedError(
                "registry-internal-secret"
            ),
            503,
            "REGISTRY_NOT_INITIALIZED",
        ),
        (
            BackendProviderLookupError(
                "BACKEND_PROVIDER_NOT_REGISTERED",
                deployment_id="ner-a",
                adapter_type="missing",
                kind="ner",
            ),
            503,
            "BACKEND_PROVIDER_NOT_REGISTERED",
        ),
        (
            DeploymentResolutionError(
                "DEPLOYMENT_NOT_FOUND",
                deployment_id="internal-deployment-id",
                expected_kind="ner",
            ),
            404,
            "DEPLOYMENT_NOT_FOUND",
        ),
        (
            DetectionSpanValidationError(
                "INVALID_SPAN",
                item_index=0,
            ),
            502,
            "NER_PROBE_RESULT_INVALID",
        ),
        (
            BackendConfigurationError(
                "TEST_NER_CONFIG_INVALID",
                "private model configuration",
            ),
            500,
            "TEST_NER_CONFIG_INVALID",
        ),
        (
            BackendTimeoutError(
                "TEST_NER_TIMEOUT",
                "private timeout detail",
            ),
            504,
            "TEST_NER_TIMEOUT",
        ),
        (
            BackendTransportError(
                "TEST_NER_TRANSPORT_ERROR",
                "http://private-ner.local",
            ),
            502,
            "TEST_NER_TRANSPORT_ERROR",
        ),
        (
            BackendResponseError(
                "TEST_NER_RESPONSE_INVALID",
                "private response body",
            ),
            502,
            "TEST_NER_RESPONSE_INVALID",
        ),
    ],
)
def test_ner_probe_maps_failures_to_safe_http_errors(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """조회·실행 실패를 기존 공통 오류 계약으로 안전하게 변환합니다."""

    service = RecordingProbeService(error=error)

    response = _request(
        "POST",
        "/deployments/ner/ner-a/probe",
        service,
    )

    _assert_error_response(
        response,
        status_code=status_code,
        code=code,
    )
    assert service.calls == [("ner", "ner-a")]
    assert "registry-internal-secret" not in response.text
    assert "internal-deployment-id" not in response.text
    assert "private" not in response.text


@pytest.mark.parametrize("kind", ["ner", "llm"])
def test_probe_returns_503_without_application_runtime(
    kind: str,
) -> None:
    """Lifespan 밖에서는 Probe Service를 조립하지 않고 503을 반환합니다."""

    application = create_app(runtime_factory=_runtime_factory)
    client = TestClient(application)
    try:
        response = client.post(
            f"/deployments/{kind}/{kind}-a/probe"
        )
    finally:
        client.close()

    _assert_error_response(
        response,
        status_code=503,
        code="APPLICATION_RUNTIME_UNAVAILABLE",
    )


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            RegistryManagerNotInitializedError(
                "registry-internal-secret"
            ),
            503,
            "REGISTRY_NOT_INITIALIZED",
        ),
        (
            BackendProviderLookupError(
                "BACKEND_PROVIDER_NOT_REGISTERED",
                deployment_id="llm-a",
                adapter_type="missing",
                kind="llm",
            ),
            503,
            "BACKEND_PROVIDER_NOT_REGISTERED",
        ),
        (
            LlmProbeBackendResultError(
                deployment_id="llm-a",
                actual_type=dict,
            ),
            500,
            "LLM_PROBE_RESULT_INVALID",
        ),
        (
            BackendTimeoutError(
                "TEST_LLM_TIMEOUT",
                "private timeout detail",
            ),
            504,
            "TEST_LLM_TIMEOUT",
        ),
        (
            BackendResponseError(
                "TEST_LLM_RESPONSE_INVALID",
                "private response body",
            ),
            502,
            "TEST_LLM_RESPONSE_INVALID",
        ),
    ],
)
def test_llm_probe_maps_failures_to_safe_http_errors(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """LLM Probe 전용 오류와 공통 Backend 오류를 안전하게 변환합니다."""

    service = RecordingProbeService(error=error)

    response = _request(
        "POST",
        "/deployments/llm/llm-a/probe",
        service,
    )

    _assert_error_response(
        response,
        status_code=status_code,
        code=code,
    )
    assert service.calls == [("llm", "llm-a")]
    assert "registry-internal-secret" not in response.text
    assert "private" not in response.text
