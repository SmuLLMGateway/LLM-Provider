"""GLiNER NER 서버의 엄격한 HTTP 요청과 응답 계약입니다."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


NonEmptyText = Annotated[str, StringConstraints(min_length=1)]
DetectionType = Literal[
    "PERSONAL_IDENTITY",
    "UNIQUE_IDENTITY",
    "CONTACT",
    "LOCATION",
    "PAYMENT",
    "FINANCE_ACCOUNT",
    "PERSONAL_FINANCE",
    "SENSITIVE_PERSONAL",
    "AUTH",
    "SECURITY_INFRA",
    "SYSTEM_LOG",
    "PERSONAL",
    "CLIENT",
    "R&D",
]
ALLOWED_DETECTION_TYPES: tuple[DetectionType, ...] = (
    "PERSONAL_IDENTITY",
    "UNIQUE_IDENTITY",
    "CONTACT",
    "LOCATION",
    "PAYMENT",
    "FINANCE_ACCOUNT",
    "PERSONAL_FINANCE",
    "SENSITIVE_PERSONAL",
    "AUTH",
    "SECURITY_INFRA",
    "SYSTEM_LOG",
    "PERSONAL",
    "CLIENT",
    "R&D",
)


class ContractModel(BaseModel):
    """추가 필드와 느슨한 타입 변환을 거부하는 계약 기반 모델입니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class NerRequest(ContractModel):
    """LPL이 모든 공통 NER 서버에 전송하는 고정 요청입니다."""

    text: NonEmptyText

    @model_validator(mode="after")
    def validate_text(self) -> NerRequest:
        """원문이 UTF-8로 안전하게 표현되는지 검증합니다."""

        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "text는 유효한 UTF-8 문자열이어야 합니다"
            ) from None
        return self


class NerDetection(ContractModel):
    """모델 종류와 무관한 공통 NER 탐지 결과 하나입니다."""

    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: NonEmptyText
    type: DetectionType
    score: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_span_order(self) -> NerDetection:
        """반개방 구간의 끝이 시작보다 뒤인지 검증합니다."""

        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        return self


class NerResponse(ContractModel):
    """LPL 공통 HTTP NER Backend가 요구하는 성공 응답입니다."""

    detections: tuple[NerDetection, ...] = ()


class HealthResponse(ContractModel):
    """모델이 준비된 프로세스의 상태 응답입니다."""

    status: Literal["ok"] = "ok"
    model_name: str = Field(alias="modelName")
    model_revision: str = Field(alias="modelRevision")
    device: Literal["cpu", "cuda"]


__all__ = [
    "ALLOWED_DETECTION_TYPES",
    "DetectionType",
    "HealthResponse",
    "NerDetection",
    "NerRequest",
    "NerResponse",
]
