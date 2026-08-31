"""중복 탐지를 축약하고 겹치는 탐지 근거를 결정적으로 정렬합니다."""

from __future__ import annotations

from app.schemas.detection import Detection, DetectionSource


_SOURCE_PRIORITY: dict[DetectionSource, int] = {
    "regex": 0,
    "ner": 1,
    "llm": 2,
}


class OverlapResolver:
    """정보 손실 없이 의미 중복만 제거하는 탐지 겹침 정책입니다."""

    def resolve(
        self,
        detections: tuple[Detection, ...],
    ) -> tuple[Detection, ...]:
        """중복을 축약하고 범위가 겹치는 서로 다른 탐지는 보존합니다."""

        if type(detections) is not tuple:
            raise TypeError(
                "detections는 SpanValidator가 반환한 tuple이어야 합니다"
            )
        if any(type(detection) is not Detection for detection in detections):
            raise TypeError(
                "detections의 모든 항목은 정확한 Detection이어야 합니다"
            )

        selected: dict[tuple[int, int, str], Detection] = {}
        for detection in detections:
            duplicate_key = (
                detection.start,
                detection.end,
                detection.type,
            )
            current = selected.get(duplicate_key)
            if current is None or _is_preferred(detection, current):
                selected[duplicate_key] = detection

        return tuple(sorted(selected.values(), key=_output_order))


def _is_preferred(candidate: Detection, current: Detection) -> bool:
    """동일 의미 중복 중 출처와 점수 우선순위가 높은 결과를 고릅니다."""

    candidate_source = _SOURCE_PRIORITY[candidate.source]
    current_source = _SOURCE_PRIORITY[current.source]
    if candidate_source != current_source:
        return candidate_source < current_source
    return candidate.score > current.score


def _output_order(
    detection: Detection,
) -> tuple[int, int, str, int, float, str]:
    """비동기 완료 순서와 무관한 최종 탐지 정렬 키를 만듭니다."""

    return (
        detection.start,
        -detection.end,
        detection.type,
        _SOURCE_PRIORITY[detection.source],
        -detection.score,
        detection.text,
    )


__all__ = ["OverlapResolver"]
