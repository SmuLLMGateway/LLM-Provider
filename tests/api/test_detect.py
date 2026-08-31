"""로컬 민감정보 탐지 HTTP API 계약을 검증합니다."""

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
from app.policies.organization_profile_validator import (
    OrganizationProfilePolicyError,
)
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
)
from app.registry.deployment_resolver import DeploymentResolutionError
from app.prompts.prompt_errors import (
    PromptContextTooLargeError,
    PromptOutputTooLargeError,
    PromptRenderError,
)
from app.schemas.detection import (
    DetectResponse,
    Detection,
    RegexCandidate,
    RegexCandidateDecision,
)
from app.schemas.detection_context import (
    OrganizationProfile,
    SourceType,
)
from app.services.adapter_catalog import AdapterCatalogService
from app.services.detection_pipeline import (
    DetectionBackendResultError,
    DetectionPipeline,
    DetectionPolicySelectionError,
)
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.generation_pipeline import GenerationPipeline
from app.services.llm_detection_output_parser import (
    LlmDetectionOutputError,
)
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


@dataclass(slots=True)
class RecordingDetectionPipeline:
    """호출 인자를 기록하고 지정한 탐지 결과나 예외를 반환합니다."""

    result: DetectResponse = field(default_factory=DetectResponse)
    error: Exception | None = None
    calls: list[
        tuple[
            str,
            str,
            str,
            OrganizationProfile | None,
            SourceType,
            tuple[RegexCandidate, ...],
        ]
    ] = field(default_factory=list)

    async def detect(
        self,
        *,
        text: str,
        ner_deployment_id: str,
        llm_deployment_id: str,
        organization_profile: OrganizationProfile | None,
        source_type: SourceType,
        regex_candidates: tuple[RegexCandidate, ...] = (),
    ) -> DetectResponse:
        """전달된 요청을 보관한 뒤 예약된 동작을 수행합니다."""

        self.calls.append(
            (
                text,
                ner_deployment_id,
                llm_deployment_id,
                organization_profile,
                source_type,
                tuple(regex_candidates),
            )
        )
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(slots=True)
class RuntimeLifecycle:
    """가짜 Runtime의 진입과 종료 횟수를 기록합니다."""

    entered: int = 0
    exited: int = 0
    runtime: ApplicationRuntime | None = None


def _runtime_factory(
    pipeline: RecordingDetectionPipeline,
) -> tuple[object, RuntimeLifecycle]:
    """테스트 Detection Pipeline을 제공하는 Runtime factory를 만듭니다."""

    lifecycle = RuntimeLifecycle()

    @asynccontextmanager
    async def factory() -> AsyncIterator[ApplicationRuntime]:
        """TestClient 수명 동안 가짜 Runtime을 제공합니다."""

        lifecycle.entered += 1
        runtime = ApplicationRuntime(
            http_client=cast(httpx.AsyncClient, object()),
            registry_manager=cast(RegistryManager, object()),
            backend_providers=cast(BackendProviderRegistry, object()),
            adapter_catalog_service=cast(
                AdapterCatalogService,
                object(),
            ),
            detection_pipeline=cast(DetectionPipeline, pipeline),
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
        lifecycle.runtime = runtime
        try:
            yield runtime
        finally:
            lifecycle.exited += 1

    return factory, lifecycle


def _post_detect(
    pipeline: RecordingDetectionPipeline,
    payload: object,
) -> tuple[httpx.Response, RuntimeLifecycle]:
    """주입한 Pipeline을 사용하는 애플리케이션에 탐지 요청을 보냅니다."""

    runtime_factory, lifecycle = _runtime_factory(pipeline)
    application = create_app(runtime_factory=runtime_factory)

    with TestClient(application) as client:
        response = client.post("/detect", json=payload)

    return response, lifecycle


def _regex_candidate_data() -> dict[str, object]:
    """요청 원문의 `1234` 구간에 맞는 미확정 Regex 후보를 만듭니다."""

    return {
        "candidateId": "R001",
        "start": 5,
        "end": 9,
        "text": "1234",
        "policyId": "P03",
        "detailType": "PHONE_NUMBER",
        "score": 1.0,
    }


def _organization_profile_data() -> dict[str, object]:
    """전체 기본 활성 정책을 충족하는 조직 프로필 요청 데이터를 만듭니다."""

    return {
        "organization": {
            "name": "ABC 주식회사",
            "aliases": ["ABC"],
            "type": "PRIVATE",
        },
        "publicContext": {
            "domains": ["abc.com"],
            "entities": [],
        },
        "privacy": {"personNameScope": True},
        "securityContext": {
            "internalIpRanges": [],
            "internalDomains": [],
            "internalSystems": [],
            "cloudAssets": [],
            "securityAssets": [],
            "protectTestLogs": True,
        },
        "confidentialTechnologyContext": {"assets": []},
        "thirdPartyContext": {"entities": []},
    }


def _valid_detect_payload(
    text: object,
    *,
    regex_candidates: object | None = None,
) -> dict[str, object]:
    """필수 Deployment ID가 포함된 탐지 요청 본문을 만듭니다."""

    payload: dict[str, object] = {
        "text": text,
        "nerDeploymentId": "ner-a",
        "llmDeploymentId": "llm-a",
        "organizationProfile": _organization_profile_data(),
        "sourceType": "CHAT_TEXT",
    }
    if regex_candidates is not None:
        payload["regexCandidates"] = regex_candidates
    return payload


def _assert_error_response(
    response: httpx.Response,
    *,
    status_code: int,
    code: str,
) -> None:
    """오류 응답이 고정된 공개 형식만 포함하는지 확인합니다."""

    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"detail"}
    assert set(body["detail"]) == {"code", "message"}
    assert body["detail"]["code"] == code
    assert isinstance(body["detail"]["message"], str)
    assert body["detail"]["message"]


