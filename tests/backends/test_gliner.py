"""GLiNER HTTP NER Adapter의 요청·응답 계약을 검증합니다."""

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
from app.backends.ner.gliner import (
    DEFAULT_MAX_GLINER_NER_RESPONSE_BYTES,
    GlinerNerBackend,
    GlinerNerConfigurationError,
    GlinerNerHttpError,
    GlinerNerInputTooLongError,
    GlinerNerRequestError,
    GlinerNerResponseError,
    GlinerNerResponseTooLargeError,
    GlinerNerTimeoutError,
)
from app.schemas.detection import Detection
from app.schemas.registry import DeploymentConfig


def _deployment(**overrides: object) -> DeploymentConfig:
    """GLiNER Backend 테스트용 Deployment를 생성합니다."""

    data: dict[str, object] = {
        "kind": "ner",
        "adapterType": "gliner_http",
        "baseUrl": "http://localhost:8008/ner",
        "timeoutMs": 5000,
        "enabled": True,
    }
    data.update(overrides)
    return DeploymentConfig.model_validate(data)


def _entity_payload(
    *,
    start: object = 0,
    end: object = 3,
    text: object = "홍길동",
    label: object = "PERSONAL_IDENTITY",
    score: object = 0.98,
) -> dict[str, object]:
    """정규화할 수 있는 GLiNER entity 항목을 만듭니다."""

    return {
        "start": start,
        "end": end,
        "text": text,
        "label": label,
        "score": score,
    }


