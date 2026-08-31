"""LLM 후보 판정·신규 탐지 JSON 출력 Parser를 검증합니다."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.services.llm_detection_output_parser import (
    LlmCandidateDecision,
    LlmDetectionCandidate,
    LlmDetectionOutput,
    LlmDetectionOutputError,
    LlmDetectionOutputParser,
)


def _decision(**overrides: object) -> dict[str, object]:
    """유효한 후보 판정에 테스트별 필드를 덮어씁니다."""

    item: dict[str, object] = {
        "candidateId": "R001",
        "decision": "CONFIRMED",
    }
    item.update(overrides)
    return item


def _detection(**overrides: object) -> dict[str, object]:
    """유효한 신규 탐지에 테스트별 필드를 덮어씁니다."""

    item: dict[str, object] = {
        "text": "홍길동",
        "type": "PERSONAL_IDENTITY",
        "score": 0.98,
    }
    item.update(overrides)
    return item


def _output(
    *,
    decisions: list[object] | None = None,
    detections: list[object] | None = None,
) -> str:
    """후보 판정과 신규 탐지를 순수 JSON 객체로 직렬화합니다."""

    return json.dumps(
        {
            "candidateDecisions": decisions or [],
            "newDetections": detections or [],
        },
        ensure_ascii=False,
    )


def test_parse_returns_frozen_structured_output() -> None:
    """유효한 객체를 두 불변 tuple로 분리해 반환합니다."""

    result = LlmDetectionOutputParser().parse(
        _output(
            decisions=[
                _decision(),
                _decision(candidateId="N001", decision="UNCERTAIN"),
            ],
            detections=[
                _detection(),
                _detection(text="프로젝트 알파", type="PERSONAL", score=0.87),
            ],
        )
    )

    assert result == LlmDetectionOutput(
        candidate_decisions=(
            LlmCandidateDecision(
                candidateId="R001",
                decision="CONFIRMED",
            ),
            LlmCandidateDecision(
                candidateId="N001",
                decision="UNCERTAIN",
            ),
        ),
        new_detections=(
            LlmDetectionCandidate(
                text="홍길동",
                type="PERSONAL_IDENTITY",
                score=0.98,
            ),
            LlmDetectionCandidate(
                text="프로젝트 알파",
                type="PERSONAL",
                score=0.87,
            ),
        ),
    )
    with pytest.raises(ValidationError):
        result.candidate_decisions[0].decision = "REJECTED"


class CustomString(str):
    """정확한 내장 str 타입 검증에 사용할 문자열 하위 타입입니다."""


@pytest.mark.parametrize("output", [None, b"{}", [], {}, CustomString("{}")])
def test_parse_rejects_non_exact_string_input(output: object) -> None:
    """문자열 하위 타입을 포함해 정확한 내장 str 이외 입력을 거부합니다."""

    with pytest.raises(TypeError, match="문자열"):
        LlmDetectionOutputParser().parse(output)  # type: ignore[arg-type]


def test_parse_accepts_empty_contract_and_json_whitespace() -> None:
    """후보와 신규 탐지가 모두 없는 객체를 허용합니다."""

    result = LlmDetectionOutputParser().parse(
        " \r\n" + _output() + "\t"
    )

    assert result == LlmDetectionOutput((), ())


@pytest.mark.parametrize(
    "output",
    ["{", "not-json", "```json\n{}\n```", "{}\n추가 설명"],
)
def test_parse_rejects_malformed_or_wrapped_json(output: str) -> None:
    """잘못된 JSON과 코드 블록·설명이 붙은 출력을 거부합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(output)

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_JSON"


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        "text",
        1,
        True,
        {},
        {"candidateDecisions": []},
        {"newDetections": []},
        {
            "candidateDecisions": [],
            "newDetections": [],
            "extra": True,
        },
        {"candidateDecisions": {}, "newDetections": []},
        {"candidateDecisions": [], "newDetections": {}},
    ],
)
def test_parse_rejects_invalid_top_level(value: object) -> None:
    """최상위 객체의 정확한 두 배열 필드 계약을 강제합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(
            json.dumps(value, ensure_ascii=False)
        )

    assert error_info.value.code == (
        "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL"
    )


@pytest.mark.parametrize(
    "item",
    [
        None,
        True,
        1,
        "item",
        [],
        {"candidateId": "R001"},
        _decision(extra=True),
        _decision(candidateId="R01"),
        _decision(candidateId="X001"),
        _decision(decision="ACCEPTED"),
        _decision(decision=1),
    ],
)
def test_parse_rejects_invalid_candidate_decision(item: object) -> None:
    """후보 판정의 ID·결정값·정확한 필드 계약을 강제합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(_output(decisions=[item]))

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_DECISION"
    assert error_info.value.item_index == 0