@pytest.mark.parametrize(
    (
        "payload",
        "expected_ner_deployment_id",
        "expected_llm_deployment_id",
        "expected_existing_count",
    ),
    [
        (
            _valid_detect_payload(
                "Call 1234",
                regex_candidates=[_regex_candidate_data()],
            ),
            "ner-a",
            "llm-a",
            1,
        ),
    ],
)
def test_detect_forwards_request_to_pipeline(
    payload: dict[str, object],
    expected_ner_deployment_id: str,
    expected_llm_deployment_id: str,
    expected_existing_count: int,
) -> None:
    """원문, Deployment 선택과 기존 Regex 결과를 Pipeline에 전달합니다."""

    pipeline = RecordingDetectionPipeline()

    response, lifecycle = _post_detect(pipeline, payload)

    assert response.status_code == 200
    assert len(pipeline.calls) == 1
    (
        text,
        ner_deployment_id,
        llm_deployment_id,
        organization_profile,
        source_type,
        regex_candidates,
    ) = pipeline.calls[0]
    assert text == payload["text"]
    assert ner_deployment_id == expected_ner_deployment_id
    assert llm_deployment_id == expected_llm_deployment_id
    assert organization_profile is not None
    assert organization_profile.organization.name == "ABC 주식회사"
    assert source_type == "CHAT_TEXT"
    assert isinstance(regex_candidates, tuple)
    assert len(regex_candidates) == expected_existing_count
    if regex_candidates:
        assert regex_candidates[0].candidate_id == "R001"
        assert regex_candidates[0].detail_type == "PHONE_NUMBER"
        assert regex_candidates[0].text == "1234"
    assert lifecycle.entered == 1
    assert lifecycle.exited == 1


def test_detect_accepts_omitted_organization_profile() -> None:
    """Gateway가 조직 정보를 보내지 않아도 Pipeline에 None으로 전달합니다."""

    payload = _valid_detect_payload("조직 정보 없는 요청")
    payload.pop("organizationProfile")
    pipeline = RecordingDetectionPipeline()

    response, _ = _post_detect(pipeline, payload)

    assert response.status_code == 200
    assert pipeline.calls[0][3] is None


