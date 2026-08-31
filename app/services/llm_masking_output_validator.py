"""LLM 마스킹 출력이 원문과 Detection을 정확히 보존하는지 검증합니다."""

from __future__ import annotations

import re
from typing import Literal

from app.policies.mask_target_resolver import MaskTarget
from app.schemas.masking import MaskReplacement, MaskResponse
from app.services.llm_masking_output_parser import (
    MaskEntityAssignment,
    ParsedMaskingOutput,
)


LlmMaskingValidationErrorCode = Literal[
    "LLM_MASKING_TARGET_MISMATCH",
    "LLM_MASKING_ENTITY_INVALID",
    "LLM_MASKING_TEXT_MISMATCH",
    "LLM_MASKING_PLACEHOLDER_COLLISION",
]

_NAMESPACE_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_ENTITY_PATTERN = re.compile(r"^entity-([1-9][0-9]{0,4})$")


class LlmMaskingValidationError(ValueError):
    """LLM 마스킹 결과가 보안 불변식을 만족하지 않으면 발생합니다."""

    def __init__(self, code: LlmMaskingValidationErrorCode) -> None:
        self.code = code
        detail = {
            "LLM_MASKING_TARGET_MISMATCH": (
                "LLM이 마스킹 대상을 정확히 한 번씩 처리하지 않았습니다"
            ),
            "LLM_MASKING_ENTITY_INVALID": (
                "LLM의 동일 대상 식별자 순서가 올바르지 않습니다"
            ),
            "LLM_MASKING_TEXT_MISMATCH": (
                "LLM 마스킹 문자열이 원문 보존 규칙과 다릅니다"
            ),
            "LLM_MASKING_PLACEHOLDER_COLLISION": (
                "마스킹 placeholder가 원문과 충돌합니다"
            ),
        }[code]
        super().__init__(f"{code}: {detail}")


class LlmMaskingOutputValidator:
    """서버가 재구성한 문자열과 LLM 출력을 byte-for-byte 비교합니다."""

    def validate(
        self,
        *,
        original_text: str,
        targets: tuple[MaskTarget, ...],
        placeholder_namespace: str,
        output: ParsedMaskingOutput,
    ) -> MaskResponse:
        """coverage, entity 순서, placeholder와 비민감 원문 보존을 검증합니다."""

        if type(original_text) is not str:
            raise TypeError("original_text는 문자열이어야 합니다")
        if type(targets) is not tuple or any(
            type(item) is not MaskTarget for item in targets
        ):
            raise TypeError("targets는 MaskTarget tuple이어야 합니다")
        if (
            type(placeholder_namespace) is not str
            or _NAMESPACE_PATTERN.fullmatch(placeholder_namespace) is None
        ):
            raise ValueError(
                "placeholder_namespace는 16자리 소문자 16진수여야 합니다"
            )
        if type(output) is not ParsedMaskingOutput:
            raise TypeError("output은 ParsedMaskingOutput이어야 합니다")
        if (
            type(output.masked_text) is not str
            or type(output.assignments) is not tuple
        ):
            raise LlmMaskingValidationError(
                "LLM_MASKING_TARGET_MISMATCH"
            )
        try:
            output.masked_text.encode("utf-8")
        except UnicodeEncodeError:
            raise LlmMaskingValidationError(
                "LLM_MASKING_TEXT_MISMATCH"
            ) from None
        if any(
            type(assignment) is not MaskEntityAssignment
            or type(assignment.target_id) is not str
            or type(assignment.entity_id) is not str
            for assignment in output.assignments
        ):
            raise LlmMaskingValidationError(
                "LLM_MASKING_TARGET_MISMATCH"
            )

        if len(output.assignments) != len(targets):
            raise LlmMaskingValidationError(
                "LLM_MASKING_TARGET_MISMATCH"
            )
        if f"[[LPL_{placeholder_namespace}_" in original_text:
            raise LlmMaskingValidationError(
                "LLM_MASKING_PLACEHOLDER_COLLISION"
            )

        entity_ordinals: dict[str, int] = {}
        replacements: list[MaskReplacement] = []
        fragments: list[str] = []
        cursor = 0

        for index, (target, assignment) in enumerate(
            zip(targets, output.assignments, strict=True),
            start=1,
        ):
            if assignment.target_id != target.target_id:
                raise LlmMaskingValidationError(
                    "LLM_MASKING_TARGET_MISMATCH"
                )
            entity_match = _ENTITY_PATTERN.fullmatch(
                assignment.entity_id
            )
            if entity_match is None:
                raise LlmMaskingValidationError(
                    "LLM_MASKING_ENTITY_INVALID"
                )

            ordinal = entity_ordinals.get(assignment.entity_id)
            if ordinal is None:
                ordinal = len(entity_ordinals) + 1
                if int(entity_match.group(1)) != ordinal:
                    raise LlmMaskingValidationError(
                        "LLM_MASKING_ENTITY_INVALID"
                    )
                entity_ordinals[assignment.entity_id] = ordinal

            placeholder = _placeholder(
                placeholder_namespace,
                ordinal,
            )

            if target.start < cursor or target.end > len(original_text):
                raise LlmMaskingValidationError(
                    "LLM_MASKING_TARGET_MISMATCH"
                )
            fragments.append(original_text[cursor : target.start])
            fragments.append(placeholder)
            cursor = target.end
            replacements.append(
                MaskReplacement.model_validate(
                    {
                        "start": target.start,
                        "end": target.end,
                        "entityId": assignment.entity_id,
                        "placeholder": placeholder,
                        "types": list(target.types),
                        "sources": list(target.sources),
                    }
                )
            )

        fragments.append(original_text[cursor:])
        expected_text = "".join(fragments)
        if output.masked_text != expected_text:
            del fragments, expected_text
            raise LlmMaskingValidationError(
                "LLM_MASKING_TEXT_MISMATCH"
            )
        return MaskResponse.model_validate(
            {
                "maskedText": output.masked_text,
                "replacements": replacements,
            }
        )


def _placeholder(namespace: str, ordinal: int) -> str:
    """요청 namespace와 entity 순번으로 충돌하기 어려운 토큰을 만듭니다."""

    return f"[[LPL_{namespace}_{ordinal:04d}]]"


__all__ = [
    "LlmMaskingOutputValidator",
    "LlmMaskingValidationError",
    "LlmMaskingValidationErrorCode",
]
