"""LLM 마스킹 API의 엄격한 요청과 응답 계약을 정의합니다."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.detection import (
    Detection,
    DetectionSource,
    DetectionType,
)
from app.schemas.registry import ResourceId


NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
EntityId = Annotated[
    str,
    StringConstraints(pattern=r"^entity-[1-9][0-9]{0,4}$"),
]
MaskPlaceholder = Annotated[
    str,
    StringConstraints(
        pattern=r"^\[\[LPL_[0-9a-f]{16}_[0-9]{4,5}\]\]$",
    ),
]
MAX_MASK_DETECTIONS = 128


class MaskingModel(BaseModel):
    """알 수 없는 필드, 암묵적 형 변환과 필드 재할당을 거부합니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


def _normalize_tuple(value: object) -> object:
    """JSON 배열을 요청 객체와 분리된 불변 tuple로 변환합니다."""

    if type(value) is list:
        return tuple(value)
    return value


class MaskRequest(MaskingModel):
    """원문, 실행할 LLM과 Gateway가 병합한 전체 탐지 결과를 받습니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
        hide_input_in_errors=True,
    )

    text: NonEmptyString
    llm_deployment_id: ResourceId = Field(alias="llmDeploymentId")
    detections: tuple[Detection, ...] = Field(
        max_length=MAX_MASK_DETECTIONS,
    )

    @field_validator("detections", mode="before")
    @classmethod
    def normalize_detections(cls, value: object) -> object:
        """JSON 배열을 내부 불변 tuple로 정규화합니다."""

        return _normalize_tuple(value)

    @model_validator(mode="after")
    def validate_detection_spans(self) -> Self:
        """모든 Detection이 요청 원문의 정확한 문자 범위를 가리키는지 확인합니다."""

        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "text는 유효한 UTF-8 문자열이어야 합니다"
            ) from None

        for index, detection in enumerate(self.detections):
            try:
                detection.text.encode("utf-8")
                detection.type.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(
                    f"detections[{index}]의 문자열은 유효한 UTF-8이어야 합니다"
                ) from None
            if detection.end > len(self.text):
                raise ValueError(
                    f"detections[{index}].end는 text 길이를 초과할 수 없습니다"
                )
            if self.text[detection.start : detection.end] != detection.text:
                raise ValueError(
                    f"detections[{index}].text는 요청 원문의 해당 구간과 "
                    "일치해야 합니다"
                )
        return self


class MaskReplacement(MaskingModel):
    """원문 구간 하나와 LLM이 판단한 동일 대상 placeholder를 연결합니다."""

    start: int = Field(ge=0)
    end: int = Field(ge=1)
    entity_id: EntityId = Field(alias="entityId")
    placeholder: MaskPlaceholder
    types: tuple[DetectionType, ...] = Field(min_length=1)
    sources: tuple[DetectionSource, ...] = Field(min_length=1)

    @field_validator("types", "sources", mode="before")
    @classmethod
    def normalize_sequences(cls, value: object) -> object:
        """파생 메타데이터 배열도 불변 tuple로 보관합니다."""

        return _normalize_tuple(value)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        """Replacement 원문 범위는 비어 있을 수 없습니다."""

        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        return self


class MaskResponse(MaskingModel):
    """검증된 마스킹 본문과 원문 좌표 기준 치환 정보를 반환합니다."""

    masked_text: str = Field(alias="maskedText")
    replacements: tuple[MaskReplacement, ...]

    @field_validator("replacements", mode="before")
    @classmethod
    def normalize_replacements(cls, value: object) -> object:
        """응답 치환 목록을 불변 tuple로 보관합니다."""

        return _normalize_tuple(value)


__all__ = [
    "EntityId",
    "MaskPlaceholder",
    "MaskReplacement",
    "MaskRequest",
    "MaskResponse",
    "MaskingModel",
    "MAX_MASK_DETECTIONS",
]