def test_detect_serializes_detection_response_as_json_array() -> None:
    """불변 Detection tuple을 공통 필드의 JSON 배열로 반환합니다."""

    pipeline = RecordingDetectionPipeline(
        result=DetectResponse(
            candidateDecisions=(
                RegexCandidateDecision(
                    candidateId="R001",
                    source="regex",
                    start=0,
                    end=5,
                    text="Alice",
                    policyId="P01",
                    detailType="PERSON_NAME",
                    score=1.0,
                    decision="CONFIRMED",
                ),
            ),
            detections=(
                Detection(
                    start=0,
                    end=5,
                    text="Alice",
                    type="PERSONAL_IDENTITY",
                    policyId="P01",
                    source="ner",
                    score=0.95,
                ),
                Detection(
                    start=13,
                    end=17,
                    text="ACME",
                    type="PERSONAL",
                    policyId="B01",
                    source="llm",
                    score=0.8,
                ),
            )
        )
    )

    response, _ = _post_detect(
        pipeline,
        _valid_detect_payload("Alice joined ACME"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "candidateDecisions": [
            {
                "candidateId": "R001",
                "source": "regex",
                "start": 0,
                "end": 5,
                "text": "Alice",
                "policyId": "P01",
                "detailType": "PERSON_NAME",
                "score": 1.0,
                "decision": "CONFIRMED",
            }
        ],
        "detections": [
            {
                "start": 0,
                "end": 5,
                "text": "Alice",
                "type": "PERSONAL_IDENTITY",
                "policyId": "P01",
                "source": "ner",
                "score": 0.95,
            },
            {
                "start": 13,
                "end": 17,
                "text": "ACME",
                "type": "PERSONAL",
                "policyId": "B01",
                "source": "llm",
                "score": 0.8,
            },
        ]
    }


def test_detect_serializes_empty_detection_response() -> None:
    """탐지 결과가 없을 때도 detections 배열을 생략하지 않습니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(),
        _valid_detect_payload("민감정보가 없는 문장"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "candidateDecisions": [],
        "detections": [],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nerDeploymentId": "ner-a", "llmDeploymentId": "llm-a"},
        {"text": "Deployment ID 누락"},
        {"text": "NER ID 누락", "llmDeploymentId": "llm-a"},
        {"text": "LLM ID 누락", "nerDeploymentId": "ner-a"},
        {
            "text": "null NER ID",
            "nerDeploymentId": None,
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "null LLM ID",
            "nerDeploymentId": "ner-a",
            "llmDeploymentId": None,
        },
        _valid_detect_payload(""),
        _valid_detect_payload(1),
        {
            **_valid_detect_payload("잘못된 출처"),
            "sourceType": "FILE",
        },
        {
            **_valid_detect_payload("abc"),
            "regexCandidates": {},
        },
        {
            **_valid_detect_payload("Call 1234"),
            "regexCandidates": [
                {**_regex_candidate_data(), "unknownField": True}
            ],
        },
        {
            **_valid_detect_payload("Call 1234"),
            "regexCandidates": [
                {**_regex_candidate_data(), "source": "ner"}
            ],
        },
        {
            **_valid_detect_payload("Call 1234"),
            "regexCandidates": [
                {**_regex_candidate_data(), "type": "UNKNOWN"}
            ],
        },
        {
            **_valid_detect_payload("Call 1234"),
            "regexCandidates": [
                {**_regex_candidate_data(), "text": "9999"}
            ],
        },
        {
            **_valid_detect_payload("Call 1234"),
            "regexCandidates": [
                {**_regex_candidate_data(), "end": 100}
            ],
        },
        {
            **_valid_detect_payload("질문"),
            "nerDeploymentId": "NER A",
        },
        {
            **_valid_detect_payload("질문"),
            "llmDeploymentId": "LLM A",
        },
        {
            **_valid_detect_payload("질문"),
            "unknownField": True,
        },
        {
            **_valid_detect_payload("질문"),
            "profileId": "profile-a",
        },
    ],
)
def test_detect_rejects_invalid_request_before_pipeline_call(
    payload: object,
) -> None:
    """잘못된 요청을 422로 거부하고 Pipeline을 실행하지 않습니다."""

    pipeline = RecordingDetectionPipeline()

    response, _ = _post_detect(pipeline, payload)

    _assert_error_response(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert pipeline.calls == []


def test_detect_request_validation_does_not_echo_invalid_input() -> None:
    """요청 검증 오류 응답에 사용자가 보낸 민감 원문을 복사하지 않습니다."""

    request_secret = "REQUEST-DETECTION-SECRET-4a2f"

    response, _ = _post_detect(
        RecordingDetectionPipeline(),
        _valid_detect_payload([request_secret]),
    )

    _assert_error_response(
        response,
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
    )
    assert request_secret not in response.text


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            DeploymentResolutionError(
                "DEPLOYMENT_NOT_FOUND",
                deployment_id="missing-ner",
                expected_kind="ner",
            ),
            404,
            "DEPLOYMENT_NOT_FOUND",
        ),
        (
            DeploymentResolutionError(
                "DEPLOYMENT_DISABLED",
                deployment_id="ner-disabled",
                expected_kind="ner",
            ),
            409,
            "DEPLOYMENT_DISABLED",
        ),
        (
            DeploymentResolutionError(
                "DEPLOYMENT_KIND_MISMATCH",
                deployment_id="llm-a",
                expected_kind="ner",
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
        (
            BackendProviderLookupError(
                "BACKEND_PROVIDER_KIND_MISMATCH",
                adapter_type="mock",
                kind="llm",
                deployment_id="llm-a",
                available_kinds=("ner",),
            ),
            503,
            "BACKEND_PROVIDER_KIND_MISMATCH",
        ),
    ],
)
def test_detect_maps_deployment_registry_and_provider_errors(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """Deployment 선택과 실행 준비 오류를 안정적인 HTTP 오류로 변환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(error=error),
        _valid_detect_payload("오류 매핑 테스트"),
    )

    _assert_error_response(
        response,
        status_code=expected_status,
        code=expected_code,
    )


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
            BackendConfigurationError(
                "TEST_BACKEND_CONFIGURATION_INVALID",
                "Deployment 설정 오류",
            ),
            500,
            "TEST_BACKEND_CONFIGURATION_INVALID",
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
                "모델 서버 응답 오류",
            ),
            502,
            "TEST_BACKEND_RESPONSE_INVALID",
        ),
    ],
)
def test_detect_maps_common_backend_errors(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """모델 서버 오류를 원인별 Gateway HTTP 상태로 변환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(error=error),
        _valid_detect_payload("Backend 오류 매핑 테스트"),
    )

    _assert_error_response(
        response,
        status_code=expected_status,
        code=expected_code,
    )


@pytest.mark.parametrize(
    "error",
    [
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_JSON"
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL"
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_ITEM",
            item_index=0,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_DECISION",
            item_index=0,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH",
            item_index=0,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_POLICY_DISABLED",
            item_index=0,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_TOO_LARGE",
            actual_bytes=2,
            max_output_bytes=1,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS",
            actual_items=2,
            max_detections=1,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_TOO_MANY_DECISIONS",
            actual_items=2,
            max_detections=1,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND",
            item_index=0,
        ),
        LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS",
            item_index=0,
        ),
    ],
)
def test_detect_maps_llm_detection_output_errors(
    error: LlmDetectionOutputError,
) -> None:
    """비신뢰 LLM 탐지 출력 오류의 code를 보존하여 502로 반환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(error=error),
        _valid_detect_payload("LLM 출력 오류 테스트"),
    )

    _assert_error_response(
        response,
        status_code=502,
        code=error.code,
    )


