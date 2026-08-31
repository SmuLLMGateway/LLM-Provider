"""공통 JSON Codec의 엄격한 파싱과 직렬화 계약을 검증합니다."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.json_codec import (
    StrictJsonDecodeError,
    StrictJsonEncodeError,
    dump_canonical_json_utf8,
    dump_json_utf8,
    load_strict_json,
)


def _exception_graph_contains(error: BaseException, secret: str) -> bool:
    """예외와 연결된 원인 어디에도 민감한 원문이 없는지 확인합니다."""

    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))

        exposed_values = (str(current), repr(current), repr(vars(current)))
        if any(secret in value for value in exposed_values):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def test_load_strict_json_accepts_text_and_utf8_bytes() -> None:
    """문자열과 UTF-8 바이트를 동일한 JSON 값으로 읽습니다."""

    expected = {
        "name": "홍길동",
        "enabled": True,
        "items": [1, None, {"score": 0.5}],
    }
    source = (
        '{"name":"홍길동","enabled":true,'
        '"items":[1,null,{"score":0.5}]}'
    )

    assert load_strict_json(source) == expected
    assert load_strict_json(source.encode("utf-8")) == expected


class _StringSubclass(str):
    """정확한 입력 타입 검증에 사용하는 문자열 하위 타입입니다."""


class _BytesSubclass(bytes):
    """정확한 입력 타입 검증에 사용하는 바이트 하위 타입입니다."""


@pytest.mark.parametrize(
    "source",
    [None, 1, {}, [], _StringSubclass("{}"), _BytesSubclass(b"{}")],
)
def test_load_strict_json_rejects_non_exact_input_types(source: Any) -> None:
    """정확한 str 또는 bytes가 아닌 입력을 JSON 원문으로 받지 않습니다."""

    with pytest.raises(TypeError):
        load_strict_json(source)


@pytest.mark.parametrize(
    "source",
    [
        "{",
        "not-json",
        "{} trailing",
        "```json\n{}\n```",
        '{"outer":{"value":1,"value":2}}',
        '{"number":NaN}',
        '{"number":Infinity}',
        '{"number":-Infinity}',
        '{"number":1e9999}',
        '{"text":"\\ud800"}',
    ],
)
def test_load_strict_json_rejects_non_strict_json(source: str) -> None:
    """문법·중복 키·비유한 숫자·잘못된 Unicode 값을 거부합니다."""

    with pytest.raises(StrictJsonDecodeError) as error_info:
        load_strict_json(source)

    error = error_info.value
    assert error.code == "INVALID_JSON"
    assert error.actual_bytes is None
    assert error.max_bytes is None


def test_load_strict_json_rejects_invalid_utf8_bytes() -> None:
    """UTF-8로 해석할 수 없는 바이트는 JSON 파싱 전에 거부합니다."""

    with pytest.raises(StrictJsonDecodeError) as error_info:
        load_strict_json(b'{"value":"\xff"}')

    error = error_info.value
    assert error.code == "INVALID_UTF8"
    assert error.actual_bytes is None
    assert error.max_bytes is None


def test_load_strict_json_enforces_utf8_byte_limit() -> None:
    """문자 수가 아닌 UTF-8 바이트 수로 입력 크기를 제한합니다."""

    source = '"가"'
    actual_bytes = len(source.encode("utf-8"))

    assert load_strict_json(source, max_bytes=actual_bytes) == "가"

    with pytest.raises(StrictJsonDecodeError) as error_info:
        load_strict_json(source, max_bytes=actual_bytes - 1)

    error = error_info.value
    assert error.code == "TOO_LARGE"
    assert error.actual_bytes == actual_bytes
    assert error.max_bytes == actual_bytes - 1


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1.0, "1"])
def test_load_strict_json_rejects_invalid_byte_limits(max_bytes: Any) -> None:
    """바이트 제한은 bool이 아닌 1 이상의 정수 또는 None만 허용합니다."""

    with pytest.raises(ValueError):
        load_strict_json("{}", max_bytes=max_bytes)


def test_decode_error_does_not_retain_sensitive_source() -> None:
    """파싱 예외와 숨겨진 원인 예외에 민감한 JSON 원문을 보관하지 않습니다."""

    secret = "TOP_SECRET_JSON_PAYLOAD_71935"

    with pytest.raises(StrictJsonDecodeError) as error_info:
        load_strict_json(f'{{"secret":"{secret}"')

    assert not _exception_graph_contains(error_info.value, secret)


def test_dump_json_utf8_returns_compact_non_ascii_bytes_by_default() -> None:
    """일반 직렬화는 한글을 이스케이프하지 않는 compact UTF-8을 반환합니다."""

    encoded = dump_json_utf8({"name": "홍길동", "items": [1, 2]})

    assert type(encoded) is bytes
    assert encoded == (
        '{"name":"홍길동","items":[1,2]}'.encode("utf-8")
    )
    assert b"\\u" not in encoded


def test_dump_json_utf8_supports_pretty_indentation() -> None:
    """indent를 지정하면 사람이 읽을 수 있는 들여쓰기 형식을 사용합니다."""

    encoded = dump_json_utf8({"value": [1]}, indent=2)
    decoded = encoded.decode("utf-8")

    assert "\n" in decoded
    assert '  "value"' in decoded
    assert "    1" in decoded


@pytest.mark.parametrize("indent", [-1, True, 1.0, "2"])
def test_dump_json_utf8_rejects_invalid_indentation(indent: Any) -> None:
    """indent는 bool이 아닌 0 이상의 정수 또는 None만 허용합니다."""

    with pytest.raises(ValueError):
        dump_json_utf8({}, indent=indent)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        object(),
        ("tuple",),
        {"set"},
        {1: "non-string-key"},
        "\ud800",
    ],
)
def test_dump_json_utf8_wraps_unsupported_values(value: object) -> None:
    """JSON 호환 값이 아닌 입력을 안전한 공통 오류로 변환합니다."""

    with pytest.raises(StrictJsonEncodeError) as error_info:
        dump_json_utf8({"value": value})

    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


def test_dump_json_utf8_rejects_cyclic_container() -> None:
    """순환 컨테이너를 재귀 오류 대신 안전한 직렬화 오류로 변환합니다."""

    items: list[object] = []
    items.append(items)

    with pytest.raises(StrictJsonEncodeError) as error_info:
        dump_json_utf8(items)

    assert error_info.value.__cause__ is None
    assert error_info.value.__context__ is None


def test_dump_json_utf8_rejects_callable_without_executing_it() -> None:
    """직렬화 전에 callable을 실행하지 않고 거부합니다."""

    called = False

    def callback() -> str:
        nonlocal called
        called = True
        return "실행됨"

    with pytest.raises(StrictJsonEncodeError):
        dump_json_utf8({"callback": callback})

    assert called is False


def test_dump_canonical_json_utf8_is_sorted_compact_and_deterministic() -> None:
    """정규 직렬화는 키 순서와 입력 순서에 관계없는 바이트를 생성합니다."""

    first = {"한글": 3, "b": {"d": 4, "c": 2}, "a": 1}
    second = {"a": 1, "b": {"c": 2, "d": 4}, "한글": 3}

    first_encoded = dump_canonical_json_utf8(first)
    second_encoded = dump_canonical_json_utf8(second)

    assert first_encoded == second_encoded
    assert first_encoded == (
        '{"a":1,"b":{"c":2,"d":4},"한글":3}'.encode("utf-8")
    )


def test_encode_error_does_not_retain_sensitive_value_or_cause() -> None:
    """직렬화 오류가 실패한 사용자 값을 예외 체인에 보관하지 않습니다."""

    secret = "TOP_SECRET_JSON_VALUE_28364"

    class SensitiveObject:
        def __repr__(self) -> str:
            return secret

    with pytest.raises(StrictJsonEncodeError) as error_info:
        dump_json_utf8({"value": SensitiveObject()})

    assert not _exception_graph_contains(error_info.value, secret)
