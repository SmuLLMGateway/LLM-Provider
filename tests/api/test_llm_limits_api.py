"""LLM Deployment 컨텍스트 한도 조회 API 계약을 검증합니다."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from app.api.dependencies import get_llm_limits_service
from app.main import create_app
from app.registry.manager import RegistryManagerNotInitializedError
from app.schemas.deployments import LlmDeploymentLimitsResponse
from app.services.llm_limits import LlmLimitsNotFoundError


@dataclass(slots=True)
class RecordingLimitsService:
    """예약한 한도 응답 또는 오류를 반환합니다."""

    result: LlmDeploymentLimitsResponse | None = None
    error: Exception | None = None
    calls: list[str] | None = None

    async def get_limits(
        self,
        *,
        deployment_id: str,
    ) -> LlmDeploymentLimitsResponse:
        if self.calls is None:
            self.calls = []
        self.calls.append(deployment_id)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise RuntimeError("테스트 한도 응답이 없습니다")
        return self.result


def _get(service: RecordingLimitsService, deployment_id: str = "llm-a"):
    application = create_app()
    application.dependency_overrides[get_llm_limits_service] = lambda: service
    client = TestClient(application)
    try:
        return client.get(f"/deployments/llm/{deployment_id}/limits")
    finally:
        client.close()


def test_limits_api_returns_complete_nullable_contract() -> None:
    """자동 조회와 Registry를 조합한 한도를 camelCase로 반환합니다."""

    service = RecordingLimitsService(
        result=LlmDeploymentLimitsResponse(
            deploymentId="llm-a",
            modelContextWindowTokens=131072,
            runtimeContextWindowTokens=16384,
            effectiveContextWindowTokens=16384,
            source="ollama_runtime",
        )
    )

    response = _get(service)

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": "llm-a",
        "modelContextWindowTokens": 131072,
        "runtimeContextWindowTokens": 16384,
        "effectiveContextWindowTokens": 16384,
        "source": "ollama_runtime",
    }
    assert service.calls == ["llm-a"]


def test_limits_api_returns_explicit_unknown_nulls() -> None:
    """한도를 알 수 없으면 필드를 생략하지 않고 null과 source를 반환합니다."""

    response = _get(
        RecordingLimitsService(
            result=LlmDeploymentLimitsResponse(
                deploymentId="llm-a",
                source="unknown",
            )
        )
    )

    assert response.status_code == 200
    assert response.json() == {
        "deploymentId": "llm-a",
        "modelContextWindowTokens": None,
        "runtimeContextWindowTokens": None,
        "effectiveContextWindowTokens": None,
        "source": "unknown",
    }


def test_limits_api_maps_not_found_and_registry_errors() -> None:
    """조회 실패와 Registry 준비 실패를 안전한 공통 오류로 변환합니다."""

    not_found = _get(
        RecordingLimitsService(error=LlmLimitsNotFoundError("missing")),
        "missing",
    )
    unavailable = _get(
        RecordingLimitsService(
            error=RegistryManagerNotInitializedError("not ready")
        )
    )

    assert not_found.status_code == 404
    assert not_found.json()["detail"]["code"] == "DEPLOYMENT_NOT_FOUND"
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == (
        "REGISTRY_NOT_INITIALIZED"
    )


def test_openapi_exposes_llm_limits_endpoint() -> None:
    """Gateway가 신규 읽기 전용 Endpoint와 응답 모델을 확인할 수 있습니다."""

    operation = create_app().openapi()["paths"][
        "/deployments/llm/{deployment_id}/limits"
    ]["get"]

    assert "200" in operation["responses"]
    schema = operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["$ref"].endswith("/LlmDeploymentLimitsResponse")
