"""종류별 Backend Adapter 목록 HTTP API 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx
from fastapi.testclient import TestClient

from app.backends.provider_registry import BackendProviderRegistry
from app.core.application_runtime import ApplicationRuntime
from app.main import create_app
from app.registry.manager import RegistryManager
from app.schemas.adapters import AdapterListResponse
from app.schemas.registry import DeploymentKind
from app.services.adapter_catalog import AdapterCatalogService
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.detection_pipeline import DetectionPipeline
from app.services.generation_pipeline import GenerationPipeline
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


@dataclass(slots=True)
class RecordingAdapterCatalog:
    """종류별 예약 응답을 반환하고 조회 기록을 보관합니다."""

    responses: dict[DeploymentKind, AdapterListResponse]
    calls: list[DeploymentKind] = field(default_factory=list)

    def list_adapters(
        self,
        kind: DeploymentKind,
    ) -> AdapterListResponse:
        """요청 종류를 기록하고 해당 Adapter 목록을 반환합니다."""

        self.calls.append(kind)
        return self.responses[kind]


def _catalog() -> RecordingAdapterCatalog:
    """NER·LLM별 공개 Adapter 계약이 있는 테스트 카탈로그를 만듭니다."""

    return RecordingAdapterCatalog(
        responses={
            "ner": AdapterListResponse(
                adapters=("gliner_http", "http_ner")
            ),
            "llm": AdapterListResponse(
                adapters=("mock", "openai_compatible")
            ),
        }
    )


def _runtime_factory(
    catalog: RecordingAdapterCatalog,
):
    """Adapter 카탈로그를 주입한 최소 테스트 Runtime을 제공합니다."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[ApplicationRuntime]:
        """TestClient 수명 동안 가짜 Runtime을 한 번 제공합니다."""

        yield ApplicationRuntime(
            http_client=cast(httpx.AsyncClient, object()),
            registry_manager=cast(RegistryManager, object()),
            backend_providers=cast(BackendProviderRegistry, object()),
            adapter_catalog_service=cast(
                AdapterCatalogService,
                catalog,
            ),
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

    return factory


def test_get_llm_adapters_returns_name_list() -> None:
    """선택 가능한 LLM Adapter 이름 배열만 반환합니다."""

    catalog = _catalog()
    application = create_app(
        runtime_factory=_runtime_factory(catalog)
    )

    with TestClient(application) as client:
        response = client.get("/adapters/llm")

    assert response.status_code == 200
    assert response.json() == {
        "adapters": ["mock", "openai_compatible"],
    }
    assert catalog.calls == ["llm"]


def test_llm_adapter_list_is_get_only_and_has_no_request_body() -> None:
    """LLM 목록 API는 GET만 허용하고 Request Body를 만들지 않습니다."""

    catalog = _catalog()
    application = create_app(
        runtime_factory=_runtime_factory(catalog)
    )

    with TestClient(application) as client:
        response = client.post("/adapters/llm")

    assert response.status_code == 405
    assert catalog.calls == []

    operation = application.openapi()["paths"]["/adapters/llm"]["get"]
    assert "requestBody" not in operation
    response_schema = application.openapi()["components"][
        "schemas"
    ]["AdapterListResponse"]
    assert set(response_schema["properties"]) == {"adapters"}
    assert response_schema["properties"]["adapters"]["type"] == "array"


def test_ner_adapter_list_is_not_exposed() -> None:
    """NER는 고정 표준 계약을 사용하므로 Adapter 목록을 제공하지 않습니다."""

    catalog = _catalog()
    application = create_app(
        runtime_factory=_runtime_factory(catalog)
    )

    with TestClient(application) as client:
        response = client.get("/adapters/ner")

    assert response.status_code == 404
    assert "/adapters/ner" not in application.openapi()["paths"]
    assert catalog.calls == []


def test_adapter_list_returns_503_without_runtime() -> None:
    """Lifespan 밖에서는 카탈로그를 조회하지 않고 안전한 오류를 반환합니다."""

    catalog = _catalog()
    application = create_app(
        runtime_factory=_runtime_factory(catalog)
    )
    client = TestClient(application)
    try:
        response = client.get("/adapters/llm")
    finally:
        client.close()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "APPLICATION_RUNTIME_UNAVAILABLE"
    )
    assert catalog.calls == []
