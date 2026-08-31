import json

import pytest
from pydantic import ValidationError

from app.schemas import GenerateRequest, LlmResult, LlmTokenUsage


def _valid_usage_data() -> dict[str, object]:
    """유효한 토큰 사용량의 JSON 필드 입력을 생성합니다."""

    return {
        "inputTokens": 12,
        "outputTokens": 8,
        "totalTokens": 20,
    }


def _valid_result_data() -> dict[str, object]:
    """유효한 LLM 생성 결과의 JSON 필드 입력을 생성합니다."""

    return {
        "text": "생성된 답변",
        "modelName": "qwen3:8b",
        "finishReason": "stop",
        "usage": _valid_usage_data(),
    }


def test_llm_token_usage_accepts_aliases_and_serializes_json() -> None:
    """토큰 사용량이 camelCase 별칭을 읽고 같은 JSON 필드로 직렬화됩니다."""

    usage = LlmTokenUsage.model_validate(_valid_usage_data())

    assert usage.input_tokens == 12
    assert usage.output_tokens == 8
    assert usage.total_tokens == 20
    assert usage.model_dump(by_alias=True, mode="json") == (
        _valid_usage_data()
    )


def test_llm_result_accepts_aliases_and_serializes_nested_json() -> None:
    """생성 결과와 중첩 사용량이 API용 camelCase JSON으로 직렬화됩니다."""

    result = LlmResult.model_validate(_valid_result_data())

    assert result.text == "생성된 답변"
    assert result.model_name == "qwen3:8b"
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.total_tokens == 20
    assert result.model_dump(by_alias=True, mode="json") == (
        _valid_result_data()
    )


def test_llm_result_accepts_and_emits_alias_json() -> None:
    """HTTP JSON에서도 camelCase 별칭을 읽고 같은 필드 이름으로 반환합니다."""

    expected = _valid_result_data()

    result = LlmResult.model_validate_json(
        json.dumps(expected, ensure_ascii=False)
    )
    serialized = json.loads(
        result.model_dump_json(by_alias=True)
    )

    assert serialized == expected


def test_generation_models_accept_python_field_names() -> None:
    """내부 Python 코드에서는 snake_case 필드 이름으로도 생성할 수 있습니다."""

    result = LlmResult(
        text="답변",
        model_name="model-a",
        finish_reason="length",
        usage=LlmTokenUsage(
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
        ),
    )

    assert result.model_dump(by_alias=True, mode="json") == {
        "text": "답변",
        "modelName": "model-a",
        "finishReason": "length",
        "usage": {
            "inputTokens": 1,
            "outputTokens": 2,
            "totalTokens": 3,
        },
    }


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("inputTokens", -1),
        ("outputTokens", -1),
        ("totalTokens", -1),
        ("inputTokens", True),
        ("outputTokens", 1.0),
        ("totalTokens", "20"),
    ],
)
def test_llm_token_usage_is_strict_and_non_negative(
    field_name: str,
    invalid_value: object,
) -> None:
    """토큰 수에 음수와 암묵적으로 변환 가능한 다른 타입을 허용하지 않습니다."""

    data = _valid_usage_data()
    data[field_name] = invalid_value

    with pytest.raises(ValidationError):
        LlmTokenUsage.model_validate(data)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("text", 123),
        ("modelName", ""),
        ("modelName", 123),
        ("finishReason", ""),
        ("finishReason", 123),
        ("usage", {"inputTokens": "1", "outputTokens": 2, "totalTokens": 3}),
    ],
)
def test_llm_result_is_strict(
    field_name: str,
    invalid_value: object,
) -> None:
    """생성 결과 필드와 중첩 모델이 암묵적 타입 변환을 허용하지 않습니다."""

    data = _valid_result_data()
    data[field_name] = invalid_value

    with pytest.raises(ValidationError):
        LlmResult.model_validate(data)


@pytest.mark.parametrize(
    ("model_type", "valid_data"),
    [
        (LlmTokenUsage, _valid_usage_data()),
        (LlmResult, _valid_result_data()),
    ],
)
def test_generation_models_forbid_extra_fields(
    model_type: type[LlmTokenUsage] | type[LlmResult],
    valid_data: dict[str, object],
) -> None:
    """Provider 결과의 알 수 없는 필드를 조용히 무시하지 않습니다."""

    data = dict(valid_data)
    data["unknownField"] = True

    with pytest.raises(ValidationError):
        model_type.model_validate(data)


def test_llm_token_usage_is_frozen() -> None:
    """검증한 토큰 사용량을 생성 후 변경하지 못하게 합니다."""

    usage = LlmTokenUsage.model_validate(_valid_usage_data())

    with pytest.raises(ValidationError):
        usage.total_tokens = 99


