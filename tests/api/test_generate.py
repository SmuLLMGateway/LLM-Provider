"""Option A 로컬 생성 HTTP API 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backends.errors import (
    BackendError,
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
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.registry.deployment_resolver import DeploymentResolutionError
from app.schemas.generation import (
    LlmResult,
    LlmTokenUsage,
    PreviousTextMessage,
)
from app.services.adapter_catalog import AdapterCatalogService
from app.services.detection_pipeline import DetectionPipeline
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.generation_pipeline import (
    GenerationBackendResultError,
    GenerationPipeline,
)
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


@dataclass(slots=True)
class RecordingGenerationPipeline:
    """호출 인자를 기록하고 지정한 결과나 예외를 반환합니다."""

    result: LlmResult = field(
        default_factory=lambda: LlmResult(text="생성 결과")
    )
    error: Exception | None = None
    calls: list[
        tuple[str, tuple[PreviousTextMessage, ...], str]
    ] = field(
        default_factory=list
    )

    async def generate(
        self,
        *,
        text: str,
        llm_deployment_id: str,
        previous_text: tuple[PreviousTextMessage, ...] = (),
    ) -> LlmResult:
        """호출 내용을 복사해 보관한 뒤 예약된 동작을 수행합니다."""

        self.calls.append((text, previous_text, llm_deployment_id))
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(slots=True)
class RuntimeLifecycle:
    """가짜 애플리케이션 Runtime의 진입과 종료 횟수를 기록합니다."""

    entered: int = 0
    exited: int = 0
    runtime: ApplicationRuntime | None = None


def _runtime_factory(
    pipeline: RecordingGenerationPipeline,
) -> tuple[object, RuntimeLifecycle]:
    """테스트 Pipeline을 제공하는 주입 가능한 Runtime factory를 만듭니다."""

    lifecycle = RuntimeLifecycle()

    @asynccontextmanager
    async def factory() -> AsyncIterator[ApplicationRuntime]:
        """Runtime을 한 번 제공하고 종료 여부를 항상 기록합니다."""

        lifecycle.entered += 1
        runtime = ApplicationRuntime(
            http_client=cast(httpx.AsyncClient, object()),
            registry_manager=cast(RegistryManager, object()),
            backend_providers=cast(BackendProviderRegistry, object()),
            adapter_catalog_service=cast(
                AdapterCatalogService,
                object(),
            ),
            detection_pipeline=cast(DetectionPipeline, object()),
            generation_pipeline=cast(GenerationPipeline, pipeline),
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
        lifecycle.runtime = runtime
        try:
            yield runtime
        finally:
            lifecycle.exited += 1

    return factory, lifecycle


def _post_generate(
    pipeline: RecordingGenerationPipeline,
    payload: object,
) -> tuple[httpx.Response, RuntimeLifecycle]:
    """가짜 Runtime으로 애플리케이션을 시작해 생성 요청을 보냅니다."""

    runtime_factory, lifecycle = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)

    with TestClient(application) as client:
        response = client.post("/generate", json=payload)

    return response, lifecycle


def _valid_generate_payload(text: object) -> dict[str, object]:
    """필수 LLM Deployment ID가 포함된 생성 요청 본문을 만듭니다."""

    return {
        "text": text,
        "llmDeploymentId": "llm-a",
    }


@pytest.mark.parametrize(
    ("payload", "expected_call"),
    [
        (
            {
                "text": "명시 Deployment 질문",
                "previousText": [
                    {"role": "user", "content": "첫 질문"},
                    {"role": "assistant", "content": "첫 답변"},
                ],
                "llmDeploymentId": "llm-a",
            },
            (
                "명시 Deployment 질문",
                (
                    PreviousTextMessage(
                        role="user",
                        content="첫 질문",
                    ),
                    PreviousTextMessage(
                        role="assistant",
                        content="첫 답변",
                    ),
                ),
                "llm-a",
            ),
        ),
    ],
)
def test_generate_forwards_text_and_llm_deployment_selection(
    payload: dict[str, object],
    expected_call: tuple[
        str,
        tuple[PreviousTextMessage, ...],
        str,
    ],
) -> None:
    """현재·이전 입력과 필수 LLM Deployment ID를 Pipeline에 전달합니다."""

    pipeline = RecordingGenerationPipeline()

    response, lifecycle = _post_generate(pipeline, payload)

    assert response.status_code == 200
    assert pipeline.calls == [expected_call]
    assert lifecycle.entered == 1
    assert lifecycle.exited == 1


def test_generate_serializes_llm_result_with_camel_case_aliases() -> None:
    """공통 LLM 결과를 선택 필드 없이 camelCase API JSON으로 반환합니다."""

    pipeline = RecordingGenerationPipeline(
        result=LlmResult(
            text="요약 결과",
            model_name="local-model-a",
            finish_reason="stop",
            usage=LlmTokenUsage(
                input_tokens=10,
                output_tokens=4,
                total_tokens=14,
            ),
        )
    )

    response, _ = _post_generate(
        pipeline,
        _valid_generate_payload("요약해 주세요"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "요약 결과",
        "modelName": "local-model-a",
        "finishReason": "stop",
        "usage": {
            "inputTokens": 10,
            "outputTokens": 4,
            "totalTokens": 14,
        },
    }


def test_generate_excludes_missing_optional_result_fields() -> None:
    """Provider가 주지 않은 선택 결과 필드를 null로 노출하지 않습니다."""

    response, _ = _post_generate(
        RecordingGenerationPipeline(result=LlmResult(text="완료")),
        _valid_generate_payload("명시 Deployment로 실행"),
    )

    assert response.status_code == 200
    assert response.json() == {"text": "완료"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"llmDeploymentId": "llm-a"},
        {"text": "LLM Deployment ID 누락"},
        {"text": "null LLM ID", "llmDeploymentId": None},
        _valid_generate_payload(""),
        _valid_generate_payload(1),
        {
            **_valid_generate_payload("질문"),
            "previousText": None,
        },
        {
            **_valid_generate_payload("질문"),
            "previousText": "이전 답변",
        },
        {
            **_valid_generate_payload("질문"),
            "previousText": ["이전 답변"],
        },
        {
            **_valid_generate_payload("질문"),
            "previousText": [
                {"role": "system", "content": "허용 안 됨"}
            ],
        },
        {
            **_valid_generate_payload("질문"),
            "previousText": [{"role": "user", "content": ""}],
        },
        {
            "text": "질문",
            "llmDeploymentId": "llm-a",
            "variables": [],
        },
        {
            "text": "질문",
            "llmDeploymentId": "llm-a",
            "unknownField": True,
        },
        {
            "text": "질문",
            "llmDeploymentId": "LLM A",
        },
        {
            **_valid_generate_payload("질문"),
            "profileId": "profile-a",
        },
    ],
)
def test_generate_rejects_invalid_requests_before_pipeline_call(
    payload: object,
) -> None:
    """누락·잘못된 타입·추가 필드 입력을 422로 거부하고 실행하지 않습니다."""

    pipeline = RecordingGenerationPipeline()

    response, _ = _post_generate(pipeline, payload)

    assert response.status_code == 422
    assert pipeline.calls == []


def test_request_validation_error_does_not_echo_invalid_input() -> None:
    """Pydantic 요청 오류 응답에도 사용자가 보낸 원문을 포함하지 않습니다."""

    request_secret = "REQUEST-VALIDATION-SECRET-4a2f"

    response, _ = _post_generate(
        RecordingGenerationPipeline(),
        _valid_generate_payload([request_secret]),
    )

    assert response.status_code == 422
    assert request_secret not in response.text
    assert response.json() == {
        "detail": {
            "code": "REQUEST_VALIDATION_FAILED",
            "message": (
                "요청 검증에 실패했습니다: "
                "body.text: 문자열이어야 합니다"
            ),
        }
    }


def test_request_validation_error_does_not_echo_unknown_field_name() -> None:
    """추가 필드의 이름도 사용자 입력으로 보고 오류 응답에 복사하지 않습니다."""

    secret_field = "REQUEST_VALIDATION_SECRET_FIELD"
    payload = _valid_generate_payload("질문")
    payload[secret_field] = True

    response, _ = _post_generate(
        RecordingGenerationPipeline(),
        payload,
    )

    assert response.status_code == 422
    assert secret_field not in response.text
    assert response.json()["detail"]["message"] == (
        "요청 검증에 실패했습니다: "
        "body: API 계약에 정의되지 않은 추가 필드가 있습니다"
    )


def test_request_validation_error_explains_malformed_json() -> None:
    """JSON 문법 오류는 내부 파서 문장 없이 원인을 명확하게 안내합니다."""

    pipeline = RecordingGenerationPipeline()
    runtime_factory, _ = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)

    with TestClient(application) as client:
        response = client.post(
            "/generate",
            content=b'{"text":',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "REQUEST_VALIDATION_FAILED",
            "message": (
                "요청 검증에 실패했습니다: "
                "body: 올바른 JSON 문법이 아닙니다"
            ),
        }
    }
    assert pipeline.calls == []


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            DeploymentResolutionError(
                "DEPLOYMENT_NOT_FOUND",
                deployment_id="missing-llm",
                expected_kind="llm",
            ),
            404,
            "DEPLOYMENT_NOT_FOUND",
        ),
        (
            DeploymentResolutionError(
                "DEPLOYMENT_DISABLED",
                deployment_id="llm-disabled",
                expected_kind="llm",
            ),
            409,
            "DEPLOYMENT_DISABLED",
        ),
        (
            DeploymentResolutionError(
                "DEPLOYMENT_KIND_MISMATCH",
                deployment_id="ner-a",
                expected_kind="llm",
            ),
            422,
            "DEPLOYMENT_KIND_MISMATCH",
        ),
        (
            RegistryManagerNotInitializedError("초기화되지 않음"),
            503,
            "REGISTRY_NOT_INITIALIZED",
        ),
        (
            BackendProviderLookupError(
                "BACKEND_PROVIDER_NOT_REGISTERED",
                adapter_type="missing_adapter",
                kind="llm",
                deployment_id="llm-a",
            ),
            503,
            "BACKEND_PROVIDER_NOT_REGISTERED",
        ),
    ],
)
def test_generate_maps_deployment_registry_and_provider_errors(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """Deployment 선택과 실행 준비 오류를 안정적인 HTTP 상태와 코드로 변환합니다."""

    response, _ = _post_generate(
        RecordingGenerationPipeline(error=error),
        _valid_generate_payload("오류 매핑 테스트"),
    )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            BackendTimeoutError(
                "TEST_BACKEND_TIMEOUT",
                "모델 서버 요청 시간이 초과되었습니다",
            ),
            504,
            "TEST_BACKEND_TIMEOUT",
        ),
        (
            BackendTransportError(
                "TEST_BACKEND_TRANSPORT_FAILED",
                "모델 서버 연결 오류",
            ),
            502,
            "TEST_BACKEND_TRANSPORT_FAILED",
        ),
        (
            BackendResponseError(
                "TEST_BACKEND_RESPONSE_INVALID",
                "Provider 응답 구조 오류",
            ),
            502,
            "TEST_BACKEND_RESPONSE_INVALID",
        ),
        (
            BackendConfigurationError(
                "TEST_BACKEND_CONFIGURATION_INVALID",
                "Deployment 설정 오류",
            ),
            500,
            "TEST_BACKEND_CONFIGURATION_INVALID",
        ),
        (
            GenerationBackendResultError(
                deployment_id="llm-a",
                actual_type=dict,
            ),
            500,
            "GENERATION_BACKEND_RESULT_INVALID",
        ),
    ],
)
def test_generate_maps_backend_errors(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """모델 서버와 Backend 계약 오류를 경계에 맞는 상태로 변환합니다."""

    response, _ = _post_generate(
        RecordingGenerationPipeline(error=error),
        _valid_generate_payload("Backend 오류 매핑 테스트"),
    )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize(
    ("error_type", "expected_status", "expected_code"),
    [
        (BackendTimeoutError, 504, "TEST_BACKEND_TIMEOUT"),
        (BackendTransportError, 502, "TEST_BACKEND_TRANSPORT_FAILED"),
        (BackendResponseError, 502, "TEST_BACKEND_RESPONSE_INVALID"),
        (
            BackendConfigurationError,
            500,
            "TEST_BACKEND_CONFIGURATION_INVALID",
        ),
    ],
)
def test_generate_does_not_expose_request_or_backend_sensitive_text(
    error_type: type[BackendError],
    expected_status: int,
    expected_code: str,
) -> None:
    """정규화한 오류 응답에 요청 원문이나 Provider 오류 원문을 노출하지 않습니다."""

    request_secret = "request-secret-7f4d2c"
    provider_secret = "PROVIDER-SECRET-2d95a1"
    pipeline = RecordingGenerationPipeline(
        error=error_type(
            expected_code,
            f"잘못된 Provider 응답: {provider_secret}",
        )
    )

    response, _ = _post_generate(
        pipeline,
        _valid_generate_payload(request_secret),
    )

    assert response.status_code == expected_status
    assert request_secret not in response.text
    assert provider_secret not in response.text
    body = response.json()
    assert set(body) == {"detail"}
    assert set(body["detail"]) == {"code", "message"}
    assert body["detail"]["code"] == expected_code
    assert isinstance(body["detail"]["message"], str)
    assert body["detail"]["message"]


def test_create_app_enters_and_exits_injected_runtime_factory() -> None:
    """TestClient 수명 동안 Runtime을 게시하고 종료 시 상태에서 제거합니다."""

    pipeline = RecordingGenerationPipeline()
    runtime_factory, lifecycle = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)

    assert lifecycle.entered == 0
    assert lifecycle.exited == 0
    assert not hasattr(application.state, "runtime")

    with TestClient(application) as client:
        assert lifecycle.entered == 1
        assert lifecycle.exited == 0
        assert application.state.runtime is lifecycle.runtime

        response = client.post(
            "/generate",
            json=_valid_generate_payload("수명 주기 테스트"),
        )
        assert response.status_code == 200

    assert lifecycle.entered == 1
    assert lifecycle.exited == 1
    assert not hasattr(application.state, "runtime")


def test_generate_returns_503_when_lifespan_runtime_is_unavailable() -> None:
    """Lifespan을 시작하지 않은 요청에는 안전한 Runtime 오류를 반환합니다."""

    runtime_factory, lifecycle = _runtime_factory(
        RecordingGenerationPipeline()
    )
    application = create_app(runtime_factory=runtime_factory)
    client = TestClient(application)
    try:
        response = client.post(
            "/generate",
            json=_valid_generate_payload("Runtime 없음 테스트"),
        )
    finally:
        client.close()

    assert lifecycle.entered == 0
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "APPLICATION_RUNTIME_UNAVAILABLE",
            "message": "애플리케이션 실행 구성이 준비되지 않았습니다",
        }
    }