@pytest.mark.parametrize(
    "item",
    [
        None,
        True,
        1,
        "item",
        [],
        {"text": "홍길동"},
        _detection(source="llm"),
        _detection(start=0),
        _detection(text=3),
        _detection(type=False),
        _detection(type="UNKNOWN"),
        _detection(score="0.98"),
        _detection(text=""),
        _detection(score=-0.01),
        _detection(score=1.01),
    ],
)
def test_parse_rejects_invalid_new_detection(item: object) -> None:
    """신규 탐지의 정확한 세 필드와 값 제약을 강제합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(_output(detections=[item]))

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_ITEM"
    assert error_info.value.item_index == 0


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parse_rejects_non_standard_json_numbers(constant: str) -> None:
    """비표준 숫자를 JSON 오류로 거부합니다."""

    output = (
        '{"candidateDecisions":[],"newDetections":['
        '{"text":"홍길동","type":"PERSONAL_IDENTITY","score":'
        + constant
        + "}]}"
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(output)

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_JSON"


def test_parse_rejects_duplicate_object_keys() -> None:
    """중복 키로 앞선 값을 숨기는 JSON 객체를 거부합니다."""

    output = (
        '{"candidateDecisions":[],"candidateDecisions":[],'
        '"newDetections":[]}'
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(output)

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_JSON"


def test_parse_enforces_utf8_byte_limit() -> None:
    """문자 수가 아니라 UTF-8 바이트 수로 출력 크기를 제한합니다."""

    output = _output(detections=[_detection(text="가")])

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser(max_output_bytes=4).parse(output)

    error = error_info.value
    assert error.code == "LLM_DETECTION_OUTPUT_TOO_LARGE"
    assert error.actual_bytes == len(output.encode("utf-8"))
    assert error.max_output_bytes == 4


def test_parse_enforces_candidate_decision_count_limit() -> None:
    """후보 판정 수가 설정 상한을 넘으면 전체 출력을 거부합니다."""

    output = _output(
        decisions=[_decision(), _decision(candidateId="R002")]
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser(max_candidate_decisions=1).parse(output)

    error = error_info.value
    assert error.code == "LLM_DETECTION_OUTPUT_TOO_MANY_DECISIONS"
    assert error.actual_items == 2
    assert error.max_detections == 1


def test_parse_enforces_new_detection_count_limit() -> None:
    """신규 탐지 수가 설정 상한을 넘으면 전체 출력을 거부합니다."""

    output = _output(
        detections=[_detection(), _detection(text="김철수")]
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser(max_detections=1).parse(output)

    error = error_info.value
    assert error.code == "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS"
    assert error.actual_items == 2
    assert error.max_detections == 1


@pytest.mark.parametrize(
    "parameter",
    ["max_output_bytes", "max_candidate_decisions", "max_detections"],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", None])
def test_constructor_rejects_invalid_limits(
    parameter: str,
    value: object,
) -> None:
    """세 실행 한도에는 bool이 아닌 1 이상의 정수만 허용합니다."""

    with pytest.raises(ValueError):
        LlmDetectionOutputParser(
            **{parameter: value}  # type: ignore[arg-type]
        )


def test_error_does_not_expose_model_output() -> None:
    """오류 메시지와 속성에 민감할 수 있는 모델 원문을 남기지 않습니다."""

    secret = "TOP_SECRET_CUSTOMER_PAYLOAD_91827"

    with pytest.raises(LlmDetectionOutputError) as error_info:
        LlmDetectionOutputParser().parse(f"not-json-{secret}")

    error = error_info.value
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in repr(vars(error))
    assert not hasattr(error, "output")
