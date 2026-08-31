"""LLM Backend와 생성 Pipeline이 공유하는 결과 모델입니다."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.schemas.registry import ResourceId


NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class GenerationModel(BaseModel):
    """생성 결과의 추가 필드와 재할당을 막고 엄격하게 검증합니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        strict=True,
    )


class LlmTokenUsage(GenerationModel):
    """Provider가 반환한 토큰 사용량을 공통 필드로 정규화합니다."""

    input_tokens: int = Field(ge=0, alias="inputTokens")
    output_tokens: int = Field(ge=0, alias="outputTokens")
    total_tokens: int = Field(ge=0, alias="totalTokens")


class LlmResult(GenerationModel):
    """서로 다른 LLM Provider의 생성 결과를 정규화한 공통 모델입니다."""

    text: str
    model_name: NonEmptyString | None = Field(
        default=None,
        alias="modelName",
    )
    finish_reason: NonEmptyString | None = Field(
        default=None,
        alias="finishReason",
    )
    usage: LlmTokenUsage | None = None


class PreviousTextMessage(GenerationModel):
    """이전 사용자 입력 또는 Local LLM 응답 한 건입니다."""

    role: Literal["user", "assistant"]
    content: NonEmptyString


class GenerateRequest(GenerationModel):
    """현재 입력, 이전 응답 배열과 필수 LLM Deployment ID를 검증합니다."""

    text: NonEmptyString
    previous_text: tuple[PreviousTextMessage, ...] = Field(
        default=(),
        alias="previousText",
    )
    llm_deployment_id: ResourceId = Field(alias="llmDeploymentId")

    @field_validator("previous_text", mode="before")
    @classmethod
    def normalize_previous_text(cls, value: object) -> object:
        """JSON 배열을 요청 원본과 분리된 불변 tuple로 변환합니다."""

        if type(value) is list:
            return tuple(value)
        return value


__all__ = [
    "GenerateRequest",
    "LlmResult",
    "LlmTokenUsage",
    "PreviousTextMessage",
]
