"""애플리케이션 경계에서 공유하는 엄격한 JSON Codec을 제공합니다."""

from __future__ import annotations

import json
from typing import Literal

from app.core.json_value import (
    InvalidJsonValueError,
    JsonValue,
    normalize_json_value,
)


StrictJsonDecodeErrorCode = Literal[
    "INVALID_UTF8",
    "INVALID_JSON",
    "TOO_LARGE",
]


class StrictJsonDecodeError(ValueError):
    """JSON 입력을 안전하게 해석할 수 없으면 발생합니다."""

    def __init__(
        self,
        code: StrictJsonDecodeErrorCode,
        *,
        actual_bytes: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.code = code
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        detail = {
            "INVALID_UTF8": "JSON 입력은 유효한 UTF-8이어야 합니다",
            "INVALID_JSON": "JSON 입력이 엄격한 형식에 맞지 않습니다",
            "TOO_LARGE": "JSON 입력이 허용 크기를 초과했습니다",
        }[code]
        super().__init__(f"{code}: {detail}")


class StrictJsonEncodeError(ValueError):
    """값을 안전한 UTF-8 JSON으로 직렬화할 수 없으면 발생합니다."""

    def __init__(self) -> None:
        super().__init__("값을 유효한 UTF-8 JSON으로 직렬화할 수 없습니다")


def load_strict_json(
    source: str | bytes,
    *,
    max_bytes: int | None = None,
) -> JsonValue:
    """UTF-8, 크기, 표준 숫자와 중복 키를 검증해 JSON을 해석합니다."""

    if type(source) not in {str, bytes}:
        raise TypeError("source는 정확한 str 또는 bytes여야 합니다")
    _require_optional_limit(max_bytes, field_name="max_bytes")

    encoded_source = b""
    if type(source) is str:
        encoding_failed = False
        try:
            encoded_source = source.encode("utf-8")
        except UnicodeEncodeError:
            encoding_failed = True
        if encoding_failed:
            del source
            raise StrictJsonDecodeError("INVALID_UTF8")
    else:
        encoded_source = source

    actual_bytes = len(encoded_source)
    if max_bytes is not None and actual_bytes > max_bytes:
        del source, encoded_source
        raise StrictJsonDecodeError(
            "TOO_LARGE",
            actual_bytes=actual_bytes,
            max_bytes=max_bytes,
        )

    decoded_source = ""
    decoding_failed = False
    try:
        decoded_source = encoded_source.decode("utf-8")
    except UnicodeDecodeError:
        decoding_failed = True
    if decoding_failed:
        del source, encoded_source
        raise StrictJsonDecodeError("INVALID_UTF8")

    parsed: object = None
    parsing_failed = False
    try:
        parsed = json.loads(
            decoded_source,
            parse_constant=_reject_non_standard_constant,
            object_pairs_hook=_build_unique_object,
        )
    except (ValueError, RecursionError, OverflowError):
        parsing_failed = True

    if parsing_failed:
        del source, encoded_source, decoded_source
        raise StrictJsonDecodeError("INVALID_JSON")

    normalized: JsonValue = None
    value_validation_failed = False
    try:
        normalized = normalize_json_value(parsed)
    except InvalidJsonValueError:
        value_validation_failed = True
    if value_validation_failed:
        del source, encoded_source, decoded_source, parsed
        raise StrictJsonDecodeError("INVALID_JSON")
    return normalized


def dump_json_utf8(
    value: object,
    *,
    indent: int | None = None,
) -> bytes:
    """유한한 숫자만 허용해 UTF-8 JSON 바이트로 직렬화합니다."""

    _require_indent(indent)
    separators = None if indent is not None else (",", ":")
    return _dump_json_utf8(
        value,
        indent=indent,
        separators=separators,
        sort_keys=False,
    )


def dump_canonical_json_utf8(value: object) -> bytes:
    """Snapshot과 해시에 사용할 정렬된 compact UTF-8 JSON을 만듭니다."""

    return _dump_json_utf8(
        value,
        indent=None,
        separators=(",", ":"),
        sort_keys=True,
    )


def _dump_json_utf8(
    value: object,
    *,
    indent: int | None,
    separators: tuple[str, str] | None,
    sort_keys: bool,
) -> bytes:
    """공통 직렬화 정책을 적용하고 내부 예외가 입력을 보존하지 않게 합니다."""

    normalized: JsonValue = None
    normalization_failed = False
    try:
        normalized = normalize_json_value(value)
    except InvalidJsonValueError:
        normalization_failed = True
    if normalization_failed:
        del value
        raise StrictJsonEncodeError()

    serialized = ""
    serialization_failed = False
    try:
        serialized = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            indent=indent,
            separators=separators,
            sort_keys=sort_keys,
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        serialization_failed = True
    if serialization_failed:
        del value, normalized
        raise StrictJsonEncodeError()

    encoded = b""
    encoding_failed = False
    try:
        encoded = serialized.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        del value, normalized, serialized
        raise StrictJsonEncodeError()
    return encoded


def _require_optional_limit(
    value: int | None,
    *,
    field_name: str,
) -> None:
    """선택 자원 제한에는 1 이상의 정확한 정수만 허용합니다."""

    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"{field_name}는 None 또는 1 이상의 정수여야 합니다")


def _require_indent(indent: int | None) -> None:
    """JSON 들여쓰기에는 0 이상의 정확한 정수만 허용합니다."""

    if indent is not None and (type(indent) is not int or indent < 0):
        raise ValueError("indent는 None 또는 0 이상의 정수여야 합니다")


def _reject_non_standard_constant(value: str) -> object:
    """표준 JSON에 없는 NaN과 Infinity 계열 상수를 거부합니다."""

    del value
    raise ValueError("비표준 JSON 숫자는 허용하지 않습니다")


def _build_unique_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """같은 키가 두 번 나타나는 모호한 JSON 객체를 거부합니다."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 JSON 객체 키는 허용하지 않습니다")
        result[key] = value
    return result


__all__ = [
    "StrictJsonDecodeError",
    "StrictJsonDecodeErrorCode",
    "StrictJsonEncodeError",
    "dump_canonical_json_utf8",
    "dump_json_utf8",
    "load_strict_json",
]
