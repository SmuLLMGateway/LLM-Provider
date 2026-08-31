"""제목 생성용 고정 Prompt의 컴파일 결과를 정의합니다."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256

from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptTemplateTooLargeError,
    PromptVariableContractError,
)
from app.prompts.prompt_renderer import (
    CompiledPromptTemplate,
    PromptRenderer,
)


TITLE_PROMPT_VARIABLES: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TitlePromptArtifact:
    """외부 변수가 없는 제목 생성 System Prompt를 보관합니다."""

    compiled_template: CompiledPromptTemplate = field(repr=False)
    content_hash: str

    def __post_init__(self) -> None:
        """컴파일 Handle, 변수 계약과 본문 해시를 다시 검증합니다."""

        if not isinstance(self.compiled_template, CompiledPromptTemplate):
            raise TypeError(
                "compiled_template은 CompiledPromptTemplate이어야 합니다."
            )
        if (
            self.compiled_template.referenced_variables
            != TITLE_PROMPT_VARIABLES
        ):
            raise ValueError(
                "제목 생성 Prompt는 외부 변수를 참조할 수 없습니다"
            )
        if (
            type(self.content_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is None
        ):
            raise ValueError("content_hash는 SHA-256 16진수 문자열이어야 합니다")

    @classmethod
    def compile(
        cls,
        source: object,
        *,
        renderer: PromptRenderer,
        template_path: str,
    ) -> TitlePromptArtifact:
        """정적 본문과 무변수 계약을 검증하고 한 번만 컴파일합니다."""

        if not isinstance(source, str) or not source.strip():
            raise InvalidPromptTemplateContentError(
                template_path,
                "제목 생성 Prompt 본문은 비어 있을 수 없습니다: "
                f"{template_path}",
            )
        try:
            encoded_source = source.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidPromptTemplateContentError(
                template_path,
                "제목 생성 Prompt를 UTF-8로 표현할 수 없습니다: "
                f"{template_path}",
            ) from error
        if len(encoded_source) > renderer.limits.max_template_bytes:
            raise PromptTemplateTooLargeError(
                template_path,
                len(encoded_source),
                renderer.limits.max_template_bytes,
            )

        compiled_template = renderer.compile_template(source)
        referenced_variables = compiled_template.referenced_variables
        if referenced_variables != TITLE_PROMPT_VARIABLES:
            raise PromptVariableContractError(
                template_path,
                required_variables=TITLE_PROMPT_VARIABLES,
                actual_variables=referenced_variables,
            )
        return cls(
            compiled_template=compiled_template,
            content_hash=sha256(encoded_source).hexdigest(),
        )

    def render(self) -> str:
        """외부 입력 없이 고정된 제목 생성 System Prompt를 렌더링합니다."""

        return self.compiled_template.render_static()


__all__ = [
    "TITLE_PROMPT_VARIABLES",
    "TitlePromptArtifact",
]
