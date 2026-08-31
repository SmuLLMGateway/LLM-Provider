"""Local LLM 마스킹 HTTP API의 경계와 오류 계약을 검증합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.backends.errors import (
    BackendConfigurationError,
    BackendInputTooLargeError,
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
from app.prompts.prompt_errors import PromptContextTooLargeError, PromptRenderError
from app.registry.deployment_resolver import DeploymentResolutionError
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.schemas.detection import Detection
from app.schemas.masking import MaskReplacement, MaskResponse
from app.services.adapter_catalog import AdapterCatalogService
from app.services.deployment_management import DeploymentManagementService
from app.services.detection_pipeline import DetectionPipeline
from app.services.generation_pipeline import GenerationPipeline
from app.services.llm_masking_output_parser import LlmMaskingOutputError
from app.services.llm_masking_output_validator import (
    LlmMaskingValidationError,
)
from app.services.masking_pipeline import (
    MaskingBackendResultError,
    MaskingInputSerializationError,
    MaskingNamespaceError,
    MaskingPipeline,
)
from app.services.title_generation_pipeline import TitleGenerationPipeline


_TEXT = "홍길동은 승인했습니다."


@dataclass(slots=True)
class RecordingMaskingPipeline:
    """호출 인자와 지정한 마스킹 응답 또는 오류를 기록합니다."""

    result: MaskResponse = field(
        default_factory=lambda: MaskResponse.model_validate(
            {"maskedText": "원문", "replacements": []}
        )
    )
    error: Exception | None = None
    calls: list[tuple[str, str, tuple[Detection, ...]]] = field(
        default_factory=list
    )

    async def mask(
        self,
        *,
        text: str,
        llm_deployment_id: str,
        detections: tuple[Detection, ...],
    ) -> MaskResponse:
        """호출 값을 기록한 뒤 예약 동작을 수행합니다."""

        self.calls.append((text, llm_deployment_id, tuple(detections)))
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(slots=True)
class RuntimeLifecycle:
    """주입 Runtime의 진입·종료 횟수와 객체를 보관합니다."""

    entered: int = 0
    exited: int = 0
    runtime: ApplicationRuntime | None = None


def _runtime_factory(
    pipeline: RecordingMaskingPipeline,
) -> tuple[object, RuntimeLifecycle]:
    """마스킹 Pipeline만 실제 테스트 대역인 Runtime을 만듭니다."""

    lifecycle = RuntimeLifecycle()

    @asynccontextmanager
    async def factory() -> AsyncIterator[ApplicationRuntime]:
        lifecycle.entered += 1
        runtime = ApplicationRuntime(
            http_client=cast(httpx.AsyncClient, object()),
            registry_manager=cast(RegistryManager, object()),
            backend_providers=cast(BackendProviderRegistry, object()),
            adapter_catalog_service=cast(AdapterCatalogService, object()),
            detection_pipeline=cast(DetectionPipeline, object()),
            generation_pipeline=cast(GenerationPipeline, object()),
            masking_pipeline=cast(MaskingPipeline, pipeline),
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


def _post_mask(
    pipeline: RecordingMaskingPipeline,
    payload: object,
) -> tuple[httpx.Response, RuntimeLifecycle]:
    """주입한 Pipeline을 사용하는 /mask JSON 요청을 보냅니다."""

    runtime_factory, lifecycle = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        response = client.post("/mask", json=payload)
    return response, lifecycle


def _detection_data(
    *,
    text: str = _TEXT,
    needle: str = "홍길동",
    source: str = "ner",
) -> dict[str, object]:
    """원문의 실제 span을 사용하는 요청 Detection을 만듭니다."""

    start = text.index(needle)
    return {
        "start": start,
        "end": start + len(needle),
        "text": needle,
        "type": "PERSONAL_IDENTITY",
        "policyId": "P01",
        "source": source,
        "score": 0.9,
    }


def _valid_payload(
    text: object = _TEXT,
    *,
    detections: object | None = None,
) -> dict[str, object]:
    """필수 필드가 모두 있는 정상 API 요청을 만듭니다."""

    return {
        "text": text,
        "llmDeploymentId": "llm-mask",
        "detections": (
            [_detection_data()] if detections is None else detections
        ),
    }


def _assert_error(
    response: httpx.Response,
    *,
    status_code: int,
    code: str,
) -> None:
    """공통 오류 응답이 안전한 두 필드만 갖는지 확인합니다."""

    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"detail"}
    assert set(body["detail"]) == {"code", "message"}
    assert body["detail"]["code"] == code
    assert type(body["detail"]["message"]) is str
    assert body["detail"]["message"]


def test_mask_forwards_validated_request_to_pipeline() -> None:
    """원문, LLM ID와 불변 전체 Detection을 Pipeline에 전달합니다."""

    pipeline = RecordingMaskingPipeline()
    payload = _valid_payload()

    response, lifecycle = _post_mask(pipeline, payload)

    assert response.status_code == 200
    assert len(pipeline.calls) == 1
    text, deployment_id, detections = pipeline.calls[0]
    assert text == _TEXT
    assert deployment_id == "llm-mask"
    assert isinstance(detections, tuple)
    assert len(detections) == 1
    assert detections[0].text == "홍길동"
    assert lifecycle.entered == 1
    assert lifecycle.exited == 1


def test_mask_serializes_exact_camel_case_response() -> None:
    """검증된 문자열과 원문 좌표 replacement를 정확히 직렬화합니다."""

    placeholder = "[[LPL_0123456789abcdef_0001]]"
    replacement = MaskReplacement.model_validate(
        {
            "start": 0,
            "end": 3,
            "entityId": "entity-1",
            "placeholder": placeholder,
            "types": ["PERSONAL_IDENTITY"],
            "sources": ["ner"],
        }
    )
    pipeline = RecordingMaskingPipeline(
        result=MaskResponse.model_validate(
            {
                "maskedText": placeholder + _TEXT[3:],
                "replacements": [replacement],
            }
        )
    )

    response, _ = _post_mask(pipeline, _valid_payload())

    assert response.status_code == 200
    assert response.json() == {
        "maskedText": placeholder + _TEXT[3:],
        "replacements": [
            {
                "start": 0,
                "end": 3,
                "entityId": "entity-1",
                "placeholder": placeholder,
                "types": ["PERSONAL_IDENTITY"],
                "sources": ["ner"],
            }
        ],
    }


def test_mask_forwards_explicit_empty_detections() -> None:
    """명시적인 빈 Detection 배열을 생략하지 않고 Pipeline에 전달합니다."""

    pipeline = RecordingMaskingPipeline(
        result=MaskResponse.model_validate(
            {"maskedText": _TEXT, "replacements": []}
        )
    )

    response, _ = _post_mask(
        pipeline,
        _valid_payload(detections=[]),
    )

    assert response.status_code == 200
    assert pipeline.calls == [(_TEXT, "llm-mask", ())]
    assert response.json() == {"maskedText": _TEXT, "replacements": []}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": _TEXT, "llmDeploymentId": "llm-mask"},
        {"text": _TEXT, "detections": []},
        {"llmDeploymentId": "llm-mask", "detections": []},
        _valid_payload(""),
        _valid_payload(1),
        {**_valid_payload(), "llmDeploymentId": None},
        {**_valid_payload(), "llmDeploymentId": "LLM MASK"},
        {**_valid_payload(), "detections": {}},
        {
            **_valid_payload(),
            "detections": [{**_detection_data(), "text": "김철수"}],
        },
        {
            **_valid_payload(),
            "detections": [{**_detection_data(), "end": 100}],
        },
        {
            **_valid_payload(),
            "detections": [{**_detection_data(), "source": "external"}],
        },
        {
            **_valid_payload(),
            "detections": [{**_detection_data(), "type": "UNKNOWN"}],
        },
        {**_valid_payload(), "unknownField": True},
        {
            "text": _TEXT,
            "llm_deployment_id": "llm-mask",
            "detections": [],
        },
    ],
)
def test_mask_rejects_invalid_request_before_pipeline(
    payload: object,
) -> None:
    """스키마 오류를 422로 거부하고 Pipeline을 실행하지 않습니다."""

    pipeline = RecordingMaskingPipeline()

    response, _ = _post_mask(pipeline, payload)

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert pipeline.calls == []


def test_mask_rejects_malformed_http_json_without_pipeline_call() -> None:
    """파싱할 수 없는 HTTP JSON 본문도 공통 422 계약으로 처리합니다."""

    pipeline = RecordingMaskingPipeline()
    runtime_factory, _ = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)
    with TestClient(application) as client:
        response = client.post(
            "/mask",
            content=b'{"text":',
            headers={"content-type": "application/json"},
        )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert pipeline.calls == []


def test_mask_validation_error_does_not_echo_request_text() -> None:
    """잘못된 타입에 숨긴 민감 원문을 422 응답에 복사하지 않습니다."""

    secret = "REQUEST-MASK-SECRET-4a2f"

    response, _ = _post_mask(
        RecordingMaskingPipeline(),
        _valid_payload([secret]),
    )

    _assert_error(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert secret not in response.text


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
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
            DeploymentResolutionError(
                "DEPLOYMENT_DISABLED",
                deployment_id="disabled",
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
            RegistryManagerNotInitializedError("not initialized"),
            503,
            "REGISTRY_NOT_INITIALIZED",
        ),
        (
            BackendProviderLookupError(
                "BACKEND_PROVIDER_NOT_REGISTERED",
                adapter_type="missing",
                kind="llm",
                deployment_id="llm-mask",
            ),
            503,
            "BACKEND_PROVIDER_NOT_REGISTERED",
        ),
    ],
)
def test_mask_maps_deployment_registry_and_provider_errors(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """실행 구성 오류를 기존 Gateway 상태·code 계약으로 매핑합니다."""

    response, _ = _post_mask(
        RecordingMaskingPipeline(error=error),
        _valid_payload(),
    )

    _assert_error(response, status_code=status_code, code=code)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            BackendTimeoutError("MASK_TIMEOUT", "timeout"),
            504,
            "MASK_TIMEOUT",
        ),
        (
            BackendTransportError("MASK_TRANSPORT", "transport"),
            502,
            "MASK_TRANSPORT",
        ),
        (
            BackendResponseError("MASK_RESPONSE", "response"),
            502,
            "MASK_RESPONSE",
        ),
        (
            BackendConfigurationError("MASK_CONFIG", "config"),
            500,
            "MASK_CONFIG",
        ),
        (
            BackendInputTooLargeError("MASK_INPUT_TOO_LARGE", "large"),
            413,
            "MASK_INPUT_TOO_LARGE",
        ),
    ],
)
def test_mask_maps_common_backend_errors(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """공통 Backend 오류 Handler가 /mask에도 동일하게 적용됩니다."""

    response, _ = _post_mask(
        RecordingMaskingPipeline(error=error),
        _valid_payload(),
    )

    _assert_error(response, status_code=status_code, code=code)


@pytest.mark.parametrize(
    "error",
    [
        LlmMaskingOutputError("LLM_MASKING_OUTPUT_INVALID_JSON"),
        LlmMaskingOutputError("LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL"),
        LlmMaskingOutputError(
            "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT",
            item_index=0,
        ),
        LlmMaskingOutputError(
            "LLM_MASKING_OUTPUT_TOO_LARGE",
            actual_bytes=2,
            max_output_bytes=1,
        ),
        LlmMaskingOutputError(
            "LLM_MASKING_OUTPUT_TOO_MANY_ASSIGNMENTS",
            actual_items=2,
            max_assignments=1,
        ),
    ],
)
def test_mask_maps_llm_json_errors_to_bad_gateway(
    error: LlmMaskingOutputError,
) -> None:
    """비신뢰 모델 JSON 오류 code를 보존하고 502로 fail-closed합니다."""

    response, _ = _post_mask(
        RecordingMaskingPipeline(error=error),
        _valid_payload(),
    )

    _assert_error(response, status_code=502, code=error.code)


@pytest.mark.parametrize(
    "code",
    [
        "LLM_MASKING_TARGET_MISMATCH",
        "LLM_MASKING_ENTITY_INVALID",
        "LLM_MASKING_TEXT_MISMATCH",
        "LLM_MASKING_PLACEHOLDER_COLLISION",
    ],
)
def test_mask_maps_security_validation_errors_to_bad_gateway(
    code: str,
) -> None:
    """coverage·entity·원문·token 불변식 실패는 502로 전체 거부합니다."""

    error = LlmMaskingValidationError(code)  # type: ignore[arg-type]
    response, _ = _post_mask(
        RecordingMaskingPipeline(error=error),
        _valid_payload(),
    )

    _assert_error(response, status_code=502, code=code)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            DetectionSpanValidationError("TEXT_MISMATCH", item_index=0),
            422,
            "MASK_DETECTIONS_INVALID",
        ),
        (
            PromptContextTooLargeError(2, 1),
            413,
            "MASK_REQUEST_TOO_LARGE",
        ),
        (
            PromptRenderError("render failed"),
            500,
            "MASK_PROMPT_RENDER_FAILED",
        ),
        (
            MaskingBackendResultError(
                deployment_id="llm-mask",
                actual_type=dict,
            ),
            500,
            "MASKING_BACKEND_RESULT_INVALID",
        ),
        (
            MaskingInputSerializationError(),
            500,
            "MASKING_INTERNAL_VALIDATION_FAILED",
        ),
        (
            MaskingNamespaceError(),
            500,
            "MASKING_INTERNAL_VALIDATION_FAILED",
        ),
    ],
)
def test_mask_maps_pipeline_boundary_errors(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    """Span·크기·Prompt·내부 계약 오류를 안정적인 공개 code로 바꿉니다."""

    response, _ = _post_mask(
        RecordingMaskingPipeline(error=error),
        _valid_payload(),
    )

    _assert_error(response, status_code=status_code, code=code)


def test_mask_error_response_exposes_neither_request_nor_backend_secret() -> None:
    """오류 detail에서 원문과 Provider 원문을 모두 제거합니다."""

    request_secret = "REQUEST-SECRET-7f4d2c"
    provider_secret = "PROVIDER-SECRET-2d95a1"
    pipeline = RecordingMaskingPipeline(
        error=BackendResponseError(
            "MASK_PROVIDER_INVALID",
            f"잘못된 Provider 응답: {provider_secret}",
        )
    )

    response, _ = _post_mask(
        pipeline,
        {
            "text": request_secret,
            "llmDeploymentId": "llm-mask",
            "detections": [],
        },
    )

    _assert_error(
        response,
        status_code=502,
        code="MASK_PROVIDER_INVALID",
    )
    assert request_secret not in response.text
    assert provider_secret not in response.text


def test_create_app_enters_and_exits_mask_runtime_factory() -> None:
    """TestClient 수명 동안 주입 Runtime을 정확히 게시하고 제거합니다."""

    pipeline = RecordingMaskingPipeline()
    runtime_factory, lifecycle = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)

    assert not hasattr(application.state, "runtime")
    with TestClient(application) as client:
        assert lifecycle.entered == 1
        assert lifecycle.exited == 0
        assert application.state.runtime is lifecycle.runtime
        response = client.post("/mask", json=_valid_payload())
        assert response.status_code == 200

    assert lifecycle.entered == 1
    assert lifecycle.exited == 1
    assert not hasattr(application.state, "runtime")


def test_mask_returns_503_without_lifespan_runtime() -> None:
    """Lifespan을 시작하지 않은 요청은 Runtime 미준비 오류를 반환합니다."""

    runtime_factory, lifecycle = _runtime_factory(
        RecordingMaskingPipeline()
    )
    application = create_app(runtime_factory=runtime_factory)
    client = TestClient(application)
    try:
        response = client.post("/mask", json=_valid_payload())
    finally:
        client.close()

    assert lifecycle.entered == 0
    _assert_error(
        response,
        status_code=503,
        code="APPLICATION_RUNTIME_UNAVAILABLE",
    )
