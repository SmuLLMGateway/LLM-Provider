"""공통 HTTP NER Backend의 요청 변환과 비신뢰 응답 검증을 확인합니다."""

from __future__ import annotations

import gzip
import json

import httpx
import pytest

from app.backends.backend_registry import (
    BackendValidationError,
    create_default_backend_registry,
)
from app.backends.errors import (
    BackendConfigurationError,
    BackendInputTooLargeError,
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
)
from app.backends.ner.http import (
    DEFAULT_MAX_NER_RESPONSE_BYTES,
    HttpNerBackend,
    HttpNerConfigurationError,
    HttpNerHttpError,
    HttpNerInputTooLongError,
    HttpNerRequestError,
    HttpNerResponseError,
    HttpNerResponseTooLargeError,
    HttpNerTimeoutError,
)
from app.schemas.detection import Detection
from app.schemas.registry import DeploymentConfig


def _deployment(**overrides: object) -> DeploymentConfig:
    """HTTP NER Backend 테스트용 Deployment 설정을 생성합니다."""

    data: dict[str, object] = {
        "kind": "ner",
        "adapterType": "http_ner",
        "baseUrl": "http://localhost:9000/v1/ner/detect",
        "timeoutMs": 5000,
        "enabled": True,
    }
    data.update(overrides)
    return DeploymentConfig.model_validate(data)


def _detection_payload(
    *,
    start: object = 0,
    end: object = 3,
    text: object = "홍길동",
    detection_type: object = "PERSONAL_IDENTITY",
    score: object = 0.98,
) -> dict[str, object]:
    """정규화할 수 있는 서버측 탐지 항목을 생성합니다."""

    return {
        "start": start,
        "end": end,
        "text": text,
        "type": detection_type,
        "score": score,
    }


@pytest.mark.asyncio
async def test_detect_posts_text_to_registered_endpoint_and_normalizes_result() -> None:
    """등록된 전체 Endpoint에 원문만 보내고 결과를 정규화합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://localhost:9000/v1/ner/detect"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "text": "홍길동은 서울에 있습니다."
        }
        return httpx.Response(
            200,
            json={
                "detections": [
                    _detection_payload(),
                    _detection_payload(
                        start=5,
                        end=7,
                        text="서울",
                        detection_type="LOCATION",
                        score=0.91,
                    ),
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HttpNerBackend(client).detect(
            "홍길동은 서울에 있습니다.",
            _deployment(),
        )

    assert result == [
        Detection(
            start=0,
            end=3,
            text="홍길동",
            type="PERSONAL_IDENTITY",
            policyId="P01",
            source="ner",
            score=0.98,
        ),
        Detection(
            start=5,
            end=7,
            text="서울",
            type="LOCATION",
            policyId="P04",
            source="ner",
            score=0.91,
        ),
    ]


@pytest.mark.asyncio
async def test_detect_accepts_empty_detection_array() -> None:
    """서버가 탐지하지 못했을 때 빈 공통 목록을 반환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"detections": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HttpNerBackend(client).detect(
            "민감정보가 없는 문장",
            _deployment(),
        )

    assert result == []


