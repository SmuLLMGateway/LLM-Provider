"""공통 JSON 값 정규화의 타입, 복사와 자원 제한 계약을 검증합니다."""

from __future__ import annotations

from collections import UserDict
from typing import Any

import pytest

from app.core.json_value import InvalidJsonValueError, normalize_json_value


def test_normalize_json_value_deep_copies_nested_json_containers() -> None:
    """중첩 Mapping과 list를 원본에서 독립된 dict와 list로 변환합니다."""

    nested_mapping = UserDict(
        {
            "text": "홍길동",
            "items": [
                {
                    "enabled": True,
                    "score": 0.98,
                    "metadata": None,
                }
            ],
        }
    )

    normalized = normalize_json_value(nested_mapping)

    assert normalized == nested_mapping.data
    assert type(normalized) is dict
    assert normalized is not nested_mapping
    assert type(normalized["items"]) is list
    assert normalized["items"] is not nested_mapping["items"]
    assert normalized["items"][0] is not nested_mapping["items"][0]


@pytest.mark.parametrize("value", [None, True, False, 0, -3, 1.5, "내용"])
def test_normalize_json_value_accepts_exact_json_scalars(value: object) -> None:
    """JSON의 null, boolean, integer, finite number와 string을 허용합니다."""

    assert normalize_json_value(value) == value


class _StringSubclass(str):
    """정확한 JSON scalar 타입 검증에 사용하는 문자열 하위 타입입니다."""


class _IntegerSubclass(int):
    """정확한 JSON scalar 타입 검증에 사용하는 정수 하위 타입입니다."""


@pytest.mark.parametrize(
    "value",
    [
        ("tuple",),
        {"set"},
        object(),
        _StringSubclass("text"),
        _IntegerSubclass(1),
    ],
)
def test_normalize_json_value_rejects_non_json_value_types(value: object) -> None:
    """JSON 값이 아닌 컬렉션, 객체와 scalar 하위 타입을 거부합니다."""

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value(value)

    assert error_info.value.code == "UNSUPPORTED_TYPE"
    assert error_info.value.path == "$"


def test_normalize_json_value_rejects_callable_without_executing_it() -> None:
    """callable을 실행하거나 결과를 읽지 않고 즉시 거부합니다."""

    called = False

    def callback() -> str:
        nonlocal called
        called = True
        return "실행됨"

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value({"callback": callback})

    assert called is False
    assert error_info.value.code == "CALLABLE_NOT_ALLOWED"
    assert error_info.value.path == "$.*"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_normalize_json_value_rejects_non_finite_numbers(value: float) -> None:
    """JSON 표준에 없는 NaN과 양·음의 무한대를 거부합니다."""

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value({"score": value})

    assert error_info.value.code == "NON_FINITE_NUMBER"
    assert error_info.value.path == "$.*"


def test_normalize_json_value_rejects_non_string_mapping_key() -> None:
    """중첩 위치를 포함해 Mapping의 문자열이 아닌 키를 거부합니다."""

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value({"nested": {1: "value"}})

    assert error_info.value.code == "NON_STRING_KEY"
    assert error_info.value.path == "$.*"


def test_normalize_json_value_rejects_non_utf8_string() -> None:
    """UTF-8로 인코딩할 수 없는 문자열을 해당 경로에서 거부합니다."""

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value({"text": "\ud800"})

    error = error_info.value
    assert error.code == "INVALID_UTF8"
    assert error.path == "$.*"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_normalize_json_value_rejects_cyclic_reference() -> None:
    """현재 순회 경로를 다시 참조하는 list 순환을 거부합니다."""

    items: list[object] = []
    items.append(items)

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value({"items": items})

    assert error_info.value.code == "CYCLIC_REFERENCE"
    assert error_info.value.path == "$.*[0]"


def test_normalize_json_value_accepts_reused_non_cyclic_container() -> None:
    """같은 컨테이너를 여러 위치에서 공유해도 순환이 아니면 각각 복사합니다."""

    shared = {"value": 1}

    normalized = normalize_json_value([shared, shared])

    assert normalized == [{"value": 1}, {"value": 1}]
    assert normalized[0] is not shared
    assert normalized[1] is not shared
    assert normalized[0] is not normalized[1]


def test_normalize_json_value_enforces_maximum_container_depth() -> None:
    """루트 컨테이너를 깊이 1로 계산해 중첩 깊이를 제한합니다."""

    assert normalize_json_value(
        {"value": 1},
        max_depth=1,
    ) == {"value": 1}

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value(
            {"nested": {"value": 1}},
            max_depth=1,
        )

    error = error_info.value
    assert error.code == "DEPTH_EXCEEDED"
    assert error.path == "$.*"
    assert error.actual_depth == 2
    assert error.max_depth == 1


def test_normalize_json_value_enforces_total_item_limit() -> None:
    """모든 Mapping 항목과 list 원소를 합산해 전체 항목 수를 제한합니다."""

    assert normalize_json_value([1, 2], max_items=2) == [1, 2]

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value([1, {"value": 2}], max_items=2)

    error = error_info.value
    assert error.code == "ITEMS_EXCEEDED"
    assert error.path == "$[1]"
    assert error.actual_items == 3
    assert error.max_items == 2


def test_normalize_json_value_enforces_utf8_string_byte_limit() -> None:
    """각 문자열 값의 UTF-8 바이트 수를 기준으로 크기를 제한합니다."""

    assert normalize_json_value("가", max_string_bytes=3) == "가"

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value("가", max_string_bytes=2)

    error = error_info.value
    assert error.code == "STRING_TOO_LARGE"
    assert error.path == "$"
    assert error.actual_bytes == 3
    assert error.max_bytes == 2


def test_normalize_json_value_applies_string_limit_to_mapping_keys() -> None:
    """문자열 크기 제한을 값뿐 아니라 Mapping 키에도 적용합니다."""

    with pytest.raises(InvalidJsonValueError) as error_info:
        normalize_json_value({"가": 1}, max_string_bytes=2)

    error = error_info.value
    assert error.code == "STRING_TOO_LARGE"
    assert error.path == "$.*"
    assert error.actual_bytes == 3
    assert error.max_bytes == 2


@pytest.mark.parametrize("parameter", ["max_depth", "max_items", "max_string_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1"])
def test_normalize_json_value_rejects_invalid_limits(
    parameter: str,
    value: Any,
) -> None:
    """정규화 제한은 bool이 아닌 1 이상의 정수 또는 None만 허용합니다."""

    arguments = {parameter: value}

    with pytest.raises(ValueError):
        normalize_json_value({}, **arguments)
