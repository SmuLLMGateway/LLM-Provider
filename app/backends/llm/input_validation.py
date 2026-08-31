"""LLM Backend 공통 호출 인자의 구조를 검증하고 복사합니다."""

from __future__ import annotations

from typing import cast

from app.backends.llm.base import (
    LlmMessages,
    LlmOutputSchema,
    LlmParameters,
)
from app.core.json_value import (
    InvalidJsonValueError,
    normalize_json_value,
)


class LlmInputValidationError(ValueError):
    """LLM 공통 호출 인자의 구조가 잘못되면 발생합니다."""


def normalize_llm_inputs(
    messages: LlmMessages,
    parameters: LlmParameters,
    output_schema: LlmOutputSchema | None,
) -> tuple[LlmMessages, LlmParameters, LlmOutputSchema | None]:
    """LLM 입력 구조를 검증하고 Backend가 사용할 깊은 복사본을 반환합니다."""

    if not isinstance(messages, list):
        raise LlmInputValidationError("messages는 배열이어야 합니다")
    if not isinstance(parameters, dict):
        raise LlmInputValidationError("parameters는 객체여야 합니다")
    if any(not isinstance(key, str) for key in parameters):
        raise LlmInputValidationError(
            "parameters의 키는 문자열이어야 합니다"
        )

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise LlmInputValidationError(
                f"messages[{index}]는 객체여야 합니다"
            )
        if any(not isinstance(key, str) for key in message):
            raise LlmInputValidationError(
                f"messages[{index}]의 키는 문자열이어야 합니다"
            )

    if output_schema is not None and not isinstance(
        output_schema,
        dict,
    ):
        raise LlmInputValidationError(
            "output_schema는 객체여야 합니다"
        )
    if output_schema is not None and any(
        not isinstance(key, str) for key in output_schema
    ):
        raise LlmInputValidationError(
            "output_schema의 키는 문자열이어야 합니다"
        )

    normalized_values: tuple[object, object, object] | None = None
    normalization_error: str | None = None
    try:
        normalized_values = (
            normalize_json_value(messages),
            normalize_json_value(parameters),
            (
                normalize_json_value(output_schema)
                if output_schema is not None
                else None
            ),
        )
    except InvalidJsonValueError as error:
        normalization_error = str(error)

    if normalization_error is not None:
        raise LlmInputValidationError(normalization_error)
    if normalized_values is None:
        raise RuntimeError("LLM 입력 정규화 결과가 없습니다")

    normalized_messages, normalized_parameters, normalized_output_schema = (
        normalized_values
    )
    return (
        cast(LlmMessages, normalized_messages),
        cast(LlmParameters, normalized_parameters),
        cast(LlmOutputSchema | None, normalized_output_schema),
    )


__all__ = [
    "LlmInputValidationError",
    "normalize_llm_inputs",
]