@pytest.mark.asyncio
async def test_detect_preserves_registered_endpoint_path() -> None:
    """등록된 Endpoint의 인코딩된 경로와 trailing slash를 보존합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://localhost:9000/custom%20ner/detect/"
        )
        return httpx.Response(200, json={"detections": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await HttpNerBackend(client).detect(
            "원문",
            _deployment(
                baseUrl="http://localhost:9000/custom%20ner/detect/"
            ),
        )


@pytest.mark.asyncio
async def test_detect_applies_deployment_timeout_in_seconds() -> None:
    """Deployment의 millisecond 제한시간을 httpx timeout으로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {
            "connect": 2.75,
            "read": 2.75,
            "write": 2.75,
            "pool": 2.75,
        }
        return httpx.Response(200, json={"detections": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await HttpNerBackend(client).detect(
            "원문",
            _deployment(timeoutMs=2750),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_text",
    [None, 123, True, ["원문"]],
)
async def test_detect_rejects_non_string_text(
    invalid_text: object,
) -> None:
    """직접 호출에서도 문자열이 아닌 원문을 HTTP 요청 전에 거부합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"detections": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerConfigurationError) as error_info:
            await HttpNerBackend(client).detect(  # type: ignore[arg-type]
                invalid_text,
                _deployment(),
            )

    assert error_info.value.code == "HTTP_NER_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_detect_rejects_non_utf8_text_without_retaining_input() -> None:
    """직렬화할 수 없는 원문을 요청하지 않고 원인 예외에도 보존하지 않습니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"detections": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerConfigurationError) as error_info:
            await HttpNerBackend(client).detect(
                "\ud800",
                _deployment(),
            )

    assert error_info.value.code == "HTTP_NER_CONFIG_INVALID"
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deployment_overrides",
    [
        {"kind": "llm"},
        {"adapterType": "other_ner"},
        {"enabled": False},
        {"baseUrl": None},
        {"timeoutMs": None},
        {"modelName": "unused-model"},
        {
            "baseUrl": (
                "http://localhost:9000/v1/ner/detect?token=value"
            )
        },
        {"baseUrl": "http://localhost:9000/v1/ner/detect#fragment"},
        {
            "baseUrl": (
                "http://user:password@localhost:9000/v1/ner/detect"
            )
        },
    ],
    ids=[
        "wrong-kind",
        "wrong-adapter",
        "disabled",
        "missing-base-url",
        "missing-timeout",
        "unused-model-name",
        "query",
        "fragment",
        "userinfo",
    ],
)
async def test_detect_rejects_invalid_deployment_before_request(
    deployment_overrides: dict[str, object],
) -> None:
    """실행할 수 없거나 모호한 HTTP NER 설정을 요청 전에 거부합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"detections": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerConfigurationError) as error_info:
            await HttpNerBackend(client).detect(
                "원문",
                _deployment(**deployment_overrides),
            )

    assert error_info.value.code == "HTTP_NER_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_detect_converts_timeout_error() -> None:
    """httpx timeout을 설정값을 포함한 공통 Backend timeout으로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerTimeoutError) as error_info:
            await HttpNerBackend(client).detect(
                "원문",
                _deployment(timeoutMs=3210),
            )

    assert isinstance(error_info.value, BackendTimeoutError)
    assert error_info.value.code == "HTTP_NER_TIMEOUT"
    assert error_info.value.timeout_ms == 3210
    assert "private timeout detail" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_detect_converts_transport_error() -> None:
    """timeout 이외의 httpx 전송 실패를 공통 Backend 전송 오류로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "private connection detail",
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerRequestError) as error_info:
            await HttpNerBackend(client).detect(
                "원문",
                _deployment(),
            )

    assert isinstance(error_info.value, BackendTransportError)
    assert error_info.value.code == "HTTP_NER_REQUEST_FAILED"
    assert "private connection detail" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_detect_converts_unsuccessful_http_status_safely() -> None:
    """실패 상태만 노출하고 비신뢰 응답 본문은 오류에 포함하지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            503,
            json={"detail": "private server response"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerHttpError) as error_info:
            await HttpNerBackend(client).detect(
                "원문",
                _deployment(),
            )

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "HTTP_NER_HTTP_ERROR"
    assert error_info.value.status_code == 503
    assert "private server response" not in str(error_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": "NER_INPUT_TOO_LONG",
            "detail": "NER 입력이 너무 깁니다",
        },
        {
            "code": "NER_INPUT_TOO_LONG",
            "detail": "NER 입력이 너무 깁니다",
            "maxTokens": 384,
        },
    ],
    ids=["without-limit", "with-limit"],
)
async def test_detect_converts_strict_input_too_long_response(
    payload: dict[str, object],
) -> None:
    """정확한 공통 413 계약만 입력 크기 오류로 승격합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(413, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerInputTooLongError) as error_info:
            await HttpNerBackend(client).detect("긴 원문", _deployment())

    assert isinstance(error_info.value, BackendInputTooLargeError)
    assert error_info.value.code == "NER_INPUT_TOO_LONG"
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    [
        b"not-json-private-response",
        b'{"code":"OTHER","detail":"private"}',
        b'{"code":"NER_INPUT_TOO_LONG","detail":123}',
        (
            b'{"code":"NER_INPUT_TOO_LONG","detail":"private",'
            b'"maxTokens":true}'
        ),
        (
            b'{"code":"NER_INPUT_TOO_LONG","detail":"private",'
            b'"unexpected":true}'
        ),
    ],
    ids=[
        "invalid-json",
        "wrong-code",
        "non-string-detail",
        "boolean-limit",
        "extra-field",
    ],
)
async def test_detect_treats_unrecognized_413_as_http_error(
    response_body: bytes,
) -> None:
    """임의 413 응답은 입력 크기 의미로 신뢰하지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(413, content=response_body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerHttpError) as error_info:
            await HttpNerBackend(client).detect("긴 원문", _deployment())

    assert error_info.value.status_code == 413
    assert "private" not in str(error_info.value)


