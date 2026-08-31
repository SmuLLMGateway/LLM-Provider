"""프롬프트 로드, 검증과 렌더링에 적용할 자원 제한을 정의합니다."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptLimits:
    """고정 Prompt 처리에 적용되는 UTF-8 byte와 시간 제한입니다."""

    max_template_bytes: int = 256 * 1024
    max_context_bytes: int = 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    render_timeout_ms: int = 1_000

    def __post_init__(self) -> None:
        """모든 제한값이 양의 정수인지 검증합니다."""

        for field_name in (
            "max_template_bytes",
            "max_context_bytes",
            "max_output_bytes",
            "render_timeout_ms",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(
                    f"{field_name}는 1 이상의 정수여야 합니다"
                )


DEFAULT_PROMPT_LIMITS = PromptLimits()


__all__ = [
    "DEFAULT_PROMPT_LIMITS",
    "PromptLimits",
]
