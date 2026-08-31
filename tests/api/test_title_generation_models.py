"""대화 제목 생성 요청과 응답 Pydantic 계약을 검증합니다."""

import pytest
from pydantic import ValidationError

from app.schemas.title_generation import (
    GenerateTitleRequest,
    GenerateTitleResponse,
)


def test_generate_title_request_accepts_camel_case_deployment_id() -> None:
    """API 별칭을 읽고 내부에서는 snake_case로 제공합니다."""

    request = GenerateTitleRequest.model_validate(
        {
            "text": "FastAPI 제목 기능을 만들어 주세요",
            "llmDeploymentId": "llm-title",
        }
    )

    assert request.text == "FastAPI 제목 기능을 만들어 주세요"
    assert request.llm_deployment_id == "llm-title"
    assert request.model_dump(by_alias=True, mode="json") == {
        "text": "FastAPI 제목 기능을 만들어 주세요",
        "llmDeploymentId": "llm-title",
    }


@pytest.mark.parametrize(
    "invalid_data",
    [
        {},
        {"text": "원문"},
        {"llmDeploymentId": "llm-title"},
        {"text": "", "llmDeploymentId": "llm-title"},
        {"text": 1, "llmDeploymentId": "llm-title"},
        {"text": "원문", "llmDeploymentId": None},
        {"text": "원문", "llmDeploymentId": "LLM TITLE"},
        {
            "text": "원문",
            "llmDeploymentId": "llm-title",
            "conversationId": "chat-a",
        },
    ],
)
def test_generate_title_request_rejects_invalid_contract(
    invalid_data: dict[str, object],
) -> None:
    """필드 누락, 잘못된 타입·ID와 추가 필드를 거부합니다."""

    with pytest.raises(ValidationError):
        GenerateTitleRequest.model_validate(invalid_data)


def test_generate_title_request_schema_marks_fields_required() -> None:
    """OpenAPI Schema에 원문과 LLM Deployment ID를 필수로 표시합니다."""

    schema = GenerateTitleRequest.model_json_schema(by_alias=True)

    assert set(schema["required"]) == {"text", "llmDeploymentId"}


def test_generate_title_response_serializes_title_only() -> None:
    """검증된 제목만 응답 JSON에 포함합니다."""

    response = GenerateTitleResponse(title="FastAPI 제목 생성")

    assert response.model_dump(by_alias=True, mode="json") == {
        "title": "FastAPI 제목 생성"
    }


@pytest.mark.parametrize(
    "invalid_data",
    [
        {},
        {"title": ""},
        {"title": 1},
        {"title": "가" * 31},
        {"title": "정상", "modelName": "내부 모델"},
    ],
)
def test_generate_title_response_rejects_invalid_contract(
    invalid_data: dict[str, object],
) -> None:
    """빈 값, 최대 길이 초과, 잘못된 타입과 내부 메타데이터를 거부합니다."""

    with pytest.raises(ValidationError):
        GenerateTitleResponse.model_validate(invalid_data)


def test_title_generation_models_are_frozen() -> None:
    """검증한 요청과 응답을 이후에 변경하지 못하게 합니다."""

    request = GenerateTitleRequest(
        text="원문",
        llm_deployment_id="llm-title",
    )
    response = GenerateTitleResponse(title="제목")

    with pytest.raises(ValidationError):
        request.text = "변경"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        response.title = "변경"  # type: ignore[misc]