def test_detect_maps_invalid_detection_result_to_bad_gateway() -> None:
    """Backend의 잘못된 Span 결과를 공통 탐지 결과 오류로 축약합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(
            error=DetectionSpanValidationError(
                "TEXT_MISMATCH",
                item_index=0,
            )
        ),
        _valid_detect_payload("Span 오류 테스트"),
    )

    _assert_error_response(
        response,
        status_code=502,
        code="DETECTION_RESULT_INVALID",
    )


def test_detect_maps_disabled_regex_policy_to_validation_error() -> None:
    """현재 비활성 정책을 참조하는 Regex 후보를 명시적인 422로 반환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(
            error=DetectionPolicySelectionError(item_index=0),
        ),
        _valid_detect_payload(
            "Call 1234",
            regex_candidates=[_regex_candidate_data()],
        ),
    )

    _assert_error_response(
        response,
        status_code=422,
        code="DETECTION_POLICY_DISABLED",
    )


def test_detect_maps_incomplete_organization_profile_to_validation_error() -> None:
    """활성 정책의 조직 기준정보 누락을 원문 없는 422 오류로 변환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(
            error=OrganizationProfilePolicyError(
                missing_sections=("securityContext",),
            ),
        ),
        _valid_detect_payload("조직 프로필 오류 테스트"),
    )

    _assert_error_response(
        response,
        status_code=422,
        code="ORGANIZATION_PROFILE_INCOMPLETE",
    )
    assert "securityContext" not in response.text


def test_detect_maps_invalid_backend_result_to_internal_error() -> None:
    """LLM Backend 공통 결과 계약 위반을 내부 구현 오류로 반환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(
            error=DetectionBackendResultError(
                stage="confidential_detection",
                deployment_id="llm-a",
                actual_type=dict,
            )
        ),
        _valid_detect_payload("Backend 계약 오류 테스트"),
    )

    _assert_error_response(
        response,
        status_code=500,
        code="DETECTION_BACKEND_RESULT_INVALID",
    )