@pytest.mark.asyncio
async def test_detect_never_follows_redirects_with_sensitive_text() -> None:
    """Client 기본값과 무관하게 원문을 Redirect 대상에 재전송하지 않습니다."""

    requested_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            307,
            headers={
                "LOCATION": "http://untrusted.example/collect",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        with pytest.raises(HttpNerHttpError) as error_info:
            await HttpNerBackend(client).detect(
                "외부로 전송하면 안 되는 원문",
                _deployment(),
            )

    assert error_info.value.status_code == 307
    assert requested_urls == [
        "http://localhost:9000/v1/ner/detect"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    [
        b"not-json-private-output",
        b"\xff",
        b'{"detections":[],"detections":[]}',
        (
            b'{"detections":[{"start":0,"end":3,"text":"abc",'
            b'"type":"PERSONAL_IDENTITY","type":"company","score":0.9}]}'
        ),
        (
            b'{"detections":[{"start":0,"end":3,"text":"abc",'
            b'"type":"PERSONAL_IDENTITY","score":NaN}]}'
        ),
    ],
    ids=[
        "malformed",
        "invalid-utf8",
        "duplicate-top-level-key",
        "duplicate-item-key",
        "non-standard-number",
    ],
)
async def test_detect_rejects_non_strict_json_response(
    response_body: bytes,
) -> None:
    """문법 오류, UTF-8 오류, 중복 키와 비표준 숫자를 모두 거부합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=response_body,
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerResponseError) as error_info:
            await HttpNerBackend(client).detect(
                "원문",
                _deployment(),
            )

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "HTTP_NER_RESPONSE_INVALID"
    assert "not-json-private-output" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"detections": None},
        {"detections": {}},
        {"detections": "not-an-array"},
        {"detections": [], "metadata": {}},
    ],
    ids=[
        "array-root",
        "missing-detections",
        "null-detections",
        "object-detections",
        "string-detections",
        "extra-top-level-field",
    ],
)
async def test_detect_rejects_invalid_top_level_response(
    payload: object,
) -> None:
    """최상위 응답은 detections 하나만 가진 객체여야 합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerResponseError) as error_info:
            await HttpNerBackend(client).detect(
                "원문",
                _deployment(),
            )

    assert error_info.value.code == "HTTP_NER_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        None,
        [],
        {},
        {
            "start": 0,
            "end": 3,
            "text": "홍길동",
            "type": "PERSONAL_IDENTITY",
        },
        {
            **_detection_payload(),
            "source": "llm",
        },
        {
            **_detection_payload(),
            "metadata": {},
        },
        _detection_payload(start=True),
        _detection_payload(start=-1),
        _detection_payload(end=0),
        _detection_payload(text=""),
        _detection_payload(detection_type=""),
        _detection_payload(detection_type="UNKNOWN"),
        _detection_payload(score="0.98"),
        _detection_payload(score=-0.1),
        _detection_payload(score=1.1),
    ],
    ids=[
        "null-item",
        "array-item",
        "empty-item",
        "missing-score",
        "provider-source",
        "extra-item-field",
        "boolean-start",
        "negative-start",
        "end-before-start",
        "empty-text",
        "empty-type",
        "unknown-type",
        "string-score",
        "negative-score",
        "score-over-one",
    ],
)
async def test_detect_rejects_invalid_detection_item(
    item: object,
) -> None:
    """탐지 항목의 정확한 필드 집합, 엄격한 타입과 범위를 검증합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"detections": [item]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HttpNerResponseError) as error_info:
            await HttpNerBackend(client).detect(
                "홍길동",
                _deployment(),
            )

    assert error_info.value.code == "HTTP_NER_RESPONSE_INVALID"


@pytest.mark.parametrize(
    "max_response_bytes",
    [True, 0, -1, 1.5, "1024"],
)
@pytest.mark.asyncio
async def test_backend_rejects_invalid_response_size_limit(
    max_response_bytes: object,
) -> None:
    """응답 크기 제한은 boolean이 아닌 1 이상의 정수만 허용합니다."""

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            HttpNerBackend(
                client,
                max_response_bytes=max_response_bytes,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_detect_rejects_decoded_response_over_size_limit() -> None:
    """압축 전송된 응답도 디코딩된 byte 크기를 기준으로 제한합니다."""

    raw_response = json.dumps(
        {
            "detections": [
                _detection_payload(
                    text="x" * 1024,
                    end=1024,
                )
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    compressed_response = gzip.compress(raw_response)
    assert len(compressed_response) < 256
    assert len(raw_response) > 256

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=compressed_response,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = HttpNerBackend(client, max_response_bytes=256)
        with pytest.raises(
            HttpNerResponseTooLargeError
        ) as error_info:
            await backend.detect(
                "x" * 1024,
                _deployment(),
            )

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "HTTP_NER_RESPONSE_TOO_LARGE"
    assert error_info.value.max_response_bytes == 256


@pytest.mark.asyncio
async def test_backend_exposes_default_response_size_limit() -> None:
    """운영 기본 응답 byte 제한을 읽기 전용 속성으로 제공합니다."""

    async with httpx.AsyncClient() as client:
        backend = HttpNerBackend(client)

        assert backend.max_response_bytes == DEFAULT_MAX_NER_RESPONSE_BYTES


def test_http_ner_errors_use_common_backend_categories() -> None:
    """HTTP NER 전용 오류를 API가 처리하는 공통 오류 계층에 연결합니다."""

    assert isinstance(
        HttpNerConfigurationError("잘못된 설정"),
        BackendConfigurationError,
    )
    assert isinstance(HttpNerRequestError(), BackendTransportError)
    assert isinstance(HttpNerTimeoutError(1000), BackendTimeoutError)
    assert isinstance(HttpNerHttpError(500), BackendResponseError)
    assert isinstance(
        HttpNerInputTooLongError(),
        BackendInputTooLargeError,
    )
    assert isinstance(
        HttpNerResponseError("잘못된 응답"),
        BackendResponseError,
    )
    assert isinstance(
        HttpNerResponseTooLargeError(1024),
        BackendResponseError,
    )


def test_default_backend_registry_defines_http_ner_contract() -> None:
    """기본 Registry에 원격 HTTP NER의 필수·금지 설정 계약을 등록합니다."""

    registry = create_default_backend_registry()
    registration = registry.require(
        deployment_id="ner-http-a",
        adapter_type="http_ner",
        kind="ner",
    )

    assert registration.required_fields == frozenset(
        {"base_url", "timeout_ms"}
    )
    assert registration.forbidden_fields == frozenset(
        {
            "model_name",
            "context_window_tokens",
        }
    )
    registry.validate_deployment("ner-http-a", _deployment())

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment(
            "ner-http-a",
            _deployment(modelName="unused-model"),
        )

    assert error_info.value.code == "ADAPTER_FIELD_NOT_ALLOWED"
    assert error_info.value.field_names == ("model_name",)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:9000/v1/ner/detect?token=value",
        "http://localhost:9000/v1/ner/detect#fragment",
        "http://user:password@localhost:9000/v1/ner/detect",
    ],
)
def test_default_backend_registry_rejects_unsafe_http_ner_url(
    base_url: str,
) -> None:
    """모호하거나 인증정보가 포함된 URL은 Snapshot 생성 전에 거부합니다."""

    registry = create_default_backend_registry()

    with pytest.raises(BackendValidationError) as error_info:
        registry.validate_deployment(
            "ner-http-a",
            _deployment(baseUrl=base_url),
        )

    assert error_info.value.code == "ADAPTER_CONFIG_INVALID"
