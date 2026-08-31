"""탐지 결과의 원문 범위와 Backend 반환 계약을 검증합니다."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import ValidationError

from app.schemas.detection import Detection, DetectionSource


DEFAULT_MAX_DETECTION_RESULTS = 10_000
DetectionSpanValidationErrorCode = Literal[
    "INVALID_TEXT",
    "INVALID_CONTAINER",
    "TOO_MANY_DETECTIONS",
    "INVALID_ITEM",
    "INVALID_SPAN",
    "TEXT_MISMATCH",
    "SOURCE_MISMATCH",
]

_DETECTION_SOURCES = frozenset({"regex", "ner", "llm"})


class DetectionSpanValidationError(ValueError):
    """탐지 결과를 안전한 원문 구간으로 검증할 수 없으면 발생합니다."""

    def __init__(
        self,
        code: DetectionSpanValidationErrorCode,
        *,
        item_index: int | None = None,
        actual_items: int | None = None,
        max_detections: int | None = None,
    ) -> None:
        self.code = code
        self.item_index = item_index
        self.actual_items = actual_items
        self.max_detections = max_detections

        detail = {
            "INVALID_TEXT": "검증할 원문이 유효하지 않습니다",
            "INVALID_CONTAINER": (
                "탐지 결과는 list 또는 tuple이어야 합니다"
            ),
            "TOO_MANY_DETECTIONS": (
                "탐지 결과 수가 허용 한도를 초과했습니다"
            ),
            "INVALID_ITEM": (
                "탐지 결과 항목이 Detection 계약과 다릅니다"
            ),
            "INVALID_SPAN": "탐지 범위가 원문 범위를 벗어났습니다",
            "TEXT_MISMATCH": (
                "탐지 문자열이 원문의 해당 구간과 일치하지 않습니다"
            ),
            "SOURCE_MISMATCH": (
                "탐지 출처가 실행한 Backend 역할과 다릅니다"
            ),
        }[code]
        if item_index is not None:
            detail = f"{detail}: index={item_index}"
        super().__init__(f"{code}: {detail}")


class SpanValidator:
    """Backend 탐지 결과를 비신뢰 입력으로 보고 전부 다시 검증합니다."""

    __slots__ = ("_max_detections",)

    def __init__(
        self,
        *,
        max_detections: int = DEFAULT_MAX_DETECTION_RESULTS,
    ) -> None:
        self._max_detections = _require_positive_integer(
            max_detections,
            field_name="max_detections",
        )

    @property
    def max_detections(self) -> int:
        """한 번에 검증할 수 있는 최대 탐지 결과 수를 반환합니다."""

        return self._max_detections

    def validate(
        self,
        original_text: str,
        detections: object,
        *,
        expected_source: DetectionSource | None = None,
    ) -> tuple[Detection, ...]:
        """모든 항목을 재검증하고 원문과 일치하는 불변 결과를 반환합니다."""

        if not _is_valid_expected_source(expected_source):
            del original_text, detections
            raise ValueError(
                "expected_source는 regex, ner, llm 또는 None이어야 합니다"
            )

        failure: DetectionSpanValidationError | None = None
        candidate: object = None
        detection_sequence: list[object] | tuple[object, ...] | None = None
        normalized: Detection | None = None
        validated: list[Detection] = []

        if type(original_text) is not str:
            failure = DetectionSpanValidationError("INVALID_TEXT")
        else:
            text_encoding_failed = False
            try:
                original_text.encode("utf-8")
            except UnicodeEncodeError:
                text_encoding_failed = True
            if text_encoding_failed:
                failure = DetectionSpanValidationError("INVALID_TEXT")

        if type(detections) is list:
            detection_sequence = cast(list[object], detections)
        elif type(detections) is tuple:
            detection_sequence = cast(tuple[object, ...], detections)
        elif failure is None:
            failure = DetectionSpanValidationError("INVALID_CONTAINER")

        actual_items = (
            len(detection_sequence)
            if detection_sequence is not None
            else 0
        )
        if failure is None and actual_items > self._max_detections:
            failure = DetectionSpanValidationError(
                "TOO_MANY_DETECTIONS",
                actual_items=actual_items,
                max_detections=self._max_detections,
            )

        if failure is None and detection_sequence is not None:
            text_length = len(original_text)
            for index, candidate in enumerate(detection_sequence):
                normalized, item_failure = _revalidate_detection(
                    candidate,
                    item_index=index,
                )
                if item_failure is not None:
                    failure = item_failure
                    break
                if normalized is None:
                    raise RuntimeError("Detection 재검증 결과가 없습니다")

                if not (
                    0
                    <= normalized.start
                    < normalized.end
                    <= text_length
                ):
                    failure = DetectionSpanValidationError(
                        "INVALID_SPAN",
                        item_index=index,
                    )
                    break
                if (
                    original_text[normalized.start : normalized.end]
                    != normalized.text
                ):
                    failure = DetectionSpanValidationError(
                        "TEXT_MISMATCH",
                        item_index=index,
                    )
                    break
                if expected_source is not None and (
                    normalized.source != expected_source
                ):
                    failure = DetectionSpanValidationError(
                        "SOURCE_MISMATCH",
                        item_index=index,
                    )
                    break
                validated.append(normalized)

        if failure is not None:
            del (
                original_text,
                detections,
                candidate,
                detection_sequence,
                normalized,
                validated,
            )
            raise failure
        return tuple(validated)


def _revalidate_detection(
    candidate: object,
    *,
    item_index: int,
) -> tuple[
    Detection | None,
    DetectionSpanValidationError | None,
]:
    """검증 우회 가능성까지 고려해 Detection을 새 모델로 다시 만듭니다."""

    if type(candidate) is not Detection:
        return None, DetectionSpanValidationError(
            "INVALID_ITEM",
            item_index=item_index,
        )

    start: object = None
    end: object = None
    text: object = None
    detection_type: object = None
    policy_id: object = None
    source: object = None
    score: object = None
    field_read_failed = False
    try:
        start = candidate.start
        end = candidate.end
        text = candidate.text
        detection_type = candidate.type
        policy_id = candidate.policy_id
        source = candidate.source
        score = candidate.score
    except AttributeError:
        field_read_failed = True
    if field_read_failed:
        del candidate
        return None, DetectionSpanValidationError(
            "INVALID_ITEM",
            item_index=item_index,
        )

    if (
        type(start) is not int
        or type(end) is not int
        or type(text) is not str
        or type(detection_type) is not str
        or type(policy_id) is not str
        or type(source) is not str
        or type(score) not in {int, float}
    ):
        del candidate, text, detection_type
        return None, DetectionSpanValidationError(
            "INVALID_ITEM",
            item_index=item_index,
        )

    if not (0 <= start < end):
        del candidate, text, detection_type
        return None, DetectionSpanValidationError(
            "INVALID_SPAN",
            item_index=item_index,
        )

    normalized: Detection | None = None
    validation_failed = False
    try:
        normalized = Detection.model_validate(
            {
                "start": start,
                "end": end,
                "text": text,
                "type": detection_type,
                "policyId": policy_id,
                "source": source,
                "score": score,
            }
        )
        normalized.text.encode("utf-8")
        normalized.type.encode("utf-8")
    except (AttributeError, ValidationError, UnicodeEncodeError):
        validation_failed = True

    if validation_failed:
        del candidate, normalized
        return None, DetectionSpanValidationError(
            "INVALID_ITEM",
            item_index=item_index,
        )
    return normalized, None


def _is_valid_expected_source(
    expected_source: DetectionSource | None,
) -> bool:
    """선택적인 예상 출처가 공통 Detection 출처인지 확인합니다."""

    return expected_source is None or (
        type(expected_source) is str
        and expected_source in _DETECTION_SOURCES
    )


def _require_positive_integer(value: int, *, field_name: str) -> int:
    """자원 제한값에 bool이 아닌 1 이상의 정수만 허용합니다."""

    if type(value) is not int or value < 1:
        raise ValueError(f"{field_name}는 1 이상의 정수여야 합니다")
    return value


__all__ = [
    "DEFAULT_MAX_DETECTION_RESULTS",
    "DetectionSpanValidationError",
    "DetectionSpanValidationErrorCode",
    "SpanValidator",
]
