"""현재 LPL Runtime이 지원하는 Backend Adapter 계약을 조회합니다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_adapter_catalog
from app.schemas.adapters import AdapterListResponse
from app.services.adapter_catalog import AdapterCatalogService


router = APIRouter(prefix="/adapters", tags=["adapters"])


@router.get(
    "/llm",
    response_model=AdapterListResponse,
)
async def list_llm_adapters(
    catalog: Annotated[
        AdapterCatalogService,
        Depends(get_adapter_catalog),
    ],
) -> AdapterListResponse:
    """등록된 LLM Adapter 계약과 Provider 준비 상태를 반환합니다."""

    return catalog.list_adapters("llm")


__all__ = ["router"]