def test_detect_maps_prompt_render_error_to_internal_error() -> None:
    """고정 탐지 Prompt 렌더링 실패를 내부 실행 오류로 반환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(
            error=PromptRenderError("렌더링 상세 오류")
        ),
        _valid_detect_payload("Prompt 오류 테스트"),
    )

    _assert_error_response(
        response,
        status_code=500,
        code="DETECTION_PROMPT_RENDER_FAILED",
    )


@pytest.mark.parametrize(
    "error",
    [
        PromptContextTooLargeError(2, 1),
        PromptOutputTooLargeError(2, 1),
    ],
)
def test_detect_maps_prompt_size_errors_to_payload_too_large(
    error: PromptRenderError,
) -> None:
    """요청 데이터로 Prompt 제한을 넘으면 내부 오류가 아닌 413을 반환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(error=error),
        _valid_detect_payload("Prompt 크기 오류 테스트"),
    )

    _assert_error_response(
        response,
        status_code=413,
        code="DETECTION_REQUEST_TOO_LARGE",
    )


def test_detect_maps_backend_input_limit_to_payload_too_large() -> None:
    """NER 서버가 원문 전체를 처리할 수 없으면 LPL도 413을 반환합니다."""

    response, _ = _post_detect(
        RecordingDetectionPipeline(
            error=BackendInputTooLargeError(
                "NER_INPUT_TOO_LONG",
                "외부에 노출하지 않을 내부 상세",
            )
        ),
        _valid_detect_payload("NER 장문 입력 테스트"),
    )

    _assert_error_response(
        response,
        status_code=413,
        code="NER_INPUT_TOO_LONG",
    )


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            BackendResponseError(
                "TEST_BACKEND_RESPONSE_INVALID",
                "Provider 응답: PROVIDER-DETECTION-SECRET-2d95a1",
            ),
            502,
            "TEST_BACKEND_RESPONSE_INVALID",
        ),
        (
            PromptRenderError(
                "Prompt 상세: PROMPT-DETECTION-SECRET-7c31"
            ),
            500,
            "DETECTION_PROMPT_RENDER_FAILED",
        ),
    ],
)
def test_detect_does_not_expose_request_or_internal_error_details(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    """정규화한 오류 응답에 원문과 내부 예외 상세를 노출하지 않습니다."""

    request_secret = "request-detection-secret-7f4d2c"
    internal_secret = str(error)
    pipeline = RecordingDetectionPipeline(error=error)

    response, _ = _post_detect(
        pipeline,
        _valid_detect_payload(request_secret),
    )

    _assert_error_response(
        response,
        status_code=expected_status,
        code=expected_code,
    )
    assert request_secret not in response.text
    assert internal_secret not in response.text


def test_detect_returns_503_when_lifespan_runtime_is_unavailable() -> None:
    """Lifespan 밖의 탐지 요청에는 안전한 Runtime 오류를 반환합니다."""

    runtime_factory, lifecycle = _runtime_factory(
        RecordingDetectionPipeline()
    )
    application = create_app(runtime_factory=runtime_factory)
    client = TestClient(application)
    try:
        response = client.post(
            "/detect",
            json=_valid_detect_payload("Runtime 없음 테스트"),
        )
    finally:
        client.close()

    assert lifecycle.entered == 0
    _assert_error_response(
        response,
        status_code=503,
        code="APPLICATION_RUNTIME_UNAVAILABLE",
    )
