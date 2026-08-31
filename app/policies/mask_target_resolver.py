"""Detection span을 마스킹 가능한 비중첩 원문 구간으로 조립합니다."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.detection import (
    Detection,
    DetectionSource,
    DetectionType,
)


_SOURCE_ORDER: dict[DetectionSource, int] = {
    "regex": 0,
    "ner": 1,
    "llm": 2,
}


@dataclass(frozen=True, slots=True)
class MaskTarget:
    """서로 겹치는 Detection들의 합집합으로 만든 한 마스킹 대상입니다."""

    target_id: str
    start: int
    end: int
    text: str
    types: tuple[DetectionType, ...]
    sources: tuple[DetectionSource, ...]


class MaskTargetResolver:
    """겹치는 Detection은 합치고 접하기만 하는 구간은 분리합니다."""

    def resolve(
        self,
        original_text: str,
        detections: tuple[Detection, ...],
    ) -> tuple[MaskTarget, ...]:
        """원문 순서의 결정적인 비중첩 MaskTarget 목록을 반환합니다."""

        if type(original_text) is not str:
            raise TypeError("original_text는 문자열이어야 합니다")
        if type(detections) is not tuple or any(
            type(item) is not Detection for item in detections
        ):
            raise TypeError("detections는 검증된 Detection tuple이어야 합니다")
        if not detections:
            return ()

        ordered = sorted(
            enumerate(detections),
            key=lambda item: (
                item[1].start,
                item[1].end,
                item[0],
            ),
        )
        components: list[
            tuple[
                int,
                int,
                set[DetectionType],
                set[DetectionSource],
            ]
        ] = []
        for _, detection in ordered:
            if components and detection.start < components[-1][1]:
                start, end, types, sources = components[-1]
                types.add(detection.type)
                sources.add(detection.source)
                components[-1] = (
                    start,
                    max(end, detection.end),
                    types,
                    sources,
                )
                continue
            components.append(
                (
                    detection.start,
                    detection.end,
                    {detection.type},
                    {detection.source},
                )
            )

        return tuple(
            MaskTarget(
                target_id=f"target-{index}",
                start=start,
                end=end,
                text=original_text[start:end],
                types=tuple(sorted(types)),
                sources=tuple(
                    sorted(sources, key=_SOURCE_ORDER.__getitem__)
                ),
            )
            for index, (start, end, types, sources) in enumerate(
                components,
                start=1,
            )
        )


__all__ = ["MaskTarget", "MaskTargetResolver"]
