"""LLM의 후보 판정과 신규 탐지 JSON 출력을 엄격하게 변환합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from app.core.json_codec import StrictJsonDecodeError, load_strict_json
from app.schemas.detection import (
    CandidateDecisionValue,
    CandidateId,
    DetectionType,
)


LlmDetectionOutputErrorCode = Literal[
    "LLM_DETECTION_OUTPUT_INVALID_JSON",
    "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL",
    "LLM_DETECTION_OUTPUT_INVALID_DECISION",
    "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH",
    "LLM_DETECTION_OUTPUT_INVALID_ITEM",
    "LLM_DETECTION_OUTPUT_POLICY_DISABLED",
    "LLM_DETECTION_OUTPUT_TOO_LARGE",
    "LLM_DETECTION_OUTPUT_TOO_MANY_DECISIONS",
    "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS",
    "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS",
    "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND",
]

DEFAULT_MAX_LLM_DETECTION_OUTPUT_BYTES = 1_048_576
DEFAULT_MAX_LLM_CANDIDATE_DECISIONS = 20_000
DEFAULT_MAX_LLM_DETECTIONS = 1_000
_TOP_LEVEL_FIELDS = frozenset({"candidateDecisions", "newDetections"})
_DECISION_FIELDS = frozenset({"candidateId", "decision"})
_DETECTION_FIELDS = frozenset({"text", "type", "score"})


class _LlmOutputModel(BaseModel):
    """LLM 출력 항목의 추가 필드와 느슨한 타입 변환을 거부합니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
    )


class LlmCandidateDecision(_LlmOutputModel):
    """LLM이 후보 ID에 대해 반환하는 최소 판정입니다."""

    candidate_id: CandidateId = Field(alias="candidateId")
    decision: CandidateDecisionValue


class LlmDetectionCandidate(_LlmOutputModel):
    """LLM이 새로 찾고 서버가 원문 좌표로 변환할 탐지 후보입니다."""

    text: Annotated[str, StringConstraints(min_length=1)]
    type: DetectionType
    score: Annotated[float, Field(ge=0.0, le=1.0)]


@dataclass(frozen=True, slots=True)
class LlmDetectionOutput:
    """검증된 후보 판정과 좌표 없는 신규 탐지의 불변 묶음입니다."""

    candidate_decisions: tuple[LlmCandidateDecision, ...]
    new_detections: tuple[LlmDetectionCandidate, ...]


class LlmDetectionOutputError(ValueError):
    """LLM 탐지 출력이 안전한 계약으로 변환되지 않으면 발생합니다."""

    def __init__(
        self,
        code: LlmDetectionOutputErrorCode,
        *,
        item_index: int | None = None,
        actual_bytes: int | None = None,
        max_output_bytes: int | None = None,
        actual_items: int | None = None,
        max_detections: int | None = None,
    ) -> None:
        self.code = code
        self.item_index = item_index
        self.actual_bytes = actual_bytes
        self.max_output_bytes = max_output_bytes
        self.actual_items = actual_items
        self.max_detections = max_detections

        detail = {
            "LLM_DETECTION_OUTPUT_INVALID_JSON": (
                "LLM 탐지 출력이 유효한 JSON이 아닙니다"
            ),
            "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL": (
                "LLM 탐지 출력은 candidateDecisions와 newDetections만 "
                "가진 객체여야 합니다"
            ),
            "LLM_DETECTION_OUTPUT_INVALID_DECISION": (
                "LLM 후보 판정 항목이 응답 계약과 다릅니다"
            ),
            "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH": (
                "LLM 후보 판정이 요청 후보 목록과 일치하지 않습니다"
            ),
            "LLM_DETECTION_OUTPUT_INVALID_ITEM": (
                "LLM 신규 탐지 항목이 후보 계약과 다릅니다"
            ),
            "LLM_DETECTION_OUTPUT_POLICY_DISABLED": (
                "LLM 탐지 출력이 비활성 정책을 사용했습니다"
            ),
            "LLM_DETECTION_OUTPUT_TOO_LARGE": (
                "LLM 탐지 출력이 허용 크기를 초과했습니다"
            ),
            "LLM_DETECTION_OUTPUT_TOO_MANY_DECISIONS": (
                "LLM 후보 판정 수가 제한을 초과했습니다"
            ),
            "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS": (
                "LLM 신규 탐지 수가 제한을 초과했습니다"
            ),
            "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND": (
                "LLM 탐지 문자열이 원문에 존재하지 않습니다"
            ),
            "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS": (
                "LLM 문맥형 탐지 문자열이 원문에서 고유하지 않습니다"
            ),
        }[code]
        if item_index is not None:
            detail = f"{detail}: index={item_index}"
        super().__init__(f"{code}: {detail}")


