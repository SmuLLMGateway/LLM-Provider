"""요청 검증 오류를 민감한 입력값 없이 이해 가능한 문장으로 변환합니다."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError


_MAX_VALIDATION_ISSUES = 5
_SAFE_FIELD_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_RESOURCE_ID_FIELDS = frozenset(
    {
        "deploymentId",
        "deployment_id",
        "llmDeploymentId",
        "nerDeploymentId",
    }
)

_GENERIC_MESSAGES = {
    "missing": "필수 필드입니다",
    "extra_forbidden": "API 계약에 정의되지 않은 추가 필드가 있습니다",
    "string_type": "문자열이어야 합니다",
    "string_too_short": "빈 문자열일 수 없습니다",
    "string_too_long": "허용된 문자열 길이를 초과했습니다",
    "bool_type": "true 또는 false여야 합니다",
    "bool_parsing": "true 또는 false여야 합니다",
    "int_type": "정수여야 합니다",
    "int_parsing": "정수여야 합니다",
    "float_type": "숫자여야 합니다",
    "float_parsing": "숫자여야 합니다",
    "finite_number": "유한한 숫자여야 합니다",
    "list_type": "JSON 배열이어야 합니다",
    "tuple_type": "JSON 배열이어야 합니다",
    "dict_type": "JSON 객체여야 합니다",
    "model_type": "JSON 객체여야 합니다",
    "model_attributes_type": "JSON 객체여야 합니다",
    "too_long": "허용된 항목 개수를 초과했습니다",
    "literal_error": "허용된 값이 아닙니다",
    "enum": "허용된 값이 아닙니다",
    "url_type": "올바른 HTTP 또는 HTTPS URL이어야 합니다",
    "url_parsing": "올바른 HTTP 또는 HTTPS URL이어야 합니다",
    "greater_than": "허용되는 최솟값보다 커야 합니다",
    "greater_than_equal": "허용되는 최솟값보다 작습니다",
    "less_than": "허용되는 최댓값보다 작아야 합니다",
    "less_than_equal": "허용되는 최댓값보다 큽니다",
    "json_invalid": "올바른 JSON 문법이 아닙니다",
    "union_tag_invalid": "허용된 객체 종류가 아닙니다",
    "union_tag_not_found": "객체 종류를 구분하는 필드가 필요합니다",
}


def _error_type(error_detail: Mapping[str, object]) -> str:
    """Pydantic 오류 종류를 안전한 문자열로 정규화합니다."""

    value = error_detail.get("type")
    return value if isinstance(value, str) else ""


def _raw_location(error_detail: Mapping[str, object]) -> tuple[object, ...]:
    """입력값이나 오류 문장을 읽지 않고 구조적 위치만 가져옵니다."""

    location = error_detail.get("loc")
    if not isinstance(location, Sequence) or isinstance(
        location,
        (str, bytes, bytearray),
    ):
        return ()
    return tuple(location)


def _field_name(error_detail: Mapping[str, object]) -> str | None:
    """고정 Schema 필드로 사용할 수 있는 마지막 위치 이름을 찾습니다."""

    for segment in reversed(_raw_location(error_detail)):
        if isinstance(segment, str) and _SAFE_FIELD_SEGMENT.fullmatch(segment):
            if segment not in {"body", "path", "query", "header", "cookie"}:
                return segment
    return None


def _format_location(error_detail: Mapping[str, object]) -> str:
    """중첩 필드와 배열 인덱스를 입력값 노출 없이 표시합니다."""

    error_type = _error_type(error_detail)
    if error_type == "json_invalid":
        return "body"

    location = list(_raw_location(error_detail))
    scope = "body"
    if location and location[0] in {
        "body",
        "path",
        "query",
        "header",
        "cookie",
    }:
        scope = location.pop(0)

    # extra_forbidden의 마지막 위치는 사용자가 만든 임의의 키일 수 있으므로
    # 오류 응답에 그대로 복사하지 않습니다.
    if error_type == "extra_forbidden" and location:
        location.pop()

    result = ""
    for segment in location:
        if isinstance(segment, int) and 0 <= segment <= 1_000_000:
            result = f"{result}[{segment}]" if result else f"항목[{segment}]"
            continue
        if not isinstance(segment, str):
            continue
        if not _SAFE_FIELD_SEGMENT.fullmatch(segment):
            continue
        result = f"{result}.{segment}" if result else segment

    return f"{scope}.{result}" if result else scope


def _format_reason(error_detail: Mapping[str, object]) -> str:
    """Pydantic 내부 문장이나 입력값 대신 고정된 안전한 사유를 반환합니다."""

    error_type = _error_type(error_detail)
    field_name = _field_name(error_detail)

    if error_type == "string_pattern_mismatch":
        if field_name in _RESOURCE_ID_FIELDS:
            return (
                "소문자 또는 숫자로 시작하고 소문자, 숫자, '.', '_', '-'만 "
                "사용할 수 있습니다"
            )
        if field_name == "adapterType":
            return (
                "소문자로 시작하고 소문자, 숫자, '.', '_', '-'만 사용할 수 "
                "있습니다"
            )
        return "허용된 문자 형식과 다릅니다"

    if field_name == "timeoutMs" and error_type in {
        "int_type",
        "int_parsing",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
    }:
        return "1 이상 300000 이하의 정수여야 합니다"

    if field_name == "baseUrl" and error_type in {
        "string_type",
        "string_too_short",
        "url_type",
        "url_parsing",
        "value_error",
    }:
        return "올바른 HTTP 또는 HTTPS URL이어야 합니다"

    return _GENERIC_MESSAGES.get(
        error_type,
        "값이 API 계약과 다릅니다",
    )


def format_validation_error_message(
    error: RequestValidationError | ValidationError,
) -> str:
    """최대 다섯 개의 안전한 필드별 오류 사유를 한 문장으로 만듭니다."""

    error_details = error.errors()
    issues: list[str] = []
    for error_detail in error_details:
        location = _format_location(error_detail)
        reason = _format_reason(error_detail)
        issue = f"{location}: {reason}"
        if issue not in issues:
            issues.append(issue)
        if len(issues) == _MAX_VALIDATION_ISSUES:
            break

    if not issues:
        return "요청 검증에 실패했습니다"

    remaining_count = max(0, len(error_details) - len(issues))
    if remaining_count:
        issues.append(f"그 외 {remaining_count}개의 오류가 있습니다")

    return f"요청 검증에 실패했습니다: {'; '.join(issues)}"


__all__ = ["format_validation_error_message"]
