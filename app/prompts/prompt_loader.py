"""config의 Jinja2 프롬프트 템플릿을 안전하게 읽습니다."""

from __future__ import annotations

import os
from pathlib import Path

from app.prompts.prompt_errors import (
    PromptTemplateDecodeError,
    PromptTemplateNotFoundError,
    PromptTemplateReadError,
    PromptTemplateTooLargeError,
)
from app.prompts.prompt_limits import (
    DEFAULT_PROMPT_LIMITS,
    PromptLimits,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_FILENAME = "prompts.j2"
MASK_PROMPT_FILENAME = "mask_prompt.j2"
TITLE_PROMPT_FILENAME = "title_prompt.j2"
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "config" / PROMPT_FILENAME
DEFAULT_MASK_PROMPT_PATH = PROJECT_ROOT / "config" / MASK_PROMPT_FILENAME
DEFAULT_TITLE_PROMPT_PATH = (
    PROJECT_ROOT / "config" / TITLE_PROMPT_FILENAME
)


class PromptLoader:
    """코드에 고정된 단일 Prompt 파일을 제한된 크기로 읽습니다."""

    def __init__(
        self,
        prompt_path: str | Path = DEFAULT_PROMPT_PATH,
        limits: PromptLimits = DEFAULT_PROMPT_LIMITS,
    ) -> None:
        self.prompt_path = Path(prompt_path).resolve()
        self.limits = limits

    def load(self) -> str:
        """고정 Jinja2 Prompt 파일의 정규화된 UTF-8 본문을 반환합니다."""

        if not self.prompt_path.is_file():
            raise PromptTemplateNotFoundError(
                str(self.prompt_path),
                "고정 Prompt 파일을 찾을 수 없습니다: "
                f"{self.prompt_path}",
            )

        try:
            with self.prompt_path.open("rb") as template_file:
                file_size = os.fstat(template_file.fileno()).st_size
                if file_size > self.limits.max_template_bytes:
                    raise PromptTemplateTooLargeError(
                        str(self.prompt_path),
                        file_size,
                        self.limits.max_template_bytes,
                    )
                encoded_source = template_file.read(
                    self.limits.max_template_bytes + 1
                )
        except OSError as error:
            raise PromptTemplateReadError(
                str(self.prompt_path),
                f"고정 Prompt를 읽을 수 없습니다: {self.prompt_path}",
            ) from error

        if len(encoded_source) > self.limits.max_template_bytes:
            raise PromptTemplateTooLargeError(
                str(self.prompt_path),
                max(file_size, len(encoded_source)),
                self.limits.max_template_bytes,
            )

        try:
            decoded_source = encoded_source.decode("utf-8")
            return decoded_source.replace("\r\n", "\n").replace(
                "\r",
                "\n",
            )
        except UnicodeDecodeError as error:
            raise PromptTemplateDecodeError(
                str(self.prompt_path),
                "고정 Prompt가 UTF-8 형식이 아닙니다: "
                f"{self.prompt_path}",
            ) from error


__all__ = [
    "DEFAULT_PROMPT_PATH",
    "DEFAULT_MASK_PROMPT_PATH",
    "DEFAULT_TITLE_PROMPT_PATH",
    "MASK_PROMPT_FILENAME",
    "PROMPT_FILENAME",
    "PromptLoader",
    "PromptTemplateDecodeError",
    "PromptTemplateNotFoundError",
    "PromptTemplateReadError",
    "PromptTemplateTooLargeError",
    "TITLE_PROMPT_FILENAME",
]
