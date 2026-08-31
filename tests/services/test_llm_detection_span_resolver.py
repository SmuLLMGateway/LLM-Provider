"""좌표 없는 LLM 후보를 원문 Detection으로 바꾸는 Resolver를 검증합니다."""

from __future__ import annotations

import pytest

from app.schemas.detection import (
    Detection,
    MULTI_OCCURRENCE_DETECTION_TYPES,
    policy_id_for_detection_type,
)
from app.services.llm_detection_output_parser import (
    LlmDetectionCandidate,
    LlmDetectionOutputError,
)
from app.services.llm_detection_span_resolver import (
    LlmDetectionSpanResolver,
)


def _candidate(
    *,
    text: str = "홍길동",
    detection_type: str = "PERSONAL_IDENTITY",
    score: float = 0.9,
) -> LlmDetectionCandidate:
    """유효한 좌표 없는 LLM 탐지 후보를 만듭니다."""

    return LlmDetectionCandidate.model_validate(
        {
            "text": text,
            "type": detection_type,
            "score": score,
        }
    )


def test_resolve_builds_exact_unicode_character_spans() -> None:
    """Python Unicode 문자 기준으로 정확한 원문 좌표를 계산합니다."""

    result = LlmDetectionSpanResolver(max_detections=10).resolve(
        "😀 담당자는 홍길동입니다.",
        (_candidate(),),
    )

    assert result == (
        Detection(
            start=7,
            end=10,
            text="홍길동",
            type="PERSONAL_IDENTITY",
            policyId="P01",
            source="llm",
            score=0.9,
        ),
    )


def test_resolve_expands_every_occurrence_of_the_same_text() -> None:
    """NER 우선 개체 문자열은 모든 출현 위치로 확장합니다."""

    result = LlmDetectionSpanResolver(max_detections=10).resolve(
        "홍길동과 홍길동",
        (_candidate(),),
    )

    assert tuple((item.start, item.end, item.text) for item in result) == (
        (0, 3, "홍길동"),
        (5, 8, "홍길동"),
    )


def test_resolve_preserves_overlapping_literal_occurrences() -> None:
    """반복 가능한 타입은 겹치는 동일 문자열 출현도 빠뜨리지 않습니다."""

    result = LlmDetectionSpanResolver(max_detections=10).resolve(
        "aaaa",
        (_candidate(text="aa", detection_type="CONTACT"),),
    )

    assert tuple((item.start, item.end) for item in result) == (
        (0, 2),
        (1, 3),
        (2, 4),
    )


def test_resolve_rejects_repeated_contextual_text() -> None:
    """문맥형 후보 문자열이 여러 위치에 있으면 임의 확장하지 않습니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionSpanResolver(max_detections=10).resolve(
            "프로젝트 알파와 프로젝트 알파",
            (_candidate(text="프로젝트 알파", detection_type="PERSONAL"),),
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS"
    assert error_info.value.item_index == 0


@pytest.mark.parametrize(
    "detection_type",
    MULTI_OCCURRENCE_DETECTION_TYPES,
)
def test_every_multi_occurrence_type_can_expand_repeated_text(
    detection_type: str,
) -> None:
    """정의된 모든 반복 가능 타입에 같은 위치 확장 정책을 적용합니다."""

    result = LlmDetectionSpanResolver(max_detections=10).resolve(
        "A A",
        (_candidate(text="A", detection_type=detection_type),),
    )

    assert tuple((item.start, item.end) for item in result) == (
        (0, 1),
        (2, 3),
    )
    assert all(
        item.policy_id == policy_id_for_detection_type(item.type)
        for item in result
    )


def test_resolve_rejects_candidate_text_absent_from_original() -> None:
    """LLM이 원문에 없는 문자열을 만들면 좌표를 추측하지 않고 거부합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionSpanResolver(max_detections=10).resolve(
            "홍길동",
            (_candidate(text="김철수"),),
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND"
    assert error_info.value.item_index == 0


def test_resolve_enforces_expanded_detection_limit() -> None:
    """후보 수가 적어도 원문 출현 확장 결과가 상한을 넘으면 거부합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionSpanResolver(max_detections=2).resolve(
            "가가가",
            (_candidate(text="가"),),
        )

    error = error_info.value
    assert error.code == "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS"
    assert error.actual_items == 3
    assert error.max_detections == 2


@pytest.mark.parametrize("candidates", [[], {}, None, "candidate"])
def test_resolve_rejects_non_tuple_candidate_container(
    candidates: object,
) -> None:
    """주입 Parser가 tuple이 아닌 결과를 반환하면 거부합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionSpanResolver(max_detections=10).resolve(
            "홍길동",
            candidates,
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_ITEM"


def test_resolve_revalidates_injected_candidate() -> None:
    """model_construct로 검증을 우회한 후보도 새 모델로 다시 검증합니다."""

    invalid = LlmDetectionCandidate.model_construct(
        text="홍길동",
        type="UNKNOWN",
        score=0.9,
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionSpanResolver(max_detections=10).resolve(
            "홍길동",
            (invalid,),
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_ITEM"
    assert error_info.value.item_index == 0


@pytest.mark.parametrize("max_detections", [0, -1, True, 1.0, "1", None])
def test_constructor_rejects_invalid_limit(max_detections: object) -> None:
    """확장 결과 상한은 bool이 아닌 1 이상의 정수만 허용합니다."""

    with pytest.raises(ValueError):
        LlmDetectionSpanResolver(
            max_detections=max_detections,  # type: ignore[arg-type]
        )
