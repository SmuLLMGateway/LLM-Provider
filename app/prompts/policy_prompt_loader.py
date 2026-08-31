"""고정 정책 Prompt JSON 파일을 제한된 크기로 읽고 파싱합니다."""

from __future__ import annotations

from pathlib import Path

from app.core.json_codec import StrictJsonDecodeError, load_strict_json
from app.prompts.prompt_errors import InvalidPromptTemplateContentError
from app.prompts.prompt_limits import DEFAULT_PROMPT_LIMITS, PromptLimits
from app.prompts.prompt_loader import PROJECT_ROOT, PromptLoader


POLICY_PROMPTS_FILENAME = "policy_prompts.json"
DEFAULT_POLICY_PROMPTS_PATH = (
    PROJECT_ROOT / "config" / POLICY_PROMPTS_FILENAME
)


class PolicyPromptLoader:
    """코드에 고정된 정책 Prompt JSON 파일을 안전하게 읽습니다."""

    def __init__(
        self,
        prompt_path: str | Path = DEFAULT_POLICY_PROMPTS_PATH,
        limits: PromptLimits = DEFAULT_PROMPT_LIMITS,
    ) -> None:
        self.prompt_path = Path(prompt_path).resolve()
        self.limits = limits
        self._text_loader = PromptLoader(self.prompt_path, limits=limits)

    def load(self) -> object:
        """중복 키와 비표준 숫자를 거부한 정책 Prompt 객체를 반환합니다."""

        source = self._text_loader.load()
        try:
            return load_strict_json(
                source,
                max_bytes=self.limits.max_template_bytes,
            )
        except StrictJsonDecodeError:
            raise InvalidPromptTemplateContentError(
                str(self.prompt_path),
                "정책 Prompt 파일이 유효한 JSON 객체가 아닙니다",
            ) from None


__all__ = [
    "DEFAULT_POLICY_PROMPTS_PATH",
    "POLICY_PROMPTS_FILENAME",
    "PolicyPromptLoader",
]
