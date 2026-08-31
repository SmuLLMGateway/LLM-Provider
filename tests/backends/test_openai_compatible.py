"""OpenAI-compatible LLM Backend의 HTTP 변환과 오류 처리를 검증합니다."""

from __future__ import annotations

from copy import deepcopy
import gzip
import json

import httpx
import pytest

from app.backends import (
    DEFAULT_MAX_RESPONSE_BYTES,
    BackendProviderRegistration,
    BackendProviderRegistry,
    OpenAICompatibleConfigurationError,
    OpenAICompatibleHttpError,
    OpenAICompatibleLlmBackend,
    OpenAICompatibleRequestError,
    OpenAICompatibleResponseError,
    OpenAICompatibleResponseTooLargeError,
    OpenAICompatibleTimeoutError,
)
from app.schemas import DeploymentConfig, LlmResult, LlmTokenUsage


def _deployment(
    **overrides: object,
) -> DeploymentConfig:
    """OpenAI 호환 Backend 테스트용 Deployment를 생성합니다."""

    data: dict[str, object] = {
        "kind": "llm",
        "adapterType": "openai_compatible",
        "baseUrl": "http://localhost:8000/v1",
        "modelName": "qwen-test",
        "timeoutMs": 5000,
        "enabled": True,
    }
    data.update(overrides)
    return DeploymentConfig.model_validate(data)


def _success_payload(
    *,
    content: str = "반갑습니다",
) -> dict[str, object]:
    """정규화할 수 있는 Chat Completions 응답을 생성합니다."""

    return {
        "model": "qwen-server",
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
    }


@pytest.mark.asyncio
async def test_generate_sends_chat_completion_and_normalizes_result() -> None:
    """공통 입력을 Chat Completions 요청으로 보내고 결과를 정규화합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == (
            "http://localhost:8000/v1/chat/completions"
        )
        assert json.loads(request.content) == {
            "model": "qwen-test",
            "messages": [
                {"role": "system", "content": "간결하게 답하세요."},
                {"role": "user", "content": "안녕하세요"},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 64,
        }
        return httpx.Response(200, json=_success_payload())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        backend = OpenAICompatibleLlmBackend(client)
        result = await backend.generate(
            [
                {"role": "system", "content": "간결하게 답하세요."},
                {"role": "user", "content": "안녕하세요"},
            ],
            _deployment(),
            {"temperature": 0, "max_tokens": 64},
        )

    assert result == LlmResult(
        text="반갑습니다",
        model_name="qwen-server",
        finish_reason="stop",
        usage=LlmTokenUsage(
            input_tokens=3,
            output_tokens=2,
            total_tokens=5,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000/v1/",
        "http://localhost:8000/v1/chat/completions",
        "http://localhost:8000/v1/chat/completions/",
    ],
)
async def test_generate_normalizes_trailing_slash_and_applies_timeout(
    base_url: str,
) -> None:
    """기본 URL의 끝 슬래시를 정리하고 millisecond timeout을 적용합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://localhost:8000/v1/chat/completions"
        )
        timeout = request.extensions["timeout"]
        assert timeout == {
            "connect": 2.75,
            "read": 2.75,
            "write": 2.75,
            "pool": 2.75,
        }
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        await backend.generate(
            [{"role": "user", "content": "질문"}],
            _deployment(baseUrl=base_url, timeoutMs=2750),
            {},
        )


@pytest.mark.asyncio
async def test_generate_preserves_percent_encoded_base_path() -> None:
    """URL을 조립할 때 reverse proxy의 인코딩된 경로를 변경하지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://localhost:8000/private%2Fmodels/v1/chat/completions"
        )
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        await backend.generate(
            [],
            _deployment(
                baseUrl=(
                    "http://localhost:8000/private%2Fmodels/v1"
                )
            ),
            {},
        )


@pytest.mark.asyncio
async def test_generate_maps_output_schema_without_mutating_inputs() -> None:
    """출력 Schema를 response_format으로 옮기고 모든 입력을 보존합니다."""

    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": "JSON으로 답하세요",
            "metadata": {"tags": ["private"]},
        }
    ]
    parameters: dict[str, object] = {
        "temperature": 0,
        "stop": ["END"],
    }
    output_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
        },
        "required": ["answer"],
    }
    original_messages = deepcopy(messages)
    original_parameters = deepcopy(parameters)
    original_output_schema = deepcopy(output_schema)

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "lpl_response",
                "strict": True,
                "schema": original_output_schema,
            },
        }
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        await backend.generate(
            messages,
            _deployment(),
            parameters,
            output_schema,
        )

    assert messages == original_messages
    assert parameters == original_parameters
    assert output_schema == original_output_schema


@pytest.mark.asyncio
async def test_generate_omits_response_format_without_output_schema() -> None:
    """출력 Schema가 없으면 response_format 필드를 전송하지 않습니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "response_format" not in json.loads(request.content)
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        await backend.generate([], _deployment(), {}, None)


