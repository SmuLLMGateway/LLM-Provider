"""LLM Backend 결과를 비신뢰 값으로 보고 공통 계약으로 재검증합니다."""

from __future__ import annotations

from pydantic import ValidationError

from app.schemas.generation import LlmResult, LlmTokenUsage


class LlmResultValidationError(TypeError):
    """Backend 결과를 안전한 LlmResult로 확인할 수 없으면 발생합니다."""

    def __init__(self, *, actual_type: type[object]) -> None:
        self.actual_type = actual_type
        super().__init__(
            "LLM Backend 결과는 검증된 LlmResult여야 합니다: "
            f"actual={actual_type.__name__}"
        )


def validate_llm_result(result: object) -> LlmResult:
    """검증 우회로 생성된 Pydantic 인스턴스까지 Plain Data로 재검증합니다."""

    actual_type = type(result)
    if actual_type is not LlmResult:
        del result
        raise LlmResultValidationError(
            actual_type=actual_type,
        )

    validation_failed = False
    invalid_type = actual_type
    try:
        text = result.text
        model_name = result.model_name
        finish_reason = result.finish_reason
        usage = result.usage
        if type(text) is not str:
            validation_failed = True
            invalid_type = type(text)
        elif model_name is not None and type(model_name) is not str:
            validation_failed = True
            invalid_type = type(model_name)
        elif (
            finish_reason is not None
            and type(finish_reason) is not str
        ):
            validation_failed = True
            invalid_type = type(finish_reason)

        usage_data: dict[str, object] | None = None
        if not validation_failed and usage is not None:
            if type(usage) is not LlmTokenUsage:
                validation_failed = True
                invalid_type = type(usage)
            else:
                usage_data = {
                    "inputTokens": usage.input_tokens,
                    "outputTokens": usage.output_tokens,
                    "totalTokens": usage.total_tokens,
                }

        if not validation_failed:
            normalized = LlmResult.model_validate(
                {
                    "text": text,
                    "modelName": model_name,
                    "finishReason": finish_reason,
                    "usage": usage_data,
                }
            )
            normalized.text.encode("utf-8")
            if normalized.model_name is not None:
                normalized.model_name.encode("utf-8")
            if normalized.finish_reason is not None:
                normalized.finish_reason.encode("utf-8")
    except (
        AttributeError,
        UnicodeEncodeError,
        ValidationError,
    ):
        validation_failed = True

    if validation_failed:
        del result
        raise LlmResultValidationError(
            actual_type=invalid_type,
        ) from None
    return result


__all__ = [
    "LlmResultValidationError",
    "validate_llm_result",
]
