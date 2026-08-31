"""좌표 없는 LLM 탐지 후보를 원문의 정확한 Detection으로 변환합니다."""

from __future__ import annotations

from pydantic import ValidationError

from app.schemas.detection import (
    Detection,
    MULTI_OCCURRENCE_DETECTION_TYPES,
    policy_id_for_detection_type,
)
from app.services.llm_detection_output_parser import (
    LlmDetectionCandidate,
    LlmDetectionOutputError,
)


class LlmDetectionSpanResolver:
    """타입 책임에 따라 LLM 후보의 원문 위치를 안전하게 계산합니다."""

    __slots__ = ("_max_detections",)

    def __init__(self, *, max_detections: int) -> None:
        if type(max_detections) is not int or max_detections < 1:
            raise ValueError("max_detections는 1 이상의 정수여야 합니다")
        self._max_detections = max_detections

    def resolve(
        self,
        original_text: str,
        candidates: object,
    ) -> tuple[Detection, ...]:
        """개체형은 모든 위치로, 문맥형은 고유한 한 위치로 변환합니다."""

        if type(original_text) is not str:
            raise TypeError("original_text는 문자열이어야 합니다")
        if type(candidates) is not tuple:
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_ITEM"
            )

        resolved: list[Detection] = []
        for index, raw_candidate in enumerate(candidates):
            candidate = _revalidate_candidate(raw_candidate, index=index)
            start = original_text.find(candidate.text)
            if start < 0:
                del (
                    candidate,
                    raw_candidate,
                    candidates,
                    original_text,
                    resolved,
                )
                raise LlmDetectionOutputError(
                    "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND",
                    item_index=index,
                )

            next_start = original_text.find(candidate.text, start + 1)
            if (
                candidate.type not in MULTI_OCCURRENCE_DETECTION_TYPES
                and next_start >= 0
            ):
                del (
                    candidate,
                    raw_candidate,
                    candidates,
                    original_text,
                    resolved,
                )
                raise LlmDetectionOutputError(
                    "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS",
                    item_index=index,
                )

            while start >= 0:
                actual_items = len(resolved) + 1
                if actual_items > self._max_detections:
                    del (
                        candidate,
                        raw_candidate,
                        candidates,
                        original_text,
                        resolved,
                    )
                    raise LlmDetectionOutputError(
                        "LLM_DETECTION_OUTPUT_TOO_MANY_ITEMS",
                        actual_items=actual_items,
                        max_detections=self._max_detections,
                    )
                resolved.append(
                    Detection(
                        start=start,
                        end=start + len(candidate.text),
                        text=candidate.text,
                        type=candidate.type,
                        policyId=policy_id_for_detection_type(candidate.type),
                        source="llm",
                        score=candidate.score,
                    )
                )
                start = next_start
                next_start = original_text.find(
                    candidate.text,
                    start + 1,
                ) if start >= 0 else -1

        return tuple(resolved)


def _revalidate_candidate(
    candidate: object,
    *,
    index: int,
) -> LlmDetectionCandidate:
    """주입 Parser가 검증을 우회해도 후보 필드를 새 모델로 다시 확인합니다."""

    if type(candidate) is not LlmDetectionCandidate:
        del candidate
        raise LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_ITEM",
            item_index=index,
        )

    normalized: LlmDetectionCandidate | None = None
    validation_failed = False
    try:
        normalized = LlmDetectionCandidate.model_validate(
            {
                "text": candidate.text,
                "type": candidate.type,
                "score": candidate.score,
            }
        )
        normalized.text.encode("utf-8")
        normalized.type.encode("utf-8")
    except (AttributeError, ValidationError, UnicodeEncodeError):
        validation_failed = True

    if validation_failed:
        del candidate, normalized
        raise LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_ITEM",
            item_index=index,
        )
    if normalized is None:
        raise RuntimeError("LLM 탐지 후보 재검증 결과가 없습니다")
    return normalized


__all__ = ["LlmDetectionSpanResolver"]
