"""대화 제목 생성 API의 요청과 응답 계약을 정의합니다."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.registry import ResourceId


NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
GeneratedTitle = Annotated[
    str,
    StringConstraints(min_length=1, max_length=30),
]


class TitleGenerationModel(BaseModel):
    """제목 생성 계약에서 알 수 없는 필드와 암묵적 변환을 거부합니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        strict=True,
    )


class GenerateTitleRequest(TitleGenerationModel):
    """첫 사용자 메시지와 제목 생성에 사용할 LLM Deployment를 받습니다."""

    text: NonEmptyString
    llm_deployment_id: ResourceId = Field(alias="llmDeploymentId")


class GenerateTitleResponse(TitleGenerationModel):
    """검증을 마친 한 줄 대화 제목을 반환합니다."""

    title: GeneratedTitle


__all__ = [
    "GenerateTitleRequest",
    "GenerateTitleResponse",
    "GeneratedTitle",
]
