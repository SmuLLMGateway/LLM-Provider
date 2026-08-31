"""Python 값을 안전한 JSON 호환 값으로 검증하고 복사합니다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Literal, TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
InvalidJsonValueErrorCode = Literal[
    "CALLABLE_NOT_ALLOWED",
    "UNSUPPORTED_TYPE",
    "NON_STRING_KEY",
    "NON_FINITE_NUMBER",
    "INVALID_UTF8",
    "CYCLIC_REFERENCE",
    "DEPTH_EXCEEDED",
    "ITEMS_EXCEEDED",
    "STRING_TOO_LARGE",
]


class InvalidJsonValueError(ValueError):
    """값이 안전한 JSON 호환 구조로 정규화될 수 없으면 발생합니다."""

    def __init__(
        self,
        code: InvalidJsonValueErrorCode,
        *,
        path: str = "$",
        type_name: str | None = None,
        actual_depth: int | None = None,
        max_depth: int | None = None,
        actual_items: int | None = None,
        max_items: int | None = None,
        actual_bytes: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.type_name = type_name
        self.actual_depth = actual_depth
        self.max_depth = max_depth
        self.actual_items = actual_items
        self.max_items = max_items
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        detail = {
            "CALLABLE_NOT_ALLOWED": "callable은 JSON 값으로 사용할 수 없습니다",
            "UNSUPPORTED_TYPE": "지원하지 않는 JSON 값 타입입니다",
            "NON_STRING_KEY": "JSON 객체의 키는 문자열이어야 합니다",
            "NON_FINITE_NUMBER": "JSON에는 유한한 숫자만 사용할 수 있습니다",
            "INVALID_UTF8": "JSON 문자열은 UTF-8로 인코딩할 수 있어야 합니다",
            "CYCLIC_REFERENCE": "JSON 값에는 순환 참조를 사용할 수 없습니다",
            "DEPTH_EXCEEDED": "JSON 값의 중첩 깊이가 제한을 초과했습니다",
            "ITEMS_EXCEEDED": "JSON 값의 항목 수가 제한을 초과했습니다",
            "STRING_TOO_LARGE": "JSON 문자열이 허용 크기를 초과했습니다",
        }[code]
        if type_name is not None:
            detail = f"{detail}: {type_name}"
        super().__init__(f"{detail}: {path}")


@dataclass(slots=True)
class _NormalizationBudget:
    """한 번의 정규화에서 소비한 컨테이너 항목 수를 기록합니다."""

    items: int = 0


def normalize_json_value(
    value: object,
    *,
    max_depth: int | None = None,
    max_items: int | None = None,
    max_string_bytes: int | None = None,
) -> JsonValue:
    """JSON 호환 타입만 허용하고 모든 컨테이너를 깊게 복사합니다."""

    _require_optional_limit(max_depth, field_name="max_depth")
    _require_optional_limit(max_items, field_name="max_items")
    _require_optional_limit(
        max_string_bytes,
        field_name="max_string_bytes",
    )

    normalized: JsonValue = None
    normalization_error: InvalidJsonValueError | None = None
    recursion_failed = False
    try:
        normalized = _normalize_json_value(
            value,
            path="$",
            active_container_ids=set(),
            depth=1,
            budget=_NormalizationBudget(),
            max_depth=max_depth,
            max_items=max_items,
            max_string_bytes=max_string_bytes,
        )
    except InvalidJsonValueError as error:
        normalization_error = _copy_public_error(error)
    except RecursionError:
        recursion_failed = True

    if normalization_error is not None:
        del value
        raise normalization_error
    if recursion_failed:
        del value
        raise InvalidJsonValueError(
            "DEPTH_EXCEEDED",
            max_depth=max_depth,
        )
    return normalized


def _copy_public_error(
    error: InvalidJsonValueError,
) -> InvalidJsonValueError:
    """내부 재귀 호출의 입력 참조를 제외한 공개 오류를 새로 만듭니다."""

    return InvalidJsonValueError(
        error.code,
        path=error.path,
        type_name=error.type_name,
        actual_depth=error.actual_depth,
        max_depth=error.max_depth,
        actual_items=error.actual_items,
        max_items=error.max_items,
        actual_bytes=error.actual_bytes,
        max_bytes=error.max_bytes,
    )


def _normalize_json_value(
    value: object,
    *,
    path: str,
    active_container_ids: set[int],
    depth: int,
    budget: _NormalizationBudget,
    max_depth: int | None,
    max_items: int | None,
    max_string_bytes: int | None,
) -> JsonValue:
    """값 하나를 재귀적으로 검증하고 독립된 JSON 컨테이너로 만듭니다."""

    if callable(value):
        raise InvalidJsonValueError(
            "CALLABLE_NOT_ALLOWED",
            path=path,
        )

    if type(value) is str:
        return _normalize_string(
            value,
            path=path,
            max_string_bytes=max_string_bytes,
        )

    if value is None or type(value) in {bool, int}:
        return value

    if type(value) is float:
        if not isfinite(value):
            raise InvalidJsonValueError(
                "NON_FINITE_NUMBER",
                path=path,
            )
        return value

    if isinstance(value, Mapping):
        _require_depth(depth, max_depth=max_depth, path=path)
        _enter_container(value, path, active_container_ids)
        try:
            normalized_mapping: dict[str, JsonValue] = {}
            for key, item in value.items():
                _consume_item(
                    budget,
                    max_items=max_items,
                    path=path,
                )
                if type(key) is not str:
                    raise InvalidJsonValueError(
                        "NON_STRING_KEY",
                        path=path,
                    )
                normalized_key = _normalize_string(
                    key,
                    path=f"{path}.*",
                    max_string_bytes=max_string_bytes,
                )
                normalized_mapping[normalized_key] = _normalize_json_value(
                    item,
                    path=f"{path}.*",
                    active_container_ids=active_container_ids,
                    depth=depth + 1,
                    budget=budget,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_string_bytes=max_string_bytes,
                )
            return normalized_mapping
        finally:
            active_container_ids.remove(id(value))

    if type(value) is list:
        _require_depth(depth, max_depth=max_depth, path=path)
        _enter_container(value, path, active_container_ids)
        try:
            normalized_list: list[JsonValue] = []
            for index, item in enumerate(value):
                _consume_item(
                    budget,
                    max_items=max_items,
                    path=path,
                )
                normalized_list.append(
                    _normalize_json_value(
                        item,
                        path=f"{path}[{index}]",
                        active_container_ids=active_container_ids,
                        depth=depth + 1,
                        budget=budget,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_string_bytes=max_string_bytes,
                    )
                )
            return normalized_list
        finally:
            active_container_ids.remove(id(value))

    raise InvalidJsonValueError(
        "UNSUPPORTED_TYPE",
        path=path,
        type_name=type(value).__name__,
    )


def _normalize_string(
    value: str,
    *,
    path: str,
    max_string_bytes: int | None,
) -> str:
    """문자열의 UTF-8 인코딩과 선택 크기 제한을 검증합니다."""

    actual_bytes = 0
    encoding_failed = False
    try:
        actual_bytes = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        del value
        raise InvalidJsonValueError("INVALID_UTF8", path=path)
    if max_string_bytes is not None and actual_bytes > max_string_bytes:
        raise InvalidJsonValueError(
            "STRING_TOO_LARGE",
            path=path,
            actual_bytes=actual_bytes,
            max_bytes=max_string_bytes,
        )
    return value


def _require_depth(
    depth: int,
    *,
    max_depth: int | None,
    path: str,
) -> None:
    """현재 컨테이너 깊이가 선택 제한 이내인지 확인합니다."""

    if max_depth is not None and depth > max_depth:
        raise InvalidJsonValueError(
            "DEPTH_EXCEEDED",
            path=path,
            actual_depth=depth,
            max_depth=max_depth,
        )


def _consume_item(
    budget: _NormalizationBudget,
    *,
    max_items: int | None,
    path: str,
) -> None:
    """컨테이너 항목 하나를 소비하고 전체 개수 제한을 적용합니다."""

    budget.items += 1
    if max_items is not None and budget.items > max_items:
        raise InvalidJsonValueError(
            "ITEMS_EXCEEDED",
            path=path,
            actual_items=budget.items,
            max_items=max_items,
        )


def _enter_container(
    value: object,
    path: str,
    active_container_ids: set[int],
) -> None:
    """현재 재귀 경로에 같은 컨테이너가 다시 나타나는지 확인합니다."""

    container_id = id(value)
    if container_id in active_container_ids:
        raise InvalidJsonValueError(
            "CYCLIC_REFERENCE",
            path=path,
        )
    active_container_ids.add(container_id)


def _require_optional_limit(
    value: int | None,
    *,
    field_name: str,
) -> None:
    """선택 자원 제한에는 1 이상의 정확한 정수만 허용합니다."""

    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"{field_name}는 None 또는 1 이상의 정수여야 합니다")


__all__ = [
    "InvalidJsonValueError",
    "InvalidJsonValueErrorCode",
    "JsonScalar",
    "JsonValue",
    "normalize_json_value",
]
