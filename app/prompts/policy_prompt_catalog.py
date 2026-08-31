"""정책별 고정 탐지 지침의 검증, 선택과 조립을 담당합니다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

from app.core.json_codec import dump_canonical_json_utf8
from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptOutputTooLargeError,
)
from app.schemas.detection import ALLOWED_POLICY_IDS, PolicyId


# Regex와 NER가 놓친 항목을 보완할 수 있도록 모든 활성 정책을 직접 검사합니다.
COMMON_LLM_POLICY_IDS: tuple[PolicyId, ...] = ALLOWED_POLICY_IDS
_JINJA_MARKERS = ("{{", "{%", "{#")


@dataclass(frozen=True, slots=True)
class PolicyPromptCatalog:
    """검증된 전체 정책 지침을 불변 상태로 보관합니다."""

    prompts: Mapping[PolicyId, str]
    content_hash: str
    max_output_bytes: int

    def __post_init__(self) -> None:
        """정책 맵과 해시, 조립 결과 제한을 방어적으로 검증합니다."""

        if type(self.max_output_bytes) is not int or self.max_output_bytes < 1:
            raise ValueError("max_output_bytes는 1 이상의 정수여야 합니다")
        normalized = _validate_policy_prompt_mapping(
            self.prompts,
            source_path="<policy-prompt-catalog>",
            require_all=False,
        )
        if (
            type(self.content_hash) is not str
            or len(self.content_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.content_hash
            )
        ):
            raise ValueError("content_hash는 SHA-256 16진수 문자열이어야 합니다")
        expected_hash = sha256(
            dump_canonical_json_utf8(normalized)
        ).hexdigest()
        if self.content_hash != expected_hash:
            raise ValueError("content_hash가 정책 Prompt 내용과 일치하지 않습니다")
        object.__setattr__(self, "prompts", MappingProxyType(normalized))

    @classmethod
    def compile(
        cls,
        sources: object,
        *,
        source_path: str,
        max_output_bytes: int,
    ) -> PolicyPromptCatalog:
        """정확한 전체 정책 본문을 검증하고 불변 Catalog를 만듭니다."""

        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes는 1 이상의 정수여야 합니다")
        normalized = _validate_policy_prompt_mapping(
            sources,
            source_path=source_path,
            require_all=True,
        )
        encoded = dump_canonical_json_utf8(normalized)
        if len(encoded) > max_output_bytes:
            raise PromptOutputTooLargeError(len(encoded), max_output_bytes)
        return cls(
            prompts=normalized,
            content_hash=sha256(encoded).hexdigest(),
            max_output_bytes=max_output_bytes,
        )

    @classmethod
    def empty(cls, *, max_output_bytes: int) -> PolicyPromptCatalog:
        """독립 Artifact 테스트용 빈 Catalog를 만듭니다."""

        encoded = dump_canonical_json_utf8({})
        return cls(
            prompts={},
            content_hash=sha256(encoded).hexdigest(),
            max_output_bytes=max_output_bytes,
        )

    def select_policy_ids(
        self,
        *,
        enabled_policy_ids: tuple[PolicyId, ...],
        candidate_policy_ids: tuple[PolicyId, ...],
    ) -> tuple[PolicyId, ...]:
        """후보 유무와 관계없이 모든 활성 정책을 고정 순서로 반환합니다."""

        enabled = frozenset(enabled_policy_ids)
        candidates = frozenset(candidate_policy_ids)
        selected = candidates | frozenset(COMMON_LLM_POLICY_IDS)
        return tuple(
            policy_id
            for policy_id in ALLOWED_POLICY_IDS
            if policy_id in enabled and policy_id in selected
        )

    def assemble(self, policy_ids: tuple[PolicyId, ...]) -> str:
        """선택된 정책 지침을 한 번씩 고정 순서의 Prompt 블록으로 조립합니다."""

        if (
            type(policy_ids) is not tuple
            or len(set(policy_ids)) != len(policy_ids)
        ):
            raise TypeError("policy_ids는 중복 없는 tuple이어야 합니다")
        selected = frozenset(policy_ids)
        ordered = tuple(
            policy_id
            for policy_id in ALLOWED_POLICY_IDS
            if policy_id in selected
        )
        if len(ordered) != len(policy_ids):
            raise ValueError("policy_ids에 지원하지 않는 정책이 있습니다")
        missing = tuple(
            policy_id for policy_id in ordered if policy_id not in self.prompts
        )
        if missing:
            raise ValueError(
                "선택한 정책의 고정 Prompt가 없습니다: "
                + ", ".join(missing)
            )
        if not ordered:
            return ""

        sections = ["<policy_instructions>"]
        for policy_id in ordered:
            sections.extend(
                (
                    f"[Policy {policy_id}]",
                    self.prompts[policy_id],
                )
            )
        sections.append("</policy_instructions>")
        assembled = "\n".join(sections)
        actual_bytes = len(assembled.encode("utf-8"))
        if actual_bytes > self.max_output_bytes:
            raise PromptOutputTooLargeError(
                actual_bytes,
                self.max_output_bytes,
            )
        return assembled


def _validate_policy_prompt_mapping(
    sources: object,
    *,
    source_path: str,
    require_all: bool,
) -> dict[PolicyId, str]:
    """정책 Prompt 객체의 키·본문·UTF-8·템플릿 금지 규칙을 검증합니다."""

    if type(sources) is not dict and not isinstance(sources, Mapping):
        raise InvalidPromptTemplateContentError(
            source_path,
            "정책 Prompt는 Policy ID를 키로 가진 객체여야 합니다",
        )
    raw_mapping = dict(sources)
    expected = frozenset(ALLOWED_POLICY_IDS)
    actual = frozenset(raw_mapping)
    if (require_all and actual != expected) or not actual <= expected:
        raise InvalidPromptTemplateContentError(
            source_path,
            "정책 Prompt 키는 정의된 Policy ID와 정확히 일치해야 합니다",
        )

    normalized: dict[PolicyId, str] = {}
    for policy_id in ALLOWED_POLICY_IDS:
        if policy_id not in raw_mapping:
            continue
        source = raw_mapping[policy_id]
        if type(source) is not str or not source.strip():
            raise InvalidPromptTemplateContentError(
                source_path,
                f"정책 Prompt 본문은 비어 있을 수 없습니다: {policy_id}",
            )
        normalized_source = (
            source.replace("\r\n", "\n").replace("\r", "\n").strip()
        )
        try:
            normalized_source.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidPromptTemplateContentError(
                source_path,
                f"정책 Prompt는 UTF-8 문자열이어야 합니다: {policy_id}",
            ) from None
        if any(marker in normalized_source for marker in _JINJA_MARKERS):
            raise InvalidPromptTemplateContentError(
                source_path,
                f"정책 Prompt에는 Jinja 구문을 사용할 수 없습니다: {policy_id}",
            )
        normalized[policy_id] = normalized_source
    return normalized


__all__ = [
    "COMMON_LLM_POLICY_IDS",
    "PolicyPromptCatalog",
]
