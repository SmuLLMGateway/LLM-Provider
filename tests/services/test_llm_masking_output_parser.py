"""비신뢰 LLM 마스킹 JSON Parser의 엄격한 계약을 검증합니다."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from app.services.llm_masking_output_parser import (
    LlmMaskingOutputError,
    LlmMaskingOutputParser,
    MaskEntityAssignment,
    ParsedMaskingOutput,
)


def _assignment(
    target_id: object = "target-1",
    entity_id: object = "entity-1",
    **extra: object,
) -> dict[str, object]:
    """테스트별로 변경할 수 있는 assignment JSON 객체를 만듭니다."""

    item: dict[str, object] = {
        "targetId": target_id,
        "entityId": entity_id,
    }
    item.update(extra)
    return item


def _output(
    *assignments: object,
    masked_text: object = "[[LPL_0123456789abcdef_0001]]",
) -> str:
    """한글을 보존한 마스킹 모델 출력 JSON을 만듭니다."""

    return json.dumps(
        {
            "maskedText": masked_text,
            "assignments": list(assignments),
        },
        ensure_ascii=False,
    )


def test_parse_returns_frozen_internal_contract() -> None:
    """유효한 JSON 객체를 불변 dataclass와 tuple로 변환합니다."""

    parsed = LlmMaskingOutputParser().parse(
        _output(
            _assignment(),
            _assignment("target-2", "entity-1"),
        )
    )

    assert parsed == ParsedMaskingOutput(
        masked_text="[[LPL_0123456789abcdef_0001]]",
        assignments=(
            MaskEntityAssignment("target-1", "entity-1"),
            MaskEntityAssignment("target-2", "entity-1"),
        ),
    )
    assert isinstance(parsed.assignments, tuple)
    with pytest.raises(FrozenInstanceError):
        parsed.masked_text = "changed"  # type: ignore[misc]


class CustomString(str):
    """exact str 검증에 사용할 문자열 하위 타입입니다."""


@pytest.mark.parametrize(
    "output",
    [None, b"{}", {}, [], 1, CustomString("{}")],
)
def test_parse_rejects_non_exact_string(output: object) -> None:
    """내장 str 이외의 모델 출력 컨테이너를 거부합니다."""

    with pytest.raises(TypeError, match="문자열"):
        LlmMaskingOutputParser().parse(output)  # type: ignore[arg-type]


def test_parse_accepts_empty_assignments_and_json_whitespace() -> None:
    """문법 Parser는 빈 assignment와 JSON 주변 공백을 허용합니다."""

    parsed = LlmMaskingOutputParser().parse(
        " \r\n" + _output(masked_text="원문") + "\t"
    )

    assert parsed.masked_text == "원문"
    assert parsed.assignments == ()


@pytest.mark.parametrize(
    "output",
    [
        "{",
        "not-json",
        "```json\n{}\n```",
        _output() + "\n설명",
        '{"maskedText":"x","assignments":[],"maskedText":"y"}',
        '{"maskedText":NaN,"assignments":[]}',
        '{"maskedText":"\\ud800","assignments":[]}',
        "\ud800",
    ],
)
def test_parse_rejects_malformed_wrapped_or_non_strict_json(
    output: str,
) -> None:
    """설명·중복 키·비표준 숫자·잘못된 Unicode를 거부합니다."""

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser().parse(output)

    assert error_info.value.code == "LLM_MASKING_OUTPUT_INVALID_JSON"


@pytest.mark.parametrize(
    "output",
    [
        "[]",
        "null",
        '"text"',
        "1",
        "true",
        json.dumps({"maskedText": "x"}),
        json.dumps({"assignments": []}),
        json.dumps(
            {"maskedText": "x", "assignments": [], "extra": True}
        ),
        json.dumps({"maskedText": 1, "assignments": []}),
        json.dumps({"maskedText": "x", "assignments": {}}),
    ],
)
def test_parse_rejects_invalid_top_level_contract(output: str) -> None:
    """최상위 값·키 집합·필드 exact type을 엄격히 확인합니다."""

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser().parse(output)

    assert error_info.value.code == (
        "LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL"
    )
    assert error_info.value.item_index is None


@pytest.mark.parametrize(
    "item",
    [
        None,
        True,
        1,
        "assignment",
        [],
        {},
        {"targetId": "target-1"},
        {"entityId": "entity-1"},
        _assignment(extra=True),
        _assignment(target_id=1),
        _assignment(entity_id=1),
        _assignment(target_id=""),
        _assignment(entity_id=""),
    ],
)
def test_parse_rejects_invalid_assignment_item(item: object) -> None:
    """assignment의 객체 형태와 정확한 두 문자열 필드를 검증합니다."""

    output = _output(_assignment(), item)

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser().parse(output)

    error = error_info.value
    assert error.code == "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT"
    assert error.item_index == 1


def test_parse_rejects_duplicate_nested_assignment_key() -> None:
    """assignment 안의 중복 키도 엄격한 JSON 오류로 처리합니다."""

    output = (
        '{"maskedText":"x","assignments":['
        '{"targetId":"target-1","targetId":"target-2",'
        '"entityId":"entity-1"}]}'
    )

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser().parse(output)

    assert error_info.value.code == "LLM_MASKING_OUTPUT_INVALID_JSON"


def test_parse_enforces_utf8_output_byte_limit_before_shape() -> None:
    """형식 검증 전에 전체 출력의 UTF-8 byte 상한을 적용합니다."""

    output = '"가"'

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser(max_output_bytes=4).parse(output)

    error = error_info.value
    assert error.code == "LLM_MASKING_OUTPUT_TOO_LARGE"
    assert error.actual_bytes == len(output.encode("utf-8")) == 5
    assert error.max_output_bytes == 4


def test_parse_accepts_output_at_exact_byte_limit() -> None:
    """출력 byte 수가 상한과 정확히 같으면 허용합니다."""

    output = _output(_assignment())

    assert LlmMaskingOutputParser(
        max_output_bytes=len(output.encode("utf-8"))
    ).parse(output).assignments[0].target_id == "target-1"


def test_parse_enforces_assignment_count_limit() -> None:
    """assignment 항목 수가 상한을 넘으면 개별 항목 전에 거부합니다."""

    output = _output(
        _assignment(),
        _assignment("target-2", "entity-2"),
    )

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser(max_assignments=1).parse(output)

    error = error_info.value
    assert error.code == "LLM_MASKING_OUTPUT_TOO_MANY_ASSIGNMENTS"
    assert error.actual_items == 2
    assert error.max_assignments == 1


def test_parse_accepts_assignment_count_at_exact_limit() -> None:
    """assignment 수가 설정 상한과 같으면 정상 파싱합니다."""

    parsed = LlmMaskingOutputParser(max_assignments=1).parse(
        _output(_assignment())
    )

    assert len(parsed.assignments) == 1


@pytest.mark.parametrize(
    "parameter",
    ["max_output_bytes", "max_assignments"],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", None])
def test_constructor_rejects_invalid_limits(
    parameter: str,
    value: object,
) -> None:
    """Parser 자원 제한은 1 이상의 exact int만 허용합니다."""

    with pytest.raises(ValueError):
        LlmMaskingOutputParser(
            **{parameter: value}  # type: ignore[arg-type]
        )


def test_parser_error_does_not_expose_or_retain_model_output() -> None:
    """오류 객체와 예외 체인에 민감한 모델 원문을 남기지 않습니다."""

    secret = "PRIVATE-MASK-MODEL-OUTPUT-91827"

    with pytest.raises(LlmMaskingOutputError) as error_info:
        LlmMaskingOutputParser().parse(f"not-json-{secret}")

    error = error_info.value
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in repr(vars(error))
    assert error.__cause__ is None
    assert not hasattr(error, "output")

