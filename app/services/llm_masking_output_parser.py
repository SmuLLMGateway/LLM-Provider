"""비신뢰 LLM 마스킹 JSON 출력을 제한된 내부 계약으로 파싱합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.json_codec import StrictJsonDecodeError, load_strict_json


DEFAULT_MAX_LLM_MASKING_OUTPUT_BYTES = 32_768
DEFAULT_MAX_MASK_ASSIGNMENTS = 128

LlmMaskingOutputErrorCode = Literal[
    "LLM_MASKING_OUTPUT_INVALID_JSON",
    "LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL",
    "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT",
    "LLM_MASKING_OUTPUT_TOO_LARGE",
    "LLM_MASKING_OUTPUT_TOO_MANY_ASSIGNMENTS",
]


class LlmMaskingOutputError(ValueError):
    """LLM 마스킹 출력을 안전한 내부 구조로 변환할 수 없으면 발생합니다."""

    def __init__(
        self,
        code: LlmMaskingOutputErrorCode,
        *,
        item_index: int | None = None,
        actual_bytes: int | None = None,
        max_output_bytes: int | None = None,
        actual_items: int | None = None,
        max_assignments: int | None = None,
    ) -> None:
        self.code = code
        self.item_index = item_index
        self.actual_bytes = actual_bytes
        self.max_output_bytes = max_output_bytes
        self.actual_items = actual_items
        self.max_assignments = max_assignments
        detail = {
            "LLM_MASKING_OUTPUT_INVALID_JSON": (
                "LLM 마스킹 출력이 유효한 JSON이 아닙니다"
            ),
            "LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL": (
                "LLM 마스킹 출력의 최상위 계약이 올바르지 않습니다"
            ),
            "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT": (
                "LLM 마스킹 대상 할당 항목이 올바르지 않습니다"
            ),
            "LLM_MASKING_OUTPUT_TOO_LARGE": (
                "LLM 마스킹 출력이 허용 크기를 초과했습니다"
            ),
            "LLM_MASKING_OUTPUT_TOO_MANY_ASSIGNMENTS": (
                "LLM 마스킹 대상 할당 수가 허용 한도를 초과했습니다"
            ),
        }[code]
        if item_index is not None:
            detail = f"{detail}: index={item_index}"
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class MaskEntityAssignment:
    """한 서버 생성 target ID를 LLM이 판단한 동일 대상 ID에 연결합니다."""

    target_id: str
    entity_id: str


@dataclass(frozen=True, slots=True)
class ParsedMaskingOutput:
    """문법과 필드 형식 검증을 통과한 LLM 마스킹 출력입니다."""

    masked_text: str
    assignments: tuple[MaskEntityAssignment, ...]


class LlmMaskingOutputParser:
    """순수 JSON 객체와 정확한 필드만 허용합니다."""

    __slots__ = ("_max_assignments", "_max_output_bytes")

    def __init__(
        self,
        *,
        max_output_bytes: int = DEFAULT_MAX_LLM_MASKING_OUTPUT_BYTES,
        max_assignments: int = DEFAULT_MAX_MASK_ASSIGNMENTS,
    ) -> None:
        self._max_output_bytes = _require_positive_integer(
            max_output_bytes,
            field_name="max_output_bytes",
        )
        self._max_assignments = _require_positive_integer(
            max_assignments,
            field_name="max_assignments",
        )

    @property
    def max_output_bytes(self) -> int:
        """허용하는 LLM 출력의 최대 UTF-8 byte 크기입니다."""

        return self._max_output_bytes

    @property
    def max_assignments(self) -> int:
        """한 출력에서 허용하는 최대 target 할당 수입니다."""

        return self._max_assignments

    def parse(self, output: str) -> ParsedMaskingOutput:
        """LLM 문자열을 엄격히 검증한 불변 출력으로 변환합니다."""

        if type(output) is not str:
            raise TypeError("output은 문자열이어야 합니다")

        parsed: object = None
        decode_error: StrictJsonDecodeError | None = None
        try:
            parsed = load_strict_json(
                output,
                max_bytes=self._max_output_bytes,
            )
        except StrictJsonDecodeError as error:
            decode_error = error

        del output
        if decode_error is not None:
            if decode_error.code == "TOO_LARGE":
                raise LlmMaskingOutputError(
                    "LLM_MASKING_OUTPUT_TOO_LARGE",
                    actual_bytes=decode_error.actual_bytes,
                    max_output_bytes=decode_error.max_bytes,
                )
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_JSON"
            )

        if type(parsed) is not dict or set(parsed) != {
            "maskedText",
            "assignments",
        }:
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL"
            )
        masked_text = parsed.get("maskedText")
        assignments = parsed.get("assignments")
        if type(masked_text) is not str or type(assignments) is not list:
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL"
            )
        try:
            masked_text.encode("utf-8")
        except UnicodeEncodeError:
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL"
            ) from None

        if len(assignments) > self._max_assignments:
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_TOO_MANY_ASSIGNMENTS",
                actual_items=len(assignments),
                max_assignments=self._max_assignments,
            )

        normalized = tuple(
            self._parse_assignment(item, index=index)
            for index, item in enumerate(assignments)
        )
        return ParsedMaskingOutput(
            masked_text=masked_text,
            assignments=normalized,
        )

    @staticmethod
    def _parse_assignment(
        item: object,
        *,
        index: int,
    ) -> MaskEntityAssignment:
        """target 할당 하나의 정확한 키, 타입과 UTF-8을 확인합니다."""

        if type(item) is not dict or set(item) != {
            "targetId",
            "entityId",
        }:
            del item
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT",
                item_index=index,
            )
        target_id = item.get("targetId")
        entity_id = item.get("entityId")
        if (
            type(target_id) is not str
            or type(entity_id) is not str
            or not target_id
            or not entity_id
        ):
            del item, target_id, entity_id
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT",
                item_index=index,
            )
        try:
            target_id.encode("utf-8")
            entity_id.encode("utf-8")
        except UnicodeEncodeError:
            del item, target_id, entity_id
            raise LlmMaskingOutputError(
                "LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT",
                item_index=index,
            ) from None
        return MaskEntityAssignment(
            target_id=target_id,
            entity_id=entity_id,
        )


def _require_positive_integer(value: int, *, field_name: str) -> int:
    """Parser 제한값에 bool이 아닌 1 이상의 정수만 허용합니다."""

    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name}는 1 이상의 정수여야 합니다")
    return value


__all__ = [
    "DEFAULT_MAX_LLM_MASKING_OUTPUT_BYTES",
    "DEFAULT_MAX_MASK_ASSIGNMENTS",
    "LlmMaskingOutputError",
    "LlmMaskingOutputErrorCode",
    "LlmMaskingOutputParser",
    "MaskEntityAssignment",
    "ParsedMaskingOutput",
]
