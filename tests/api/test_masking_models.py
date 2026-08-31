"""마스킹 API 요청과 응답 Pydantic 계약을 검증합니다."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.detection import policy_id_for_detection_type
from app.schemas.masking import (
    MAX_MASK_DETECTIONS,
    MaskReplacement,
    MaskRequest,
    MaskResponse,
)


_TEXT = "홍길동은 ACME에 연락했습니다."


def _detection_data(
    needle: str = "홍길동",
    *,
    source: str = "ner",
    detection_type: str = "PERSONAL_IDENTITY",
) -> dict[str, object]:
    """원문의 실제 문자 좌표를 사용하는 Detection 데이터를 만듭니다."""

    start = _TEXT.index(needle)
    return {
        "start": start,
        "end": start + len(needle),
        "text": needle,
        "type": detection_type,
        "policyId": policy_id_for_detection_type(detection_type),
        "source": source,
        "score": 0.9,
    }


def _request_data(
    *,
    text: object = _TEXT,
    detections: object | None = None,
) -> dict[str, object]:
    """필수 필드를 모두 가진 MaskRequest 입력을 만듭니다."""

    return {
        "text": text,
        "llmDeploymentId": "llm-mask",
        "detections": (
            [_detection_data()] if detections is None else detections
        ),
    }


def _replacement_data(**overrides: object) -> dict[str, object]:
    """유효한 마스킹 replacement 데이터를 만듭니다."""

    data: dict[str, object] = {
        "start": 0,
        "end": 3,
        "entityId": "entity-1",
        "placeholder": "[[LPL_0123456789abcdef_0001]]",
        "types": ["PERSONAL_IDENTITY"],
        "sources": ["ner"],
    }
    data.update(overrides)
    return data


def test_mask_request_accepts_all_detection_sources_and_aliases() -> None:
    """Gateway가 병합한 Regex·NER·LLM Detection을 모두 받습니다."""

    text = "010-1234 홍길동 기밀"
    request = MaskRequest.model_validate(
        {
            "text": text,
            "llmDeploymentId": "llm-mask",
            "detections": [
                {
                    "start": 0,
                    "end": 8,
                    "text": "010-1234",
                    "type": "CONTACT",
                    "policyId": "P03",
                    "source": "regex",
                    "score": 1.0,
                },
                {
                    "start": 9,
                    "end": 12,
                    "text": "홍길동",
                    "type": "PERSONAL_IDENTITY",
                    "policyId": "P01",
                    "source": "ner",
                    "score": 0.9,
                },
                {
                    "start": 13,
                    "end": 15,
                    "text": "기밀",
                    "type": "PERSONAL",
                    "policyId": "B01",
                    "source": "llm",
                    "score": 0.5,
                },
            ],
        }
    )

    assert request.llm_deployment_id == "llm-mask"
    assert tuple(item.source for item in request.detections) == (
        "regex",
        "ner",
        "llm",
    )
    serialized = request.model_dump(mode="json")
    assert serialized["llmDeploymentId"] == "llm-mask"
    assert "llm_deployment_id" not in serialized
    assert isinstance(serialized["detections"], list)


def test_mask_request_requires_explicit_detection_array() -> None:
    """탐지 목록 누락과 명시적인 빈 목록을 구분합니다."""

    with pytest.raises(ValidationError):
        MaskRequest.model_validate(
            {"text": _TEXT, "llmDeploymentId": "llm-mask"}
        )

    request = MaskRequest.model_validate(
        _request_data(detections=[])
    )
    assert request.detections == ()


@pytest.mark.parametrize(
    "invalid_data",
    [
        {},
        {"text": _TEXT, "detections": []},
        {"llmDeploymentId": "llm-mask", "detections": []},
        _request_data(text=""),
        _request_data(text=1),
        {**_request_data(), "llmDeploymentId": None},
        {**_request_data(), "llmDeploymentId": "LLM MASK"},
        {**_request_data(), "unknownField": True},
        {
            "text": _TEXT,
            "llm_deployment_id": "llm-mask",
            "detections": [],
        },
        {**_request_data(), "detections": {}},
    ],
)
def test_mask_request_rejects_missing_wrong_and_extra_fields(
    invalid_data: dict[str, object],
) -> None:
    """필수값·엄격한 타입·camelCase·추가 필드 계약을 강제합니다."""

    with pytest.raises(ValidationError):
        MaskRequest.model_validate(invalid_data)


@pytest.mark.parametrize(
    "detection",
    [
        {**_detection_data(), "start": -1},
        {**_detection_data(), "end": len(_TEXT) + 1},
        {**_detection_data(), "text": "김철수"},
        {**_detection_data(), "source": "external"},
        {**_detection_data(), "type": "UNKNOWN"},
        {**_detection_data(), "score": -0.1},
        {**_detection_data(), "unknown": True},
    ],
)
def test_mask_request_rejects_invalid_detection(
    detection: dict[str, object],
) -> None:
    """각 Detection의 공통 스키마와 원문 span 일치를 검증합니다."""

    with pytest.raises(ValidationError):
        MaskRequest.model_validate(
            _request_data(detections=[detection])
        )


def test_mask_request_uses_unicode_code_point_offsets() -> None:
    """한글·이모지·결합 문자를 Python 문자 좌표로 검증합니다."""

    text = "가😀나e\u0301끝"
    request = MaskRequest.model_validate(
        {
            "text": text,
            "llmDeploymentId": "llm-mask",
            "detections": [
                {
                    "start": 1,
                    "end": 5,
                        "text": "😀나e\u0301",
                        "type": "PERSONAL",
                        "policyId": "B01",
                        "source": "llm",
                    "score": 0.1,
                }
            ],
        }
    )

    assert request.detections[0].text == text[1:5]


def test_mask_request_rejects_non_utf8_text() -> None:
    """고립 surrogate를 Prompt 입력으로 전달하지 않습니다."""

    with pytest.raises(ValidationError):
        MaskRequest.model_validate(
            {
                "text": "비밀\ud800",
                "llmDeploymentId": "llm-mask",
                "detections": [],
            }
        )


def test_mask_request_limits_detection_count_in_schema_and_runtime() -> None:
    """Detection 개수 상한을 OpenAPI와 실제 검증에 함께 적용합니다."""

    schema = MaskRequest.model_json_schema(by_alias=True)
    assert (
        schema["properties"]["detections"]["maxItems"]
        == MAX_MASK_DETECTIONS
    )

    with pytest.raises(ValidationError):
        MaskRequest.model_validate(
            _request_data(
                detections=[_detection_data()]
                * (MAX_MASK_DETECTIONS + 1)
            )
        )


def test_mask_request_defensively_copies_and_freezes_detections() -> None:
    """호출자가 원본 배열을 바꿔 검증된 요청을 변경하지 못합니다."""

    detections = [_detection_data()]
    request = MaskRequest.model_validate(
        _request_data(detections=detections)
    )
    detections.clear()

    assert len(request.detections) == 1
    assert isinstance(request.detections, tuple)
    assert not hasattr(request.detections, "append")
    with pytest.raises(ValidationError):
        request.text = "변경"  # type: ignore[misc]


def test_mask_replacement_and_response_serialize_exact_contract() -> None:
    """불변 내부 tuple을 camelCase JSON 배열로 직렬화합니다."""

    response = MaskResponse.model_validate(
        {
            "maskedText": "[[LPL_0123456789abcdef_0001]]은 ACME에 연락했습니다.",
            "replacements": [_replacement_data()],
        }
    )

    assert response.masked_text.startswith("[[LPL_")
    assert isinstance(response.replacements, tuple)
    assert response.replacements[0].entity_id == "entity-1"
    assert response.model_dump(mode="json") == {
        "maskedText": "[[LPL_0123456789abcdef_0001]]은 ACME에 연락했습니다.",
        "replacements": [
            {
                "start": 0,
                "end": 3,
                "entityId": "entity-1",
                "placeholder": "[[LPL_0123456789abcdef_0001]]",
                "types": ["PERSONAL_IDENTITY"],
                "sources": ["ner"],
            }
        ],
    }


@pytest.mark.parametrize(
    "replacement",
    [
        _replacement_data(start=3, end=3),
        _replacement_data(entityId="entity-0"),
        _replacement_data(entityId="entity-100000"),
        _replacement_data(placeholder="[[PERSON_1]]"),
        _replacement_data(placeholder="[[LPL_ABCDEF0123456789_0001]]"),
        _replacement_data(types=[]),
        _replacement_data(types=["UNKNOWN"]),
        _replacement_data(sources=[]),
        _replacement_data(sources=["external"]),
        _replacement_data(extra=True),
    ],
)
def test_mask_replacement_rejects_invalid_shape(
    replacement: dict[str, object],
) -> None:
    """응답 replacement의 span·ID·placeholder·근거 계약을 강제합니다."""

    with pytest.raises(ValidationError):
        MaskReplacement.model_validate(replacement)


def test_mask_response_requires_explicit_replacements() -> None:
    """응답에서 마스킹 근거 배열을 생략하지 않습니다."""

    with pytest.raises(ValidationError):
        MaskResponse.model_validate({"maskedText": _TEXT})
