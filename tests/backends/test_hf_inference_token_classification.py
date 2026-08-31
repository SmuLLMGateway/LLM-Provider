"""Hugging Face Inference Token Classification NER 계약을 검증합니다."""

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
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
)
from app.backends.ner.hf_inference_token_classification import (
    DEFAULT_MAX_HF_INFERENCE_NER_RESPONSE_BYTES,
    HfInferenceNerConfigurationError,
    HfInferenceNerHttpError,
    HfInferenceNerRequestError,
    HfInferenceNerResponseError,
    HfInferenceNerResponseTooLargeError,
    HfInferenceNerTimeoutError,
    HfInferenceTokenClassificationNerBackend,
)
from app.schemas.detection import Detection
from app.schemas.registry import DeploymentConfig


def _deployment(**overrides: object) -> DeploymentConfig:
    """Hugging Face NER Backend 테스트용 Deployment를 생성합니다."""

    data: dict[str, object] = {
        "kind": "ner",
        "adapterType": "hf_inference_token_classification",
        "baseUrl": "http://localhost:9000/models/korean-ner",
        "timeoutMs": 5000,
        "enabled": True,
    }
    data.update(overrides)
    return DeploymentConfig.model_validate(data)


def _token_payload(
    *,
    label_field: str = "entity_group",
    label: object = "PERSONAL_IDENTITY",
    score: object = 0.98,
    word: object = "홍길동",
    start: object = 0,
    end: object = 3,
    index: object | None = None,
) -> dict[str, object]:
    """정규화할 수 있는 Hugging Face token classification 항목을 만듭니다."""

    item: dict[str, object] = {
        label_field: label,
        "score": score,
        "word": word,
        "start": start,
        "end": end,
    }
    if index is not None:
        item["index"] = index
    return item