@pytest.mark.asyncio
async def test_generate_allows_missing_optional_response_metadata() -> None:
    """선택 응답 메타데이터가 없으면 모델명 fallback과 None을 사용합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "응답"},
                        "finish_reason": None,
                    }
                ],
                "usage": None,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        result = await backend.generate([], _deployment(), {})

    assert result == LlmResult(
        text="응답",
        model_name="qwen-test",
        finish_reason=None,
        usage=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reserved_name",
    ["model", "messages", "stream", "n", "response_format"],
)
async def test_generate_rejects_reserved_parameter_keys(
    reserved_name: str,
) -> None:
    """Adapter가 소유한 요청 필드를 parameters로 덮어쓰지 못하게 합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleConfigurationError
        ) as error_info:
            await backend.generate(
                [],
                _deployment(),
                {reserved_name: "invalid"},
            )

    assert error_info.value.code == "OPENAI_COMPATIBLE_CONFIG_INVALID"
    assert reserved_name in error_info.value.detail
    assert called is False


@pytest.mark.asyncio
async def test_generate_rejects_non_string_parameter_key() -> None:
    """HTTP JSON 객체의 parameters 키로 문자열만 허용합니다."""

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request)
        )
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(OpenAICompatibleConfigurationError):
            await backend.generate(
                [],
                _deployment(),
                {1: "invalid"},  # type: ignore[dict-item]
            )


@pytest.mark.asyncio
async def test_generate_rejects_non_serializable_input_without_cause() -> None:
    """직렬화 오류가 입력 객체를 원인 예외로 보존하지 않게 합니다."""

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request)
        )
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleConfigurationError
        ) as error_info:
            await backend.generate(
                [],
                _deployment(),
                {"unsupported": object()},
            )

    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deployment_overrides",
    [
        {"kind": "ner"},
        {"adapterType": "ollama_native"},
        {"enabled": False},
        {"baseUrl": None},
        {"modelName": None},
        {"timeoutMs": None},
        {"baseUrl": "http://localhost:8000/v1?token=value"},
        {"baseUrl": "http://localhost:8000/v1#fragment"},
        {"baseUrl": "http://user:password@localhost:8000/v1"},
    ],
)
async def test_generate_rejects_invalid_deployment(
    deployment_overrides: dict[str, object],
) -> None:
    """직접 호출에서도 실행할 수 없는 Deployment를 요청 전에 거부합니다."""

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleConfigurationError
        ) as error_info:
            await backend.generate(
                [],
                _deployment(**deployment_overrides),
                {},
            )

    assert error_info.value.code == "OPENAI_COMPATIBLE_CONFIG_INVALID"
    assert called is False


