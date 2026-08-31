"""모델과 Gateway에서 받은 탐지 구간을 fail-closed로 검증합니다."""

from __future__ import annotations

from typing import Any

import pytest

from app.policies.span_validator import (
    DEFAULT_MAX_DETECTION_RESULTS,
    DetectionSpanValidationError,
    SpanValidator,
)
from app.schemas.detection import Detection


def _detection(**overrides: object) -> Detection:
    """유효한 탐지 결과에 테스트별 값을 덮어씁니다."""

    values: dict[str, object] = {
        "start": 0,
        "end": 3,
        "text": "홍길동",
        "type": "PERSONAL_IDENTITY",
        "policyId": "P01",
        "source": "ner",
        "score": 0.9,
    }
    values.update(overrides)
    return Detection.model_validate(values)


def _unsafe_detection(**overrides: object) -> Detection:
    """Pydantic 검증을 우회한 비신뢰 Detection을 만듭니다."""

    values: dict[str, object] = {
        "start": 0,
        "end": 3,
        "text": "홍길동",
        "type": "PERSONAL_IDENTITY",
        "policy_id": "P01",
        "source": "ner",
        "score": 0.9,
    }
    values.update(overrides)
    return Detection.model_construct(**values)


def _assert_error_does_not_expose(
    error: BaseException,
    secret: str,
) -> None:
    """공개 오류와 연결된 예외에 사용자 원문이 남지 않는지 확인합니다."""

    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))

        exposed = (str(current), repr(current), repr(vars(current)))
        assert all(secret not in value for value in exposed)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)

    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if filename.endswith("/app/policies/span_validator.py"):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


class _ListSubclass(list[Detection]):
    """정확한 list 타입 검증에 사용하는 하위 타입입니다."""


class _TupleSubclass(tuple[Detection, ...]):
    """정확한 tuple 타입 검증에 사용하는 하위 타입입니다."""


class _IntegerSubclass(int):
    """검증 우회 필드의 정수 하위 타입을 확인합니다."""


class _StringSubclass(str):
    """검증 우회 필드의 문자열 하위 타입을 확인합니다."""


@pytest.mark.parametrize(
    "detections",
    [None, {}, "detections", {_detection()}, _ListSubclass(), _TupleSubclass()],
)
def test_validate_rejects_non_exact_sequence_container(
    detections: object,
) -> None:
    """정확한 내장 list와 tuple 이외의 컨테이너를 거부합니다."""

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", detections)  # type: ignore[arg-type]

    error = error_info.value
    assert error.code == "INVALID_CONTAINER"
    assert error.item_index is None


@pytest.mark.parametrize("detections", [[None], [1], [{}], ["item"]])
def test_validate_rejects_non_detection_item(detections: list[object]) -> None:
    """컨테이너의 각 항목은 정확한 Detection 객체여야 합니다."""

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", detections)  # type: ignore[arg-type]

    error = error_info.value
    assert error.code == "INVALID_ITEM"
    assert error.item_index == 0


def test_validate_rejects_detection_subclass() -> None:
    """임의 동작을 추가할 수 있는 Detection 하위 타입을 거부합니다."""

    class CustomDetection(Detection):
        pass

    item = CustomDetection(
        start=0,
        end=3,
        text="홍길동",
        type="PERSONAL_IDENTITY",
        policyId="P01",
        source="ner",
        score=0.9,
    )

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", (item,))

    assert error_info.value.code == "INVALID_ITEM"
    assert error_info.value.item_index == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"start": True},
        {"start": _IntegerSubclass(0)},
        {"end": 3.0},
        {"text": ""},
        {"text": _StringSubclass("홍길동")},
        {"type": 1},
        {"source": "model"},
        {"score": float("nan")},
        {"score": -0.1},
        {"score": 1.1},
    ],
)
def test_validate_revalidates_model_construct_items(
    overrides: dict[str, object],
) -> None:
    """model_construct로 우회한 필드 타입과 제약도 다시 검증합니다."""

    item = _unsafe_detection(**overrides)

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", (item,))

    error = error_info.value
    assert error.code == "INVALID_ITEM"
    assert error.item_index == 0


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (-1, 1),
        (0, 0),
        (1, 1),
        (2, 1),
        (0, 4),
        (3, 4),
    ],
)
def test_validate_rejects_every_invalid_span_boundary(
    start: int,
    end: int,
) -> None:
    """0 <= start < end <= 원문 길이 규칙의 모든 경계를 확인합니다."""

    item = _unsafe_detection(start=start, end=end, text="홍")

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", (item,))

    error = error_info.value
    assert error.code == "INVALID_SPAN"
    assert error.item_index == 0


