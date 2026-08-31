"""Deployment 공개 정보 조회 HTTP API 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backends.provider_registry import BackendProviderRegistry
from app.core.application_runtime import ApplicationRuntime
from app.main import create_app
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.services.adapter_catalog import AdapterCatalogService
from app.services.detection_pipeline import DetectionPipeline
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.generation_pipeline import GenerationPipeline
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


DeploymentKind = Literal["ner", "llm"]


@dataclass(frozen=True, slots=True)
class DeploymentFixture:
    """상세 조회용 실행 설정을 보관합니다."""

    kind: DeploymentKind
    enabled: bool
    adapter_type: str = "openai_compatible"
    base_url: str | None = "http://10.0.0.8:11434/v1"
    model_name: str | None = "qwen3:8b"
    timeout_ms: int | None = 12345
    secret_ref: str = "deployment-secret-reference"


@dataclass(slots=True)
class RecordingRegistryManager:
    """지정한 Deployment Snapshot을 반환하는 테스트용 Manager입니다."""

    deployments: Mapping[str, DeploymentFixture]
    error: Exception | None = None
    capture_count: int = 0

    def capture(self) -> SimpleNamespace:
        """현재 Snapshot 조회 횟수를 기록하고 지정한 결과를 반환합니다."""

        self.capture_count += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(deployments=self.deployments)


def _deployment(
    *,
    kind: DeploymentKind,
    enabled: bool = True,
) -> DeploymentFixture:
    """실행 설정과 내부 비공개 값이 있는 Deployment를 만듭니다."""

    return DeploymentFixture(
        kind=kind,
        enabled=enabled,
        adapter_type=(
            "http_ner"
            if kind == "ner"
            else "openai_compatible"
        ),
        base_url=(
            "http://10.0.0.7:8008/v1/ner/detect"
            if kind == "ner"
            else "http://10.0.0.8:11434/v1"
        ),
        model_name=None if kind == "ner" else "qwen3:8b",
    )


def _runtime_factory(
    registry_manager: RecordingRegistryManager,
):
    """Deployment API에 테스트용 RegistryManager를 주입합니다."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[ApplicationRuntime]:
        """TestClient 수명 동안 가짜 Runtime을 제공합니다."""

        yield ApplicationRuntime(
            http_client=cast(httpx.AsyncClient, object()),
            registry_manager=cast(RegistryManager, registry_manager),
            backend_providers=cast(BackendProviderRegistry, object()),
            adapter_catalog_service=cast(
                AdapterCatalogService,
                object(),
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


def _get(
    path: str,
    registry_manager: RecordingRegistryManager,
) -> httpx.Response:
    """테스트용 Runtime으로 Deployment 조회 요청을 보냅니다."""

    application = create_app(
        runtime_factory=_runtime_factory(registry_manager)
    )
    with TestClient(application) as client:
        return client.get(path)


def _deployments() -> dict[str, DeploymentFixture]:
    """정렬·필터·선택 필드 검증에 사용할 Deployment 집합을 만듭니다."""

    return {
        "ner-z-disabled": _deployment(
            kind="ner",
            enabled=False,
        ),
        "llm-b": _deployment(
            kind="llm",
        ),
        "ner-a": _deployment(
            kind="ner",
        ),
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/deployments/ner",
            [
                {
                    "deploymentId": "ner-a",
                    "enabled": True,
                },
                {
                    "deploymentId": "ner-z-disabled",
                    "enabled": False,
                },
            ],
        ),
        (
            "/deployments/llm",
            [
                {
                    "deploymentId": "llm-b",
                    "enabled": True,
                }
            ],
        ),
    ],
)
def test_list_deployments_returns_kind_specific_id_sorted_public_summaries(
    path: str,
    expected: list[dict[str, object]],
) -> None:
    """종류별 목록은 해당 종류만 ID순 공개 요약으로 반환합니다."""

    registry_manager = RecordingRegistryManager(_deployments())

    response = _get(path, registry_manager)

    assert response.status_code == 200
    assert response.json() == {"deployments": expected}
    assert registry_manager.capture_count == 1
    forbidden_fields = {
        "baseUrl",
        "timeoutMs",
        "adapterConfig",
        "adapterType",
        "modelName",
        "modelInfo",
        "displayName",
        "modelId",
        "description",
        "kind",
    }
    assert all(
        f'"{field_name}"' not in response.text
        for field_name in forbidden_fields
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/deployments/ner/ner-a",
            {
                "deploymentId": "ner-a",
                "enabled": True,
                "baseUrl": "http://10.0.0.7:8008/v1/ner/detect",
                "timeoutMs": 12345,
            },
        ),
        (
            "/deployments/llm/llm-b",
            {
                "deploymentId": "llm-b",
                "enabled": True,
                "adapterType": "openai_compatible",
                "baseUrl": "http://10.0.0.8:11434/v1",
                "modelName": "qwen3:8b",
                "timeoutMs": 12345,
            },
        ),
    ],
)
def test_get_deployment_returns_kind_specific_operational_detail(
    path: str,
    expected: dict[str, object],
) -> None:
    """종류별 상세 조회는 Gateway에 필요한 실행 설정을 반환합니다."""

    response = _get(path, RecordingRegistryManager(_deployments()))

    assert response.status_code == 200
    assert response.json() == expected
    for field_name in (
        "adapterConfig",
        "kind",
        "modelInfo",
        "displayName",
        "modelId",
        "description",
    ):
        assert f'"{field_name}"' not in response.text
    assert "deployment-adapter-secret" not in response.text
    assert "deployment-secret-reference" not in response.text