def test_llm_result_is_frozen() -> None:
    """검증한 생성 결과를 생성 후 변경하지 못하게 합니다."""

    result = LlmResult.model_validate(_valid_result_data())

    with pytest.raises(ValidationError):
        result.text = "변경"


def test_llm_result_allows_empty_text_and_optional_metadata() -> None:
    """빈 생성 결과와 Provider가 주지 않은 선택 메타데이터를 표현할 수 있습니다."""

    result = LlmResult(text="")

    assert result.model_dump(
        by_alias=True,
        mode="json",
        exclude_none=True,
    ) == {"text": ""}


def test_generate_request_accepts_llm_deployment_id_alias() -> None:
    """생성 요청이 현재·이전 입력과 LLM Deployment ID 별칭을 검증합니다."""

    request = GenerateRequest.model_validate(
        {
            "text": "사용자 질문",
            "previousText": [
                {"role": "user", "content": "첫 번째 사용자 입력"},
                {"role": "assistant", "content": "첫 번째 모델 답변"},
            ],
            "llmDeploymentId": "llm-a",
        }
    )

    assert request.text == "사용자 질문"
    assert isinstance(request.previous_text, tuple)
    assert len(request.previous_text) == 2
    assert request.previous_text[0].role == "user"
    assert request.previous_text[0].content == "첫 번째 사용자 입력"
    assert request.previous_text[1].role == "assistant"
    assert request.previous_text[1].content == "첫 번째 모델 답변"
    assert request.llm_deployment_id == "llm-a"
    assert request.model_dump(by_alias=True, mode="json") == {
        "text": "사용자 질문",
        "previousText": [
            {"role": "user", "content": "첫 번째 사용자 입력"},
            {"role": "assistant", "content": "첫 번째 모델 답변"},
        ],
        "llmDeploymentId": "llm-a",
    }


def test_generate_request_allows_omitted_or_empty_previous_text() -> None:
    """이전 응답이 없으면 previousText를 생략하거나 빈 배열로 보냅니다."""

    omitted = GenerateRequest.model_validate(
        {"text": "첫 질문", "llmDeploymentId": "llm-a"}
    )
    explicit_empty = GenerateRequest.model_validate(
        {
            "text": "첫 질문",
            "previousText": [],
            "llmDeploymentId": "llm-a",
        }
    )

    assert omitted.previous_text == ()
    assert explicit_empty.previous_text == ()


@pytest.mark.parametrize(
    "invalid_data",
    [
        {"text": "LLM Deployment ID 누락 질문"},
        {
            "text": "null LLM Deployment ID 질문",
            "llmDeploymentId": None,
        },
    ],
)
def test_generate_request_requires_non_null_llm_deployment_id(
    invalid_data: dict[str, object],
) -> None:
    """생성 요청은 null이 아닌 LLM Deployment ID를 반드시 포함해야 합니다."""

    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(invalid_data)


def test_generate_request_schema_marks_llm_deployment_id_as_required() -> None:
    """OpenAPI에서 LLM ID는 필수, previousText는 선택으로 공개합니다."""

    schema = GenerateRequest.model_json_schema(by_alias=True)

    assert "llmDeploymentId" in schema["required"]
    assert "previousText" not in schema["required"]
    assert schema["properties"]["llmDeploymentId"]["type"] == "string"
    assert schema["properties"]["previousText"]["type"] == "array"
    assert schema["properties"]["previousText"]["items"]["$ref"].endswith(
        "/PreviousTextMessage"
    )


@pytest.mark.parametrize(
    "invalid_data",
    [
        {},
        {"llmDeploymentId": "llm-a"},
        {"text": "", "llmDeploymentId": "llm-a"},
        {"text": 1, "llmDeploymentId": "llm-a"},
        {
            "text": "질문",
            "previousText": None,
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "질문",
            "previousText": "이전 답변",
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "질문",
            "previousText": ["이전 답변"],
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "질문",
            "previousText": [{"role": "system", "content": "금지"}],
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "질문",
            "previousText": [{"role": "user", "content": ""}],
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "질문",
            "previousText": [
                {"role": "assistant", "content": "정상", "extra": True}
            ],
            "llmDeploymentId": "llm-a",
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
            "llmDeploymentId": "llm-a",
            "profileId": "profile-a",
        },
    ],
)
def test_generate_request_rejects_invalid_contract(
    invalid_data: dict[str, object],
) -> None:
    """누락·빈 문자열·잘못된 타입과 알 수 없는 필드를 거부합니다."""

    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(invalid_data)