def test_validate_accepts_full_text_boundary_and_returns_tuple() -> None:
    """원문 전체를 정확히 가리키는 양 끝 경계는 허용합니다."""

    item = _detection()
    result = SpanValidator().validate("홍길동", [item])

    assert result == (item,)
    assert result[0] is not item


def test_validate_uses_unicode_code_point_offsets() -> None:
    """한글과 이모지도 Python 문자열의 문자 위치 기준으로 검증합니다."""

    original_text = "가😀나다"
    item = _detection(start=1, end=3, text="😀나")

    assert SpanValidator().validate(original_text, (item,)) == (item,)


def test_validate_rejects_invalid_utf8_original_text() -> None:
    """고립 surrogate가 포함된 원문은 비신뢰 입력으로 거부합니다."""

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍\ud800길동", ())

    error = error_info.value
    assert error.code == "INVALID_TEXT"
    assert error.item_index is None


def test_validate_rejects_invalid_utf8_detection_text() -> None:
    """검증을 우회해 고립 surrogate를 가진 탐지 문자열도 거부합니다."""

    item = _unsafe_detection(start=0, end=1, text="\ud800")

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", (item,))

    error = error_info.value
    assert error.code == "INVALID_ITEM"
    assert error.item_index == 0


def test_validate_rejects_text_that_differs_from_original_slice() -> None:
    """Detection.text가 원문의 [start, end) 구간과 다르면 거부합니다."""

    item = _detection(text="김철수")

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", (item,))

    error = error_info.value
    assert error.code == "TEXT_MISMATCH"
    assert error.item_index == 0


def test_validate_applies_optional_expected_source() -> None:
    """Backend별 검증에서는 기대한 source와 다른 결과를 거부합니다."""

    item = _detection(source="llm")

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate("홍길동", (item,), expected_source="ner")

    error = error_info.value
    assert error.code == "SOURCE_MISMATCH"
    assert error.item_index == 0


def test_validate_skips_source_constraint_when_not_requested() -> None:
    """expected_source가 없으면 세 공통 source를 모두 허용합니다."""

    items = tuple(
        _detection(source=source)
        for source in ("regex", "ner", "llm")
    )

    assert SpanValidator().validate("홍길동", items) == items


def test_validate_enforces_result_count_before_item_validation() -> None:
    """개수 상한을 넘으면 개별 항목을 읽기 전에 batch 전체를 거부합니다."""

    invalid_item = _unsafe_detection(start=-1)

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator(max_detections=1).validate(
            "홍길동",
            (invalid_item, invalid_item),
        )

    error = error_info.value
    assert error.code == "TOO_MANY_DETECTIONS"
    assert error.item_index is None
    assert error.actual_items == 2
    assert error.max_detections == 1


def test_validate_accepts_result_count_at_exact_limit() -> None:
    """탐지 결과 개수가 설정한 상한과 같으면 허용합니다."""

    item = _detection()

    assert SpanValidator(max_detections=1).validate("홍길동", [item]) == (
        item,
    )


def test_default_detection_limit_is_positive_integer() -> None:
    """기본 상한은 실수로 무제한이 되지 않는 양의 정수여야 합니다."""

    assert type(DEFAULT_MAX_DETECTION_RESULTS) is int
    assert DEFAULT_MAX_DETECTION_RESULTS > 0


@pytest.mark.parametrize("max_detections", [0, -1, True, 1.0, "1", None])
def test_constructor_rejects_invalid_limits(max_detections: Any) -> None:
    """개수 상한은 bool이 아닌 1 이상의 정수만 허용합니다."""

    with pytest.raises(ValueError):
        SpanValidator(max_detections=max_detections)


def test_validation_error_does_not_expose_original_text_or_items() -> None:
    """오류 객체와 예외 체인 어디에도 민감한 원문을 보존하지 않습니다."""

    secret = "TOP_SECRET_DETECTION_TEXT_58214"
    item = _unsafe_detection(
        start=0,
        end=len(secret),
        text="X" * len(secret),
    )

    with pytest.raises(DetectionSpanValidationError) as error_info:
        SpanValidator().validate(secret, (item,))

    error = error_info.value
    _assert_error_does_not_expose(error, secret)
    assert set(vars(error)) <= {
        "code",
        "item_index",
        "actual_items",
        "max_detections",
    }
    assert not hasattr(error, "original_text")
    assert not hasattr(error, "detections")
