"""탐지 범위 검증과 중복·겹침 정책을 하나의 경계로 조립합니다."""

from __future__ import annotations

from app.policies.overlap_resolver import OverlapResolver
from app.policies.span_validator import (
    DetectionSpanValidationError,
    SpanValidator,
)
from app.schemas.detection import Detection, DetectionSource


class DetectionResultValidator:
    """Backend 탐지 결과가 응답에 들어가기 전 적용할 공통 검증 경계입니다."""

    def __init__(
        self,
        *,
        span_validator: SpanValidator | None = None,
        overlap_resolver: OverlapResolver | None = None,
    ) -> None:
        self._span_validator = (
            span_validator
            if span_validator is not None
            else SpanValidator()
        )
        self._overlap_resolver = (
            overlap_resolver
            if overlap_resolver is not None
            else OverlapResolver()
        )

    def validate(
        self,
        original_text: str,
        detections: object,
        *,
        expected_source: DetectionSource | None = None,
    ) -> tuple[Detection, ...]:
        """전체 결과를 먼저 검증한 뒤 중복과 겹침 정책을 적용합니다."""

        validated: tuple[Detection, ...] = ()
        configuration_error_detail: str | None = None
        validation_error: DetectionSpanValidationError | None = None
        try:
            validated = self._span_validator.validate(
                original_text,
                detections,
                expected_source=expected_source,
            )
        except DetectionSpanValidationError as error:
            validation_error = DetectionSpanValidationError(
                error.code,
                item_index=error.item_index,
                actual_items=error.actual_items,
                max_detections=error.max_detections,
            )
        except ValueError as error:
            configuration_error_detail = str(error)

        if validation_error is not None:
            del original_text, detections, validated
            raise validation_error
        if configuration_error_detail is not None:
            del original_text, detections, validated
            raise ValueError(configuration_error_detail)

        del original_text, detections
        return self._overlap_resolver.resolve(validated)


__all__ = ["DetectionResultValidator"]
