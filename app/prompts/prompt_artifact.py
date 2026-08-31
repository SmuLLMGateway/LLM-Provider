"""검증과 컴파일을 마친 Prompt 런타임 Artifact를 정의합니다."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256

from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptOutputTooLargeError,
    PromptTemplateTooLargeError,
    PromptVariableContractError,
)
from app.prompts.policy_prompt_catalog import PolicyPromptCatalog
from app.prompts.prompt_renderer import (
    CompiledPromptTemplate,
    PromptRenderer,
)
from app.schemas.detection import PolicyId


FIXED_PROMPT_VARIABLES = frozenset(
    {"text", "existing_detections"}
)


@dataclass(frozen=True, slots=True)
class PromptArtifact:
    """고정 탐지 Prompt의 검증·컴파일 결과를 보관합니다."""

    compiled_template: CompiledPromptTemplate = field(repr=False)
    content_hash: str
    policy_prompts: PolicyPromptCatalog = field(repr=False)

    def __post_init__(self) -> None:
        """Artifact의 Handle, 해시와 고정 변수 계약을 검증합니다."""

        if not isinstance(self.compiled_template, CompiledPromptTemplate):
            raise TypeError(
                "compiled_template은 CompiledPromptTemplate이어야 합니다."
            )
        if (
            self.compiled_template.referenced_variables
            != FIXED_PROMPT_VARIABLES
        ):
            raise ValueError(
                "고정 Prompt는 text와 existing_detections만 "
                "참조해야 합니다"
            )
        if (
            type(self.content_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is None
        ):
            raise ValueError("content_hash는 SHA-256 16진수 문자열이어야 합니다")
        if not isinstance(self.policy_prompts, PolicyPromptCatalog):
            raise TypeError("policy_prompts는 PolicyPromptCatalog여야 합니다")

    @classmethod
    def compile(
        cls,
        source: object,
        *,
        renderer: PromptRenderer,
        template_path: str,
        policy_prompts: PolicyPromptCatalog | None = None,
    ) -> PromptArtifact:
        """고정 본문과 변수 계약을 검증하고 한 번만 컴파일합니다."""

        if not isinstance(source, str) or not source.strip():
            raise InvalidPromptTemplateContentError(
                template_path,
                f"고정 Prompt 본문은 비어 있을 수 없습니다: {template_path}",
            )
        try:
            encoded_source = source.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidPromptTemplateContentError(
                template_path,
                f"고정 Prompt를 UTF-8로 표현할 수 없습니다: {template_path}",
            ) from error
        if len(encoded_source) > renderer.limits.max_template_bytes:
            raise PromptTemplateTooLargeError(
                template_path,
                len(encoded_source),
                renderer.limits.max_template_bytes,
            )

        compiled_template = renderer.compile_template(source)
        referenced_variables = compiled_template.referenced_variables
        if referenced_variables != FIXED_PROMPT_VARIABLES:
            raise PromptVariableContractError(
                template_path,
                required_variables=FIXED_PROMPT_VARIABLES,
                actual_variables=referenced_variables,
            )
        return cls(
            compiled_template=compiled_template,
            content_hash=sha256(encoded_source).hexdigest(),
            policy_prompts=(
                policy_prompts
                if policy_prompts is not None
                else PolicyPromptCatalog.empty(
                    max_output_bytes=renderer.limits.max_output_bytes,
                )
            ),
        )

    def render(
        self,
        *,
        text: str,
        existing_detections: str,
        policy_ids: tuple[PolicyId, ...] = (),
    ) -> str:
        """기본 Prompt와 선택한 정책별 지침을 제한 안에서 조립합니다."""

        rendered = self.compiled_template.render(
            text=text,
            existing_detections=existing_detections,
        )
        policy_block = self.policy_prompts.assemble(policy_ids)
        if not policy_block:
            return rendered
        combined = rendered.rstrip() + "\n\n" + policy_block + "\n"
        actual_bytes = len(combined.encode("utf-8"))
        if actual_bytes > self.policy_prompts.max_output_bytes:
            raise PromptOutputTooLargeError(
                actual_bytes,
                self.policy_prompts.max_output_bytes,
            )
        return combined


__all__ = [
    "FIXED_PROMPT_VARIABLES",
    "PromptArtifact",
]
