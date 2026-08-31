"""전역 활성 Policy ID 조회·전체 교체 API 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backends.provider_registry import BackendProviderRegistry
from app.core.application_runtime import ApplicationRuntime
from app.main import create_app
from app.registry.manager import RegistryManager
from app.services.adapter_catalog import AdapterCatalogService
from app.services.deployment_management import DeploymentManagementService
from app.services.detection_pipeline import DetectionPipeline
from app.services.generation_pipeline import GenerationPipeline
from app.services.masking_pipeline import MaskingPipeline
from app.services.policy_settings import (
    PolicySettingsFileStore,
    PolicySettingsManager,
    PolicySettingsStorageError,
)
from app.services.title_generation_pipeline import TitleGenerationPipeline


def _client(manager: object | None) -> TestClient:
    """지정한 정책 Manager를 가진 최소 Runtime 애플리케이션을 만듭니다."""

    @asynccontextmanager
    async def runtime_factory() -> AsyncIterator[ApplicationRuntime]:
        yield ApplicationRuntime(
            http_client=cast(httpx.AsyncClient, object()),
            registry_manager=cast(RegistryManager, object()),
            backend_providers=cast(BackendProviderRegistry, object()),
            adapter_catalog_service=cast(AdapterCatalogService, object()),
            detection_pipeline=cast(DetectionPipeline, object()),
            generation_pipeline=cast(GenerationPipeline, object()),
            masking_pipeline=cast(MaskingPipeline, object()),
            title_generation_pipeline=cast(TitleGenerationPipeline, object()),
            deployment_management_service=cast(
                DeploymentManagementService,
                object(),
            ),
            policy_settings_manager=cast(PolicySettingsManager | None, manager),
        )

    return TestClient(create_app(runtime_factory=runtime_factory))


def test_get_and_put_enabled_policies_persist_and_activate(tmp_path) -> None:
    """GET 기본값과 PUT 원자 저장·즉시 반영을 실제 Manager로 확인합니다."""

    manager = PolicySettingsManager(PolicySettingsFileStore(tmp_path))
    manager.initialize()

    with _client(manager) as client:
        initial = client.get("/policies/enabled")
        updated = client.put(
            "/policies/enabled",
            json={"enabledPolicies": ["B01", "P03", "P01"]},
        )
        captured = client.get("/policies/enabled")

    assert initial.status_code == 200
    assert len(initial.json()["enabledPolicies"]) == 14
    assert updated.status_code == 200
    assert updated.json() == {
        "enabledPolicies": ["P01", "P03", "B01"]
    }
    assert captured.json() == updated.json()
    assert PolicySettingsFileStore(tmp_path).load_or_initialize() == (
        manager.capture()
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"enabledPolicies": []},
        {"enabledPolicies": ["P01", "P01"]},
        {"enabledPolicies": ["UNKNOWN"]},
        {"enabledPolicies": ["P01"], "extra": True},
        {"enabled_policy_ids": ["P01"]},
    ],
)
def test_put_rejects_invalid_contract(tmp_path, payload) -> None:
    """필수·비어 있지 않음·중복·등록·camelCase 계약 위반은 422입니다."""

    manager = PolicySettingsManager(PolicySettingsFileStore(tmp_path))
    manager.initialize()

    with _client(manager) as client:
        response = client.put("/policies/enabled", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_policy_api_reports_unavailable_manager() -> None:
    """Runtime에 Manager가 없으면 설정 API를 503으로 거부합니다."""

    with _client(None) as client:
        response = client.get("/policies/enabled")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "POLICY_SETTINGS_NOT_INITIALIZED"
    )


class FailingManager:
    """PUT 저장 실패를 발생시키는 Manager 대역입니다."""

    def capture(self):
        raise AssertionError

    def replace(self, request):
        del request
        raise PolicySettingsStorageError("failed")


def test_put_maps_storage_failure_to_safe_error() -> None:
    """파일 저장 실패를 안전한 500 응답으로 변환합니다."""

    with _client(FailingManager()) as client:
        response = client.put(
            "/policies/enabled",
            json={"enabledPolicies": ["P01"]},
        )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == (
        "POLICY_SETTINGS_STORAGE_FAILED"
    )
