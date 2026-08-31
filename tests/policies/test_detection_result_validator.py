"""Span 검증과 중복 정리를 묶는 탐지 결과 검증 Facade를 확인합니다."""

from __future__ import annotations

import pytest

from app.policies.detection_result_validator import DetectionResultValidator
from app.policies.span_validator import DetectionSpanValidationError
from app.schemas.detection import Detection


def _detection(
    *,
    source: str = "ner",
    score: float = 0.9,
) -> Detection:
    """Facade 테스트에 사용할 유효한 탐지 결과를 만듭니다."""

    return Detection.model_validate(
        {
            "start": 0,
            "end": 3,
            "text": "홍길동",
            "type": "PERSONAL_IDENTITY",
            "policyId": "P01",
            "source": source,
            "score": score,
        }
    )


def test_validate_checks_spans_then_resolves_duplicates() -> None:
    """유효한 결과를 먼저 검증한 뒤 출처 우선순위로 중복을 정리합니다."""

    llm = _detection(source="llm", score=1.0)
    ner = _detection(source="ner", score=0.2)

    result = DetectionResultValidator().validate(
        "홍길동",
        [llm, ner],
    )

    assert result == (ner,)


def test_validate_applies_expected_source_before_resolving() -> None:
    """Facade도 특정 Backend가 반환한 source 계약을 강제합니다."""

    item = _detection(source="llm")

    with pytest.raises(DetectionSpanValidationError) as error_info:
        DetectionResultValidator().validate(
            "홍길동",
            (item,),
            expected_source="ner",
        )

    assert error_info.value.code == "SOURCE_MISMATCH"


def test_validate_never_discards_invalid_item_before_span_validation() -> None:
    """중복 제거로 가려질 결과도 먼저 검증하여 비신뢰 batch를 거부합니다."""

    trusted = _detection(source="regex", score=1.0)
    invalid = Detection.model_construct(
        start=0,
        end=3,
        text="김철수",
        type="PERSONAL_IDENTITY",
        policy_id="P01",
        source="llm",
        score=0.1,
    )

    with pytest.raises(DetectionSpanValidationError) as error_info:
        DetectionResultValidator().validate(
            "홍길동",
            (trusted, invalid),
        )

    error = error_info.value
    assert error.code == "TEXT_MISMATCH"
    assert error.item_index == 1


def test_validate_returns_empty_tuple_for_empty_results() -> None:
    """탐지 결과가 없는 유효한 batch는 불변 빈 tuple로 반환합니다."""

    assert DetectionResultValidator().validate("원문", []) == ()


def test_facade_error_does_not_retain_sensitive_input() -> None:
    """Facade가 변환한 오류와 내부 호출 정보에 원문을 남기지 않습니다."""

    secret = "TOP_SECRET_FACADE_TEXT_83041"
    invalid = Detection.model_construct(
        start=0,
        end=len(secret),
        text="X" * len(secret),
        type="PERSONAL",
        policy_id="B01",
        source="llm",
        score=0.5,
    )

    with pytest.raises(DetectionSpanValidationError) as error_info:
        DetectionResultValidator().validate(secret, (invalid,))

    error = error_info.value
    assert secret not in str(error)
    assert secret not in repr(vars(error))
    assert error.__cause__ is None
    assert error.__context__ is None

    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if filename.endswith(
            "/app/policies/detection_result_validator.py"
        ):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