def test_llm_detail_omits_optional_detail_fields() -> None:
    """Mock의 선택 실행 필드와 공개 선택 필드는 null 대신 생략합니다."""

    deployment = DeploymentFixture(
        kind="llm",
        enabled=False,
        adapter_type="mock",
        base_url=None,
        model_name=None,
        timeout_ms=None,
    )

    response = _get(
        "/deployments/llm/llm-mock-disabled",
        RecordingRegistryManager({"llm-mock-disabled": deployment}),
    )

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": "llm-mock-disabled",
        "enabled": False,
        "adapterType": "mock",
    }
    assert '"baseUrl"' not in response.text
    assert '"modelName"' not in response.text
    assert '"timeoutMs"' not in response.text
    assert '"adapterConfig"' not in response.text
    assert '"kind"' not in response.text
    assert "deployment-secret-reference" not in response.text


def test_deployment_detail_does_not_derive_display_metadata() -> None:
    """상세 응답은 ID와 실행 설정에서 표시 메타데이터를 파생하지 않습니다."""

    deployment = DeploymentFixture(
        kind="llm",
        enabled=True,
        model_name="configured-model-name",
    )

    response = _get(
        "/deployments/llm/llm-without-public-info",
        RecordingRegistryManager(
            {"llm-without-public-info": deployment}
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": "llm-without-public-info",
        "enabled": True,
        "adapterType": "openai_compatible",
        "baseUrl": "http://10.0.0.8:11434/v1",
        "modelName": "configured-model-name",
        "timeoutMs": 12345,
    }
    assert '"kind"' not in response.text
    assert '"adapterConfig"' not in response.text
    assert '"displayName"' not in response.text
    assert '"modelId"' not in response.text
    assert '"description"' not in response.text
    assert "deployment-secret-reference" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/deployments",
        "/deployments?kind=ner",
        "/deployments/llm-b",
    ],
)
def test_removed_deployment_routes_return_404_without_snapshot_capture(
    path: str,
) -> None:
    """기존 전체·Query·종류 없는 상세 경로는 더 이상 제공하지 않습니다."""

    registry_manager = RecordingRegistryManager(_deployments())

    response = _get(path, registry_manager)

    assert response.status_code == 404
    assert registry_manager.capture_count == 0


def test_get_unknown_deployment_returns_safe_404() -> None:
    """존재하지 않는 ID는 공통 DEPLOYMENT_NOT_FOUND 오류로 반환합니다."""

    response = _get(
        "/deployments/ner/unknown-deployment",
        RecordingRegistryManager(_deployments()),
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DEPLOYMENT_NOT_FOUND"
    assert "unknown-deployment" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/deployments/ner/llm-b",
        "/deployments/llm/ner-a",
    ],
)
def test_get_deployment_with_wrong_path_kind_returns_safe_404(
    path: str,
) -> None:
    """경로 종류와 실제 Deployment 종류가 다르면 존재하지 않는 것처럼 처리합니다."""

    response = _get(
        path,
        RecordingRegistryManager(_deployments()),
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DEPLOYMENT_NOT_FOUND"


def test_list_deployments_returns_503_when_registry_is_not_initialized() -> None:
    """Runtime의 Registry가 준비되지 않았으면 공통 503 오류를 반환합니다."""

    response = _get(
        "/deployments/ner",
        RecordingRegistryManager(
            {},
            error=RegistryManagerNotInitializedError(
                "registry-internal-secret"
            ),
        ),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "REGISTRY_NOT_INITIALIZED"
    assert "registry-internal-secret" not in response.text


def test_list_deployments_returns_503_when_runtime_is_unavailable() -> None:
    """Lifespan 밖의 요청은 안전한 공통 Runtime 오류를 반환합니다."""

    registry_manager = RecordingRegistryManager({})
    application = create_app(
        runtime_factory=_runtime_factory(registry_manager)
    )
    client = TestClient(application)
    try:
        response = client.get("/deployments/llm")
    finally:
        client.close()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "APPLICATION_RUNTIME_UNAVAILABLE"
    )
    assert registry_manager.capture_count == 0