@pytest.mark.asyncio
async def test_detect_posts_configured_request_and_normalizes_result() -> None:
    """내부 고정 labels·threshold를 보내고 결과 출처를 NER로 고정합니다."""

    text = "홍길동은 서울에 있습니다."

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://localhost:8008/ner"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "text": text,
            "labels": ["사람", "회사", "조직", "주소", "장소"],
            "threshold": 0.4,
        }
        return httpx.Response(
            200,
            json={
                "text": text,
                "entities": [
                    _entity_payload(),
                    _entity_payload(
                        start=5,
                        end=7,
                        text="서울",
                        label="LOCATION",
                        score=0.91,
                    ),
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await GlinerNerBackend(client).detect(
            text,
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
async def test_detect_accepts_empty_entity_array() -> None:
    """서버가 개체를 찾지 못하면 빈 공통 목록을 반환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"text": "민감정보가 없는 문장", "entities": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await GlinerNerBackend(client).detect(
            "민감정보가 없는 문장",
            _deployment(),
        )

    assert result == []


@pytest.mark.asyncio
async def test_detect_treats_base_url_as_complete_endpoint() -> None:
    """baseUrl의 인코딩된 경로를 보존하고 별도 경로를 붙이지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://localhost:8008/private%2Fmodels/gliner"
        )
        return httpx.Response(
            200,
            json={"text": "원문", "entities": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await GlinerNerBackend(client).detect(
            "원문",
            _deployment(
                baseUrl=(
                    "http://localhost:8008/"
                    "private%2Fmodels/gliner"
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
        return httpx.Response(
            200,
            json={"text": "원문", "entities": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await GlinerNerBackend(client).detect(
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
        return httpx.Response(
            200,
            json={"text": "원문", "entities": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerConfigurationError) as error_info:
            await GlinerNerBackend(client).detect(  # type: ignore[arg-type]
                invalid_text,
                _deployment(),
            )

    assert error_info.value.code == "GLINER_NER_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_detect_rejects_non_utf8_text_without_retaining_input() -> None:
    """직렬화할 수 없는 원문과 내부 예외를 외부 오류에 보존하지 않습니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(
            200,
            json={"text": "원문", "entities": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerConfigurationError) as error_info:
            await GlinerNerBackend(client).detect(
                "\ud800",
                _deployment(),
            )

    assert error_info.value.code == "GLINER_NER_CONFIG_INVALID"
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
        {"baseUrl": "http://localhost:8008/ner?token=value"},
        {"baseUrl": "http://localhost:8008/ner#fragment"},
        {"baseUrl": "http://user:password@localhost:8008/ner"},
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
    """실행 불가하거나 안전하지 않은 GLiNER 설정을 요청 전에 거부합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(
            200,
            json={"text": "원문", "entities": []},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerConfigurationError) as error_info:
            await GlinerNerBackend(client).detect(
                "원문",
                _deployment(**deployment_overrides),
            )

    assert error_info.value.code == "GLINER_NER_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_detect_converts_timeout_error_safely() -> None:
    """httpx timeout을 원인 상세를 숨긴 공통 Backend timeout으로 바꿉니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "private timeout detail",
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerTimeoutError) as error_info:
            await GlinerNerBackend(client).detect(
                "원문",
                _deployment(timeoutMs=3210),
            )

    assert isinstance(error_info.value, BackendTimeoutError)
    assert error_info.value.code == "GLINER_NER_TIMEOUT"
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
        with pytest.raises(GlinerNerRequestError) as error_info:
            await GlinerNerBackend(client).detect(
                "원문",
                _deployment(),
            )

    assert isinstance(error_info.value, BackendTransportError)
    assert error_info.value.code == "GLINER_NER_REQUEST_FAILED"
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
            json={"detail": "private server response"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerHttpError) as error_info:
            await GlinerNerBackend(client).detect(
                "원문",
                _deployment(),
            )

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "GLINER_NER_HTTP_ERROR"
    assert error_info.value.status_code == 503
    assert "private server response" not in str(error_info.value)


@pytest.mark.asyncio
async def test_detect_preserves_ner_server_input_too_long_semantics() -> None:
    """전용 서버의 엄격한 413 계약은 공통 장문 입력 오류로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            413,
            json={
                "code": "NER_INPUT_TOO_LONG",
                "detail": "내부 상세는 외부로 전달하지 않습니다",
                "maxTokens": 384,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerInputTooLongError) as error_info:
            await GlinerNerBackend(client).detect("원문", _deployment())

    assert isinstance(error_info.value, BackendInputTooLargeError)
    assert error_info.value.code == "GLINER_NER_INPUT_TOO_LONG"
    assert "내부 상세" not in str(error_info.value)


@pytest.mark.asyncio
async def test_detect_treats_unrecognized_413_as_generic_http_error() -> None:
    """임의 서버의 413 본문을 신뢰해 LPL 오류 의미를 바꾸지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            413,
            json={"code": "UNTRUSTED", "detail": "private"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerHttpError) as error_info:
            await GlinerNerBackend(client).detect("원문", _deployment())

    assert error_info.value.status_code == 413
    assert error_info.value.code == "GLINER_NER_HTTP_ERROR"


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
        with pytest.raises(GlinerNerHttpError) as error_info:
            await GlinerNerBackend(client).detect(
                "외부로 전송하면 안 되는 원문",
                _deployment(),
            )

    assert error_info.value.status_code == 307
    assert requested_urls == ["http://localhost:8008/ner"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    [
        b"not-json-private-output",
        b"\xff",
        b'{"text":"abc","text":"private","entities":[]}',
        b'{"text":"abc","entities":[],"entities":[]}',
        (
            b'{"text":"abc","entities":[{"start":0,"end":3,'
            b'"text":"abc","label":"PERSONAL_IDENTITY","label":"private",'
            b'"score":0.9}]}'
        ),
        (
            b'{"text":"abc","entities":[{"start":0,"end":3,'
            b'"text":"abc","label":"PERSONAL_IDENTITY","score":NaN}]}'
        ),
    ],
    ids=[
        "malformed",
        "invalid-utf8",
        "duplicate-top-level-text",
        "duplicate-top-level-entities",
        "duplicate-item-key",
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
        with pytest.raises(GlinerNerResponseError) as error_info:
            await GlinerNerBackend(client).detect(
                "abc",
                _deployment(),
            )

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "GLINER_NER_RESPONSE_INVALID"
    assert "not-json-private-output" not in str(error_info.value)
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"text": "abc"},
        {"entities": []},
        {"text": None, "entities": []},
        {"text": 123, "entities": []},
        {"text": "abc", "entities": None},
        {"text": "abc", "entities": {}},
        {"text": "abc", "entities": "not-an-array"},
        {"text": "abc", "entities": [], "metadata": {}},
        {"text": "different", "entities": []},
    ],
    ids=[
        "null-root",
        "array-root",
        "empty-object",
        "missing-entities",
        "missing-text",
        "null-text",
        "non-string-text",
        "null-entities",
        "object-entities",
        "string-entities",
        "extra-field",
        "different-text",
    ],
)
async def test_detect_rejects_invalid_top_level_response(
    payload: object,
) -> None:
    """응답은 입력과 같은 text 및 entities만 가진 객체여야 합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerResponseError) as error_info:
            await GlinerNerBackend(client).detect(
                "abc",
                _deployment(),
            )

    assert error_info.value.code == "GLINER_NER_RESPONSE_INVALID"


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
            "text": "abc",
            "label": "PERSONAL_IDENTITY",
        },
        {
            **_entity_payload(text="abc"),
            "source": "ner",
        },
        {
            **_entity_payload(text="abc"),
            "metadata": {},
        },
        _entity_payload(text="abc", start=True),
        _entity_payload(text="abc", start=-1),
        _entity_payload(text="abc", end=0),
        _entity_payload(text="abc", end=4),
        _entity_payload(text=""),
        _entity_payload(text="abd"),
        _entity_payload(text=123),
        _entity_payload(text="abc", label=""),
        _entity_payload(text="abc", label=123),
        _entity_payload(text="abc", score=True),
        _entity_payload(text="abc", score="0.98"),
        _entity_payload(text="abc", score=-0.1),
        _entity_payload(text="abc", score=1.1),
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
        "end-outside-input",
        "empty-text",
        "text-span-mismatch",
        "non-string-text",
        "empty-label",
        "non-string-label",
        "boolean-score",
        "string-score",
        "negative-score",
        "score-over-one",
    ],
)
async def test_detect_rejects_invalid_entity_item(item: object) -> None:
    """entity의 정확한 필드 집합 및 원문 기반 span·text를 강제합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=json.dumps(
                {"text": "abc", "entities": [item]},
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerResponseError) as error_info:
            await GlinerNerBackend(client).detect(
                "abc",
                _deployment(),
            )

    assert error_info.value.code == "GLINER_NER_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("text", "\ud800"),
        ("label", "\ud800"),
    ],
)
async def test_detect_rejects_non_utf8_entity_strings(
    field_name: str,
    invalid_value: str,
) -> None:
    """entity 문자열이 유효한 UTF-8이 아니면 안전한 응답 오류로 바꿉니다."""

    item = _entity_payload(text="abc")
    item[field_name] = invalid_value
    response_body = json.dumps(
        {"text": "abc", "entities": [item]},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

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
        with pytest.raises(GlinerNerResponseError) as error_info:
            await GlinerNerBackend(client).detect(
                "abc",
                _deployment(),
            )

    assert error_info.value.code == "GLINER_NER_RESPONSE_INVALID"
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_detect_does_not_expose_mismatched_private_text() -> None:
    """원문 불일치 응답을 거부하면서 두 비신뢰 문자열을 숨깁니다."""

    private_input = "private-person-name"
    private_response_text = "private-server-output"

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "text": private_response_text,
                "entities": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GlinerNerResponseError) as error_info:
            await GlinerNerBackend(client).detect(
                private_input,
                _deployment(),
            )

    error_text = str(error_info.value)
    assert private_input not in error_text
    assert private_response_text not in error_text


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
            GlinerNerBackend(
                client,
                max_response_bytes=max_response_bytes,  # type: ignore[arg-type]
            )


def test_backend_rejects_non_async_http_client() -> None:
    """Backend가 호출 계약과 다른 HTTP client 객체를 거부합니다."""

    with pytest.raises(TypeError):
        GlinerNerBackend(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_detect_rejects_decoded_response_over_size_limit() -> None:
    """압축 응답도 디코딩된 byte 크기를 기준으로 제한합니다."""

    text = "x" * 1024
    raw_response = json.dumps(
        {
            "text": text,
            "entities": [
                _entity_payload(
                    text=text,
                    end=len(text),
                )
            ],
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
        backend = GlinerNerBackend(
            client,
            max_response_bytes=256,
        )
        with pytest.raises(
            GlinerNerResponseTooLargeError
        ) as error_info:
            await backend.detect(
                text,
                _deployment(),
            )

    assert isinstance(error_info.value, BackendResponseError)
    assert error_info.value.code == "GLINER_NER_RESPONSE_TOO_LARGE"
    assert error_info.value.max_response_bytes == 256


@pytest.mark.asyncio
async def test_backend_exposes_default_response_size_limit() -> None:
    """운영 기본 응답 byte 제한을 읽기 전용 속성으로 제공합니다."""

    async with httpx.AsyncClient() as client:
        backend = GlinerNerBackend(client)

        assert (
            backend.max_response_bytes
            == DEFAULT_MAX_GLINER_NER_RESPONSE_BYTES
        )


def test_gliner_ner_errors_use_common_backend_categories() -> None:
    """전용 오류를 API가 처리하는 공통 Backend 오류 계층에 연결합니다."""

    assert isinstance(
        GlinerNerConfigurationError("잘못된 설정"),
        BackendConfigurationError,
    )
    assert isinstance(
        GlinerNerRequestError(),
        BackendTransportError,
    )
    assert isinstance(
        GlinerNerTimeoutError(1000),
        BackendTimeoutError,
    )
    assert isinstance(
        GlinerNerHttpError(500),
        BackendResponseError,
    )
    assert isinstance(
        GlinerNerResponseError("잘못된 응답"),
        BackendResponseError,
    )
    assert isinstance(
        GlinerNerResponseTooLargeError(1024),
        BackendResponseError,
    )


def test_default_backend_registry_does_not_register_gliner_adapter() -> None:
    """Legacy GLiNER 구현체를 운영 기본 선택지로 노출하지 않습니다."""

    registry = create_default_backend_registry()

    with pytest.raises(BackendValidationError) as error_info:
        registry.require(
            deployment_id="ner-gliner-a",
            adapter_type="gliner_http",
            kind="ner",
        )

    assert error_info.value.code == "ADAPTER_NOT_REGISTERED"