class LlmDetectionOutputParser:
    """후보 판정과 신규 탐지를 가진 순수 JSON 객체만 허용합니다."""

    __slots__ = (
        "_max_candidate_decisions",
        "_max_detections",
        "_max_output_bytes",
    )

    def __init__(
        self,
        *,
        max_output_bytes: int = DEFAULT_MAX_LLM_DETECTION_OUTPUT_BYTES,
        max_candidate_decisions: int = (
            DEFAULT_MAX_LLM_CANDIDATE_DECISIONS
        ),
        max_detections: int = DEFAULT_MAX_LLM_DETECTIONS,
    ) -> None:
        self._max_output_bytes = _require_positive_integer(
            max_output_bytes,
            field_name="max_output_bytes",
        )
        self._max_candidate_decisions = _require_positive_integer(
            max_candidate_decisions,
            field_name="max_candidate_decisions",
        )
        self._max_detections = _require_positive_integer(
            max_detections,
            field_name="max_detections",
        )

    @property
    def max_output_bytes(self) -> int:
        """허용하는 LLM 출력의 최대 UTF-8 byte 크기를 반환합니다."""

        return self._max_output_bytes

    @property
    def max_candidate_decisions(self) -> int:
        """한 출력에서 허용하는 후보 판정 수를 반환합니다."""

        return self._max_candidate_decisions

    @property
    def max_detections(self) -> int:
        """한 출력에서 허용하는 신규 탐지 수를 반환합니다."""

        return self._max_detections

    def parse(self, output: str) -> LlmDetectionOutput:
        """LLM 문자열을 검증하고 불변 출력 객체로 변환합니다."""

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

        if decode_error is not None:
            del output
            if decode_error.code == "TOO_LARGE":
                raise LlmDetectionOutputError(
                    "LLM_DETECTION_OUTPUT_TOO_LARGE",
                    actual_bytes=decode_error.actual_bytes,
                    max_output_bytes=decode_error.max_bytes,
                )
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_JSON"
            )

        del output
        if type(parsed) is not dict or parsed.keys() != _TOP_LEVEL_FIELDS:
            del parsed
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL"
            )

        decision_items = parsed["candidateDecisions"]
        detection_items = parsed["newDetections"]
        if type(decision_items) is not list or type(detection_items) is not list:
            del parsed, decision_items, detection_items
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL"
            )

        if len(decision_items) > self._max_candidate_decisions:
            actual_items = len(decision_items)
            del parsed, decision_items, detection_items
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_TOO_MANY_DECISIONS",
                actual_items=actual_items,
                max_detections=self._max_candidate_decisions,
            )
        if len(detection_items) > self._max_detections:
            actual_items = len(detection_items)
            del parsed, decision_items, detection_items
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS",
                actual_items=actual_items,
                max_detections=self._max_detections,
            )

        decisions = tuple(
            self._parse_decision(item, index=index)
            for index, item in enumerate(decision_items)
        )
        detections = tuple(
            self._parse_detection(item, index=index)
            for index, item in enumerate(detection_items)
        )
        del parsed, decision_items, detection_items
        return LlmDetectionOutput(
            candidate_decisions=decisions,
            new_detections=detections,
        )

    @staticmethod
    def _parse_decision(
        item: object,
        *,
        index: int,
    ) -> LlmCandidateDecision:
        """후보 판정 하나를 정확한 두 필드로 검증합니다."""

        if type(item) is not dict or item.keys() != _DECISION_FIELDS:
            del item
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_DECISION",
                item_index=index,
            )

        data = dict(item)
        try:
            decision = LlmCandidateDecision.model_validate(data)
            decision.candidate_id.encode("utf-8")
        except (ValidationError, UnicodeEncodeError):
            del item, data
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_DECISION",
                item_index=index,
            ) from None
        return decision

    @staticmethod
    def _parse_detection(
        item: object,
        *,
        index: int,
    ) -> LlmDetectionCandidate:
        """신규 탐지 하나를 좌표 없는 정확한 세 필드로 검증합니다."""

        if type(item) is not dict or item.keys() != _DETECTION_FIELDS:
            del item
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_ITEM",
                item_index=index,
            )

        data = dict(item)
        try:
            candidate = LlmDetectionCandidate.model_validate(data)
            candidate.text.encode("utf-8")
            candidate.type.encode("utf-8")
        except (ValidationError, UnicodeEncodeError):
            del item, data
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_ITEM",
                item_index=index,
            ) from None
        return candidate


def _require_positive_integer(value: int, *, field_name: str) -> int:
    """Parser 자원 제한값이 1 이상의 정확한 정수인지 검증합니다."""

    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name}는 1 이상의 정수여야 합니다")
    return value


__all__ = [
    "DEFAULT_MAX_LLM_CANDIDATE_DECISIONS",
    "DEFAULT_MAX_LLM_DETECTION_OUTPUT_BYTES",
    "DEFAULT_MAX_LLM_DETECTIONS",
    "LlmCandidateDecision",
    "LlmDetectionCandidate",
    "LlmDetectionOutput",
    "LlmDetectionOutputError",
    "LlmDetectionOutputErrorCode",
    "LlmDetectionOutputParser",
]
