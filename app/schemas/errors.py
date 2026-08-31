"""LPL API가 공통으로 반환하는 안전한 오류 응답 모델을 정의합니다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.registry import NonEmptyString


class ApiErrorModel(BaseModel):
    """오류 응답에 계약 밖 필드가 추가되는 것을 막습니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )


class ApiErrorDetail(ApiErrorModel):
    """클라이언트가 분기할 코드와 사람이 읽을 메시지를 제공합니다."""

    code: NonEmptyString
    message: NonEmptyString


class ApiErrorResponse(ApiErrorModel):
    """FastAPI HTTP 오류의 공통 최상위 구조입니다."""

    detail: ApiErrorDetail


__all__ = ["ApiErrorDetail", "ApiErrorResponse"]
