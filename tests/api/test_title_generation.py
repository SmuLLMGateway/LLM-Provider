"""대화 제목 생성 HTTP API 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backends.errors import BackendTimeoutError
from app.backends.provider_registry import (
    BackendProviderLookupError,
    BackendProviderRegistry,
)
from app.core.application_runtime import ApplicationRuntime
from app.main import create_app
from app.prompts.prompt_errors import PromptRenderError
from app.registry.deployment_resolver import DeploymentResolutionError
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.schemas.title_generation import GenerateTitleResponse
from app.services.adapter_catalog import AdapterCatalogService
from app.services.detection_pipeline import DetectionPipeline
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.generation_pipeline import GenerationPipeline
from app.services.title_generation_pipeline import (
    TitleBackendResultError,
    TitleGenerationPipeline,
)
from app.services.title_output_validator import (
    TitleOutputValidationError,
)


@dataclass(slots=True)
class RecordingTitleGenerationPipeline:
    """호출 인자를 기록하고 지정한 제목이나 예외를 반환합니다."""

    result: GenerateTitleResponse = field(
        default_factory=lambda: GenerateTitleResponse(
            title="자동 생성 제목"
        )
    )
    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def generate_title(
        self,
        *,
        text: str,
        llm_deployment_id: str,
    ) -> GenerateTitleResponse:
        """요청을 기록한 뒤 예약된 동작을 수행합니다."""

        self.calls.append((text, llm_deployment_id))
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(slots=True)
class RuntimeLifecycle:
    """가짜 Runtime의 진입과 종료 상태를 기록합니다."""

    entered: int = 0
    exited: int = 0


def _runtime_factory(
    pipeline: RecordingTitleGenerationPipeline,
) -> tuple[object, RuntimeLifecycle]:
    """제목 Pipeline을 주입하는 테스트 Runtime factory를 만듭니다."""

    lifecycle = RuntimeLifecycle()

    @asynccontextmanager
    async def factory() -> AsyncIterator[ApplicationRuntime]:
        """TestClient 수명 동안 가짜 Runtime을 제공합니다."""

        lifecycle.entered += 1
        try:
            yield ApplicationRuntime(
                http_client=cast(httpx.AsyncClient, object()),
                registry_manager=cast(RegistryManager, object()),
                backend_providers=cast(
                    BackendProviderRegistry,
                    object(),
                ),
                adapter_catalog_service=cast(
                    AdapterCatalogService,
                    object(),
                ),
                detection_pipeline=cast(
                    DetectionPipeline,
                    object(),
                ),
                generation_pipeline=cast(
                    GenerationPipeline,
                    object(),
                ),
                masking_pipeline=object(),  # type: ignore[arg-type]
                title_generation_pipeline=cast(
                    TitleGenerationPipeline,
                    pipeline,
                ),
                deployment_management_service=cast(
                    DeploymentManagementService,
                    object(),
                ),
            )
        finally:
            lifecycle.exited += 1

    return factory, lifecycle


def _post_title(
    pipeline: RecordingTitleGenerationPipeline,
    payload: object,
) -> tuple[httpx.Response, RuntimeLifecycle]:
    """주입한 Pipeline을 사용하는 제목 생성 요청을 보냅니다."""

    runtime_factory, lifecycle = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        response = client.post("/titles", json=payload)
    return response, lifecycle


def _valid_payload(text: object = "FastAPI 제목 기능") -> dict[str, object]:
    """필수 필드가 포함된 제목 생성 요청을 만듭니다."""

    return {
        "text": text,
        "llmDeploymentId": "llm-title",
    }


def test_generate_title_forwards_text_and_deployment_selection() -> None:
    """원문과 요청별 LLM Deployment ID를 Pipeline에 전달합니다."""

    pipeline = RecordingTitleGenerationPipeline(
        result=GenerateTitleResponse(title="FastAPI 제목 생성")
    )

    response, lifecycle = _post_title(
        pipeline,
        _valid_payload(),
    )

    assert response.status_code == 200
    assert response.json() == {"title": "FastAPI 제목 생성"}
    assert pipeline.calls == [
        ("FastAPI 제목 기능", "llm-title")
    ]
    assert lifecycle.entered == 1
    assert lifecycle.exited == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "원문"},
        {"llmDeploymentId": "llm-title"},
        _valid_payload(""),
        _valid_payload(1),
        {"text": "원문", "llmDeploymentId": None},
        {"text": "원문", "llmDeploymentId": "LLM TITLE"},
        {
            **_valid_payload(),
            "conversationId": "chat-a",
        },
    ],
)
def test_generate_title_rejects_invalid_request_before_pipeline(
    payload: object,
) -> None:
    """잘못된 요청을 422로 거부하고 모델 Pipeline을 호출하지 않습니다."""

    pipeline = RecordingTitleGenerationPipeline()

    response, _ = _post_title(pipeline, payload)

    assert response.status_code == 422
    assert pipeline.calls == []
    assert response.json()["detail"]["code"] == (
        "REQUEST_VALIDATION_FAILED"
    )


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            DeploymentResolutionError(
                "DEPLOYMENT_NOT_FOUND",
                deployment_id="missing",
                expected_kind="llm",
            ),
            404,
            "DEPLOYMENT_NOT_FOUND",
        ),
        (
            RegistryManagerNotInitializedError("초기화되지 않음"),
            503,
            "REGISTRY_NOT_INITIALIZED",
        ),
        (
            BackendProviderLookupError(
                "BACKEND_PROVIDER_NOT_REGISTERED",
                adapter_type="missing",
                kind="llm",
                deployment_id="llm-title",
            ),
            503,
            "BACKEND_PROVIDER_NOT_REGISTERED",
        ),
        (
            BackendTimeoutError(
                "TITLE_BACKEND_TIMEOUT",
                "모델 서버 응답 제한시간 초과",
            ),
            504,
            "TITLE_BACKEND_TIMEOUT",
        ),
        (
            TitleOutputValidationError("제목이 여러 줄입니다"),
            502,
            "TITLE_OUTPUT_INVALID",
        ),
        (
            PromptRenderError("렌더링 실패"),
            500,
            "TITLE_PROMPT_RENDER_FAILED",
        ),
        (
            TitleBackendResultError(
                deployment_id="llm-title",
                actual_type=dict,
            ),
            500,
            "TITLE_BACKEND_RESULT_INVALID",
        ),
    ],
)
def test_generate_title_maps_runtime_errors(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """Deployment, Backend, Prompt와 출력 오류를 안정적인 HTTP 계약으로 바꿉니다."""

    response, _ = _post_title(
        RecordingTitleGenerationPipeline(error=error),
        _valid_payload(),
    )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code


def test_generate_title_errors_do_not_expose_sensitive_input() -> None:
    """요청 원문과 모델 출력이 오류 응답에 포함되지 않게 합니다."""

    request_secret = "PRIVATE-CONVERSATION-9f42"
    model_secret = "PRIVATE-MODEL-OUTPUT-7c18"
    pipeline = RecordingTitleGenerationPipeline(
        error=TitleOutputValidationError(
            f"잘못된 출력 길이: {model_secret}"
        )
    )

    response, _ = _post_title(
        pipeline,
        _valid_payload(request_secret),
    )

    assert response.status_code == 502
    assert request_secret not in response.text
    assert model_secret not in response.text
    assert response.json() == {
        "detail": {
            "code": "TITLE_OUTPUT_INVALID",
            "message": "모델 서버 호출 또는 응답 처리에 실패했습니다",
        }
    }


def test_generate_title_returns_503_without_lifespan_runtime() -> None:
    """Lifespan이 시작되지 않은 요청에는 안전한 Runtime 오류를 냅니다."""

    runtime_factory, lifecycle = _runtime_factory(
        RecordingTitleGenerationPipeline()
    )
    application = create_app(runtime_factory=runtime_factory)
    client = TestClient(application)
    try:
        response = client.post(
            "/titles",
            json=_valid_payload(),
        )
    finally:
        client.close()

    assert lifecycle.entered == 0
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "APPLICATION_RUNTIME_UNAVAILABLE"
    )