@pytest.mark.asyncio
async def test_generate_converts_timeout_error() -> None:
    """httpx timeout을 설정값이 포함된 Backend timeout 오류로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleTimeoutError
        ) as error_info:
            await backend.generate(
                [],
                _deployment(timeoutMs=3210),
                {},
            )

    assert error_info.value.code == "OPENAI_COMPATIBLE_TIMEOUT"
    assert error_info.value.timeout_ms == 3210
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_generate_converts_request_error() -> None:
    """timeout 이외의 httpx 전송 실패를 Backend 요청 오류로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleRequestError
        ) as error_info:
            await backend.generate([], _deployment(), {})

    assert error_info.value.code == "OPENAI_COMPATIBLE_REQUEST_FAILED"
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
async def test_generate_converts_unsuccessful_http_status() -> None:
    """실패 HTTP 상태를 응답 본문을 노출하지 않는 Backend 오류로 변환합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            503,
            json={"error": {"message": "secret server detail"}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(OpenAICompatibleHttpError) as error_info:
            await backend.generate([], _deployment(), {})

    assert error_info.value.code == "OPENAI_COMPATIBLE_HTTP_ERROR"
    assert error_info.value.status_code == 503
    assert "secret server detail" not in str(error_info.value)


@pytest.mark.asyncio
async def test_generate_rejects_non_json_response() -> None:
    """성공 상태라도 JSON이 아닌 응답은 형식 오류로 처리합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=b"not-json",
            headers={"content-type": "text/plain"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleResponseError
        ) as error_info:
            await backend.generate([], _deployment(), {})

    assert error_info.value.code == "OPENAI_COMPATIBLE_RESPONSE_INVALID"
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_body",
    [
        (
            b'{"choices":[{"message":{"content":"first",'
            b'"content":"second"},"finish_reason":"stop"}]}'
        ),
        (
            b'{"choices":[{"message":{"content":"ok"},'
            b'"finish_reason":"stop"}],"unexpected":NaN}'
        ),
    ],
    ids=["duplicate-key", "nan"],
)
async def test_generate_rejects_non_strict_json_response(
    response_body: bytes,
) -> None:
    """중복 키와 비표준 숫자가 있는 Provider JSON 응답을 거부합니다."""

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
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleResponseError
        ) as error_info:
            await backend.generate([], _deployment(), {})

    assert error_info.value.code == "OPENAI_COMPATIBLE_RESPONSE_INVALID"
    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


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
            OpenAICompatibleLlmBackend(
                client,
                max_response_bytes=max_response_bytes,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_generate_rejects_decoded_response_over_size_limit() -> None:
    """압축된 응답도 디코딩된 byte 크기를 기준으로 제한합니다."""

    raw_response = json.dumps(
        _success_payload(content="x" * DEFAULT_MAX_RESPONSE_BYTES),
        ensure_ascii=False,
    ).encode("utf-8")
    compressed_response = gzip.compress(raw_response)
    assert len(compressed_response) < DEFAULT_MAX_RESPONSE_BYTES

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
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(
            OpenAICompatibleResponseTooLargeError
        ) as error_info:
            await backend.generate([], _deployment(), {})

    assert (
        error_info.value.code
        == "OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE"
    )
    assert (
        error_info.value.max_response_bytes
        == DEFAULT_MAX_RESPONSE_BYTES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"choices": None},
        {"choices": {}},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": []}]},
    ],
)
async def test_generate_rejects_invalid_choices_structure(
    payload: object,
) -> None:
    """choices와 첫 message가 Chat Completions 구조가 아니면 거부합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(OpenAICompatibleResponseError):
            await backend.generate([], _deployment(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [None, 123, True, [], {}],
)
async def test_generate_rejects_non_string_content(content: object) -> None:
    """첫 번째 assistant message의 content는 문자열이어야 합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(OpenAICompatibleResponseError):
            await backend.generate([], _deployment(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        [],
        {},
        {
            "prompt_tokens": 1,
            "completion_tokens": 2,
        },
        {
            "prompt_tokens": -1,
            "completion_tokens": 2,
            "total_tokens": 1,
        },
        {
            "prompt_tokens": "1",
            "completion_tokens": 2,
            "total_tokens": 3,
        },
        {
            "prompt_tokens": True,
            "completion_tokens": 2,
            "total_tokens": 3,
        },
    ],
)
async def test_generate_rejects_invalid_usage(usage: object) -> None:
    """누락되거나 잘못된 타입의 토큰 사용량을 공통 결과로 받지 않습니다."""

    payload = _success_payload()
    payload["usage"] = usage

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(OpenAICompatibleResponseError):
            await backend.generate([], _deployment(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("model", ""),
        ("model", 123),
        ("finish_reason", ""),
        ("finish_reason", 123),
    ],
)
async def test_generate_rejects_invalid_optional_metadata(
    field_name: str,
    invalid_value: object,
) -> None:
    """Provider가 보낸 선택 문자열 메타데이터도 엄격하게 검증합니다."""

    payload = _success_payload()
    if field_name == "finish_reason":
        choices = payload["choices"]
        assert isinstance(choices, list)
        choice = choices[0]
        assert isinstance(choice, dict)
        choice[field_name] = invalid_value
    else:
        payload[field_name] = invalid_value

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        with pytest.raises(OpenAICompatibleResponseError):
            await backend.generate([], _deployment(), {})


@pytest.mark.asyncio
async def test_backend_reuses_shared_client_for_multiple_requests() -> None:
    """같은 Backend가 주입받은 AsyncClient 연결 풀을 계속 재사용합니다."""

    requested_contents: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        messages = payload["messages"]
        assert isinstance(messages, list)
        message = messages[0]
        assert isinstance(message, dict)
        content = message["content"]
        assert isinstance(content, str)
        requested_contents.append(content)
        return httpx.Response(
            200,
            json=_success_payload(content=f"응답-{content}"),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleLlmBackend(client)
    try:
        first = await backend.generate(
            [{"role": "user", "content": "첫 번째"}],
            _deployment(),
            {},
        )
        assert client.is_closed is False
        second = await backend.generate(
            [{"role": "user", "content": "두 번째"}],
            _deployment(),
            {},
        )
        assert client.is_closed is False
    finally:
        await client.aclose()

    assert requested_contents == ["첫 번째", "두 번째"]
    assert first.text == "응답-첫 번째"
    assert second.text == "응답-두 번째"


@pytest.mark.asyncio
async def test_provider_registry_returns_runnable_openai_backend() -> None:
    """Provider Registry가 Deployment에 등록된 동일 Backend를 선택합니다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_success_payload())

    deployment = _deployment()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        providers = BackendProviderRegistry(
            [
                BackendProviderRegistration(
                    kind="llm",
                    adapter_type="openai_compatible",
                    provider=backend,
                )
            ]
        )

        selected = providers.require_llm(
            deployment_id="llm-openai-compatible-a",
            deployment=deployment,
        )
        result = await selected.generate([], deployment, {})

    assert selected is backend
    assert result.text == "반갑습니다"