@pytest.mark.asyncio
async def test_detect_posts_inputs_to_full_endpoint_and_normalizes_labels() -> None:
    """HF 요청을 보내고 두 label 형식을 원문 기반 Detection으로 변환합니다."""

    text = "홍길동은 서울에 있습니다."

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == (
            "http://localhost:9000/models/korean-ner"
        )
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"inputs": text}
        return httpx.Response(
            200,
            json=[
                _token_payload(
                    word="홍 ##길 ##동",
                ),
                _token_payload(
                    label_field="entity",
                    label="LOCATION",
                    score=0.91,
                    word="▁전혀-신뢰하지-않는-서울-토큰",
                    start=5,
                    end=7,
                    index=1,
                ),
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HfInferenceTokenClassificationNerBackend(
            client
        ).detect(text, _deployment())

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
async def test_detect_accepts_empty_token_array() -> None:
    """서버가 개체를 찾지 못하면 빈 공통 목록을 반환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await HfInferenceTokenClassificationNerBackend(
            client
        ).detect("민감정보가 없는 문장", _deployment())

    assert result == []


@pytest.mark.asyncio
async def test_detect_treats_base_url_as_complete_endpoint() -> None:
    """baseUrl의 인코딩된 경로를 보존하고 별도 경로를 붙이지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://localhost:9000/private%2Fmodels/korean-ner"
        )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await HfInferenceTokenClassificationNerBackend(client).detect(
            "원문",
            _deployment(
                baseUrl=(
                    "http://localhost:9000/"
                    "private%2Fmodels/korean-ner"
                )
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
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await HfInferenceTokenClassificationNerBackend(client).detect(
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
    """직접 호출에서도 문자열이 아닌 입력을 HTTP 요청 전에 거부합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(
            HfInferenceNerConfigurationError
        ) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect(
                invalid_text,  # type: ignore[arg-type]
                _deployment(),
            )

    assert error_info.value.code == "HF_INFERENCE_NER_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_detect_rejects_non_utf8_text_without_retaining_input() -> None:
    """직렬화할 수 없는 원문과 내부 예외를 외부 오류에 보존하지 않습니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(
            HfInferenceNerConfigurationError
        ) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect("\ud800", _deployment())

    assert error_info.value.code == "HF_INFERENCE_NER_CONFIG_INVALID"
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
        {"baseUrl": "http://localhost:9000/models/ner?token=value"},
        {"baseUrl": "http://localhost:9000/models/ner#fragment"},
        {
            "baseUrl": (
                "http://user:password@localhost:9000/models/ner"
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
    """실행 불가하거나 안전하지 않은 HF NER 설정을 요청 전에 거부합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(
            HfInferenceNerConfigurationError
        ) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect(
                "원문",
                _deployment(**deployment_overrides),
            )

    assert error_info.value.code == "HF_INFERENCE_NER_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_detect_converts_timeout_error_safely() -> None:
    """httpx timeout을 원인 상세를 숨긴 공통 Backend timeout으로 바꿉니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HfInferenceNerTimeoutError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect(
                "원문",
                _deployment(timeoutMs=3210),
            )

    assert isinstance(error_info.value, BackendTimeoutError)
    assert error_info.value.code == "HF_INFERENCE_NER_TIMEOUT"
    assert error_info.value.timeout_ms == 3210
    assert "private timeout detail" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_detect_converts_transport_error_safely() -> None:
    """timeout 이외의 전송 오류에서도 내부 네트워크 상세를 숨깁니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "private connection detail",
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HfInferenceNerRequestError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect("원문", _deployment())

    assert isinstance(error_info.value, BackendTransportError)
    assert error_info.value.code == "HF_INFERENCE_NER_REQUEST_FAILED"
    assert "private connection detail" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_detect_converts_unsuccessful_http_status_safely() -> None:
    """실패 상태만 노출하고 비신뢰 서버 응답 본문은 숨깁니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            503,
            json={"error": "private server response"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HfInferenceNerHttpError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect("원문", _deployment())

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "HF_INFERENCE_NER_HTTP_ERROR"
    assert error_info.value.status_code == 503
    assert "private server response" not in str(error_info.value)


@pytest.mark.asyncio
async def test_detect_never_follows_redirects_with_sensitive_text() -> None:
    """Client 설정과 무관하게 원문을 Redirect 대상에 재전송하지 않습니다."""

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
        with pytest.raises(HfInferenceNerHttpError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect(
                "외부로 전송하면 안 되는 원문",
                _deployment(),
            )

    assert error_info.value.status_code == 307
    assert requested_urls == [
        "http://localhost:9000/models/korean-ner"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    [
        b"not-json-private-output",
        b"\xff",
        (
            b'[{"entity_group":"PER","entity_group":"ORG",'
            b'"score":0.9,"word":"abc","start":0,"end":3}]'
        ),
        (
            b'[{"entity_group":"PER","score":0.9,"word":"abc",'
            b'"start":0,"start":1,"end":3}]'
        ),
        (
            b'[{"entity_group":"PER","score":NaN,"word":"abc",'
            b'"start":0,"end":3}]'
        ),
    ],
    ids=[
        "malformed",
        "invalid-utf8",
        "duplicate-label-key",
        "duplicate-offset-key",
        "non-standard-number",
    ],
)
async def test_detect_rejects_non_strict_json_response(
    response_body: bytes,
) -> None:
    """문법·UTF-8·중복 키·비표준 숫자 오류를 모두 거부합니다."""

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
        with pytest.raises(HfInferenceNerResponseError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect("abc", _deployment())

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "HF_INFERENCE_NER_RESPONSE_INVALID"
    assert "not-json-private-output" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"error": "model is loading"},
        "not-an-array",
        123,
    ],
    ids=[
        "null",
        "object",
        "hf-inference-error-object",
        "string",
        "number",
    ],
)
async def test_detect_rejects_non_array_response(payload: object) -> None:
    """HF 성공 응답은 token classification 배열이어야 합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HfInferenceNerResponseError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect("원문", _deployment())

    assert error_info.value.code == "HF_INFERENCE_NER_RESPONSE_INVALID"
    assert "model is loading" not in str(error_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        None,
        [],
        {},
        {
            "score": 0.98,
            "word": "홍길동",
            "start": 0,
            "end": 3,
        },
        {
            "entity_group": "PER",
            "score": 0.98,
            "word": "홍길동",
            "end": 3,
        },
        {
            "entity_group": "PER",
            "score": 0.98,
            "word": "홍길동",
            "start": 0,
        },
        {
            **_token_payload(),
            "entity": "B-PER",
        },
        _token_payload(index=0),
        {
            **_token_payload(),
            "metadata": {},
        },
        {
            **_token_payload(),
            "source": "llm",
        },
        _token_payload(start=True),
        _token_payload(start=-1),
        _token_payload(end=0),
        _token_payload(end=100),
        _token_payload(label=""),
        _token_payload(label="\ud800"),
        _token_payload(word=""),
        _token_payload(word="\ud800"),
        _token_payload(word=123),
        _token_payload(score="0.98"),
        _token_payload(score=-0.1),
        _token_payload(score=1.1),
        _token_payload(
            label_field="entity",
            label="B-PER",
            index=True,
        ),
        _token_payload(
            label_field="entity",
            label="B-PER",
            index=-1,
        ),
        _token_payload(
            label_field="entity",
            label="B-PER",
            index="1",
        ),
    ],
    ids=[
        "null-item",
        "array-item",
        "empty-item",
        "missing-label",
        "missing-start",
        "missing-end",
        "both-label-fields",
        "grouped-item-with-index",
        "unknown-extra-field",
        "provider-source",
        "boolean-start",
        "negative-start",
        "end-before-start",
        "end-outside-input",
        "empty-label",
        "invalid-utf8-label",
        "empty-word",
        "invalid-utf8-word",
        "non-string-word",
        "string-score",
        "negative-score",
        "score-over-one",
        "boolean-index",
        "negative-index",
        "string-index",
    ],
)
async def test_detect_rejects_invalid_token_item(item: object) -> None:
    """정확한 필드 집합과 엄격한 label·span·score·index 형식을 강제합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=json.dumps(
                [item],
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HfInferenceNerResponseError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect("홍길동", _deployment())

    assert error_info.value.code == "HF_INFERENCE_NER_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_detect_does_not_expose_invalid_span_input_in_error() -> None:
    """잘못된 span을 거부하면서 사용자 원문과 서버 word를 숨깁니다."""

    private_text = "private-person-name"
    private_word = "private-token-output"

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json=[
                _token_payload(
                    word=private_word,
                    start=0,
                    end=len(private_text) + 1,
                )
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HfInferenceNerResponseError) as error_info:
            await HfInferenceTokenClassificationNerBackend(
                client
            ).detect(private_text, _deployment())

    error_text = str(error_info.value)
    assert private_text not in error_text
    assert private_word not in error_text


@pytest.mark.parametrize(
    "max_response_bytes",
    [True, 0, -1, 1.5, "1024"],
)
@pytest.mark.asyncio
async def test_backend_rejects_invalid_response_size_limit(
    max_response_bytes: object,
) -> None:
    """응답 제한은 boolean이 아닌 1 이상의 정수만 허용합니다."""

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            HfInferenceTokenClassificationNerBackend(
                client,
                max_response_bytes=max_response_bytes,  # type: ignore[arg-type]
            )


def test_backend_rejects_non_async_http_client() -> None:
    """Backend가 호출 계약과 다른 HTTP client 객체를 거부합니다."""

    with pytest.raises(TypeError):
        HfInferenceTokenClassificationNerBackend(  # type: ignore[arg-type]
            object()
        )


@pytest.mark.asyncio
async def test_detect_rejects_decoded_response_over_size_limit() -> None:
    """압축 응답도 디코딩된 byte 크기를 기준으로 제한합니다."""

    text = "x" * 1024
    raw_response = json.dumps(
        [
            _token_payload(
                word="x" * 1024,
                end=len(text),
            )
        ],
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
        backend = HfInferenceTokenClassificationNerBackend(
            client,
            max_response_bytes=256,
        )
        with pytest.raises(
            HfInferenceNerResponseTooLargeError
        ) as error_info:
            await backend.detect(text, _deployment())

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == (
        "HF_INFERENCE_NER_RESPONSE_TOO_LARGE"
    )
    assert error_info.value.max_response_bytes == 256


@pytest.mark.asyncio
async def test_backend_exposes_default_response_size_limit() -> None:
    """운영 기본 응답 byte 제한을 읽기 전용 속성으로 제공합니다."""

    async with httpx.AsyncClient() as client:
        backend = HfInferenceTokenClassificationNerBackend(client)

        assert (
            backend.max_response_bytes
            == DEFAULT_MAX_HF_INFERENCE_NER_RESPONSE_BYTES
        )


def test_hf_inference_ner_errors_use_common_backend_categories() -> None:
    """전용 오류를 API가 처리하는 공통 Backend 오류 계층에 연결합니다."""

    assert isinstance(
        HfInferenceNerConfigurationError("잘못된 설정"),
        BackendConfigurationError,
    )
    assert isinstance(
        HfInferenceNerRequestError(),
        BackendTransportError,
    )
    assert isinstance(
        HfInferenceNerTimeoutError(1000),
        BackendTimeoutError,
    )
    assert isinstance(
        HfInferenceNerHttpError(500),
        BackendResponseError,
    )
    assert isinstance(
        HfInferenceNerResponseError("잘못된 응답"),
        BackendResponseError,
    )
    assert isinstance(
        HfInferenceNerResponseTooLargeError(1024),
        BackendResponseError,
    )


def test_default_backend_registry_does_not_register_hf_adapter() -> None:
    """Legacy HF 구현체를 운영 기본 선택지로 노출하지 않습니다."""

    registry = create_default_backend_registry()

    with pytest.raises(BackendValidationError) as error_info:
        registry.require(
            deployment_id="ner-hf-inference-a",
            adapter_type="hf_inference_token_classification",
            kind="ner",
        )

    assert error_info.value.code == "ADAPTER_NOT_REGISTERED"
