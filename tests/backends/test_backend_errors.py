"""Adapter에 독립적인 Backend 오류 계층과 기존 오류 호환성을 검증합니다."""

from __future__ import annotations

from typing import Any

import pytest

from app.backends import (
    BackendConfigurationError,
    BackendError,
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
    MockLlmConfigurationError,
    MockNerConfigurationError,
    OpenAICompatibleBackendError,
    OpenAICompatibleConfigurationError,
    OpenAICompatibleHttpError,
    OpenAICompatibleRequestError,
    OpenAICompatibleResponseError,
    OpenAICompatibleResponseTooLargeError,
    OpenAICompatibleTimeoutError,
)


def test_openai_compatible_base_error_preserves_public_contract() -> None:
    """기존 OpenAI 기본 오류의 생성자와 공개 속성을 그대로 유지합니다."""

    error = OpenAICompatibleBackendError(
        "OPENAI_COMPATIBLE_RESPONSE_INVALID",
        "응답 상세",
    )

    assert isinstance(error, BackendError)
    assert isinstance(error, RuntimeError)
    assert error.code == "OPENAI_COMPATIBLE_RESPONSE_INVALID"
    assert error.detail == "응답 상세"
    assert str(error) == (
        "OPENAI_COMPATIBLE_RESPONSE_INVALID: 응답 상세"
    )


@pytest.mark.parametrize(
    (
        "error",
        "common_category",
        "expected_code",
        "expected_detail",
        "attribute_name",
        "attribute_value",
    ),
    [
        (
            OpenAICompatibleConfigurationError("설정 상세"),
            BackendConfigurationError,
            "OPENAI_COMPATIBLE_CONFIG_INVALID",
            "설정 상세",
            None,
            None,
        ),
        (
            OpenAICompatibleRequestError(),
            BackendTransportError,
            "OPENAI_COMPATIBLE_REQUEST_FAILED",
            "모델 서버에 요청을 전송하지 못했습니다",
            None,
            None,
        ),
        (
            OpenAICompatibleTimeoutError(3210),
            BackendTimeoutError,
            "OPENAI_COMPATIBLE_TIMEOUT",
            "모델 서버 요청이 3210ms를 초과했습니다",
            "timeout_ms",
            3210,
        ),
        (
            OpenAICompatibleHttpError(503),
            BackendResponseError,
            "OPENAI_COMPATIBLE_HTTP_ERROR",
            "모델 서버가 HTTP 503 상태를 반환했습니다",
            "status_code",
            503,
        ),
        (
            OpenAICompatibleResponseError("응답 상세"),
            BackendResponseError,
            "OPENAI_COMPATIBLE_RESPONSE_INVALID",
            "응답 상세",
            None,
            None,
        ),
        (
            OpenAICompatibleResponseTooLargeError(1024),
            BackendResponseError,
            "OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE",
            "모델 서버 응답이 허용된 크기를 초과했습니다",
            "max_response_bytes",
            1024,
        ),
    ],
    ids=[
        "configuration",
        "transport",
        "timeout",
        "http-response",
        "invalid-response",
        "response-too-large",
    ],
)
def test_openai_compatible_errors_keep_legacy_and_common_contracts(
    error: OpenAICompatibleBackendError,
    common_category: type[BackendError],
    expected_code: str,
    expected_detail: str,
    attribute_name: str | None,
    attribute_value: object,
) -> None:
    """기존 오류 타입을 유지하면서 Adapter 공통 분류에도 포함합니다."""

    assert isinstance(error, common_category)
    assert isinstance(error, OpenAICompatibleBackendError)
    assert isinstance(error, BackendError)
    assert isinstance(error, RuntimeError)
    assert error.code == expected_code
    assert error.detail == expected_detail
    assert str(error) == f"{expected_code}: {expected_detail}"
    if attribute_name is not None:
        assert getattr(error, attribute_name) == attribute_value


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_detail"),
    [
        (
            MockLlmConfigurationError("LLM 설정 상세"),
            "MOCK_LLM_CONFIG_INVALID",
            "LLM 설정 상세",
        ),
        (
            MockNerConfigurationError("NER 설정 상세"),
            "MOCK_NER_CONFIG_INVALID",
            "NER 설정 상세",
        ),
    ],
    ids=["mock-llm", "mock-ner"],
)
def test_mock_configuration_errors_use_common_category(
    error: BackendConfigurationError,
    expected_code: str,
    expected_detail: str,
) -> None:
    """Mock Backend 설정 오류도 공통 HTTP 매핑 대상에 포함합니다."""

    assert isinstance(error, BackendConfigurationError)
    assert isinstance(error, BackendError)
    assert error.code == expected_code
    assert error.detail == expected_detail


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        (None, "상세"),
        ("", "상세"),
        (1, "상세"),
        (True, "상세"),
        ("BACKEND_TEST_ERROR", None),
        ("BACKEND_TEST_ERROR", ""),
        ("BACKEND_TEST_ERROR", 1),
        ("BACKEND_TEST_ERROR", False),
    ],
    ids=[
        "none-code",
        "empty-code",
        "integer-code",
        "boolean-code",
        "none-detail",
        "empty-detail",
        "integer-detail",
        "boolean-detail",
    ],
)
def test_backend_error_rejects_invalid_public_fields(
    code: Any,
    detail: Any,
) -> None:
    """오류 응답에 쓰이는 code와 내부 detail은 비어 있지 않은 문자열만 받습니다."""

    with pytest.raises(ValueError):
        BackendError(code, detail)
