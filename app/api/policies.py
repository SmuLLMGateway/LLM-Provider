"""전역 활성 Policy ID 설정 조회와 전체 교체 API를 제공합니다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_policy_settings_manager
from app.api.error_handlers import (
    REQUEST_VALIDATION_ERROR_RESPONSES,
    raise_api_error,
)
from app.schemas.errors import ApiErrorResponse
from app.schemas.policy_settings import PolicySettings
from app.services.policy_settings import (
    PolicySettingsManager,
    PolicySettingsNotInitializedError,
    PolicySettingsStorageError,
)


router = APIRouter(prefix="/policies", tags=["policies"])
_POLICY_ERROR_RESPONSES = {
    **REQUEST_VALIDATION_ERROR_RESPONSES,
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": ApiErrorResponse,
        "description": "활성 정책 설정 파일 저장 실패",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ApiErrorResponse,
        "description": "활성 정책 설정이 아직 준비되지 않음",
    },
}


@router.get(
    "/enabled",
    response_model=PolicySettings,
    responses=_POLICY_ERROR_RESPONSES,
)
async def get_enabled_policies(
    manager: Annotated[
        PolicySettingsManager,
        Depends(get_policy_settings_manager),
    ],
) -> PolicySettings:
    """현재 LPL 인스턴스가 검사하는 전역 활성 정책을 반환합니다."""

    try:
        return manager.capture()
    except PolicySettingsNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="POLICY_SETTINGS_NOT_INITIALIZED",
            message="활성 정책 설정이 아직 준비되지 않았습니다",
        )


@router.put(
    "/enabled",
    response_model=PolicySettings,
    responses=_POLICY_ERROR_RESPONSES,
)
async def replace_enabled_policies(
    request: PolicySettings,
    manager: Annotated[
        PolicySettingsManager,
        Depends(get_policy_settings_manager),
    ],
) -> PolicySettings:
    """활성 정책 전체를 원자 저장하고 실행 상태에 즉시 반영합니다."""

    try:
        return manager.replace(request).settings
    except PolicySettingsNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="POLICY_SETTINGS_NOT_INITIALIZED",
            message="활성 정책 설정이 아직 준비되지 않았습니다",
        )
    except PolicySettingsStorageError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="POLICY_SETTINGS_STORAGE_FAILED",
            message="활성 정책 설정을 저장할 수 없습니다",
        )


__all__ = ["router"]
