"""Backend Adapter 카탈로그 응답 스키마입니다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.registry import AdapterType


class AdapterResponseModel(BaseModel):
    """외부 Adapter 응답 모델의 공통 설정입니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        strict=True,
    )


class AdapterListResponse(AdapterResponseModel):
    """현재 실행 가능한 종류별 Adapter 이름 목록입니다."""

    adapters: tuple[AdapterType, ...] = ()


__all__ = ["AdapterListResponse"]
