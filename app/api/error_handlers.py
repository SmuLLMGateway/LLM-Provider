"""요청 데이터가 오류 응답에 노출되지 않도록 공통 Handler를 제공합니다."""

from __future__ import annotations

from typing import Never

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.request_validation import format_validation_error_message
from app.backends.errors import (
    BackendConfigurationError,
    BackendError,
    BackendInputTooLargeError,
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
)
from app.backends.backend_registry import BackendValidationError
from app.registry.deployment_resolver import DeploymentResolutionError
from app.registry.mutator import (
    DeploymentAlreadyExistsError,
    DeploymentMustBeDisabledError,
    DeploymentNotFoundError,
)
from app.schemas.errors import ApiErrorResponse
from app.services.deployment_management import (
    DeploymentActivationError,
    DeploymentRollbackError,
    DeploymentStorageError,
)


REQUEST_VALIDATION_ERROR_RESPONSES = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ApiErrorResponse,
        "description": "요청 또는 API 계약 검증 오류",
    }
}


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    """민감한 내부 상세를 제외한 공통 JSON 오류 응답을 만듭니다."""

    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": message,
            }
        },
    )


def raise_api_error(
    *,
    status_code: int,
    code: str,
    message: str,
) -> Never:
    """민감한 내부 상세를 제외한 공통 HTTP 오류를 발생시킵니다."""

    raise HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
        },
    ) from None


async def request_validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """입력값은 숨기고 잘못된 필드와 안전한 사유를 담은 422를 반환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="REQUEST_VALIDATION_FAILED",
        message=format_validation_error_message(error),
    )


async def deployment_resolution_error_handler(
    request: Request,
    error: DeploymentResolutionError,
) -> JSONResponse:
    """요청이 선택한 Deployment의 조회·상태·종류 오류를 변환합니다."""

    del request
    status_code, message = {
        "DEPLOYMENT_NOT_FOUND": (
            status.HTTP_404_NOT_FOUND,
            "요청한 Deployment를 찾을 수 없습니다",
        ),
        "DEPLOYMENT_DISABLED": (
            status.HTTP_409_CONFLICT,
            "요청한 Deployment가 비활성화되어 있습니다",
        ),
        "DEPLOYMENT_KIND_MISMATCH": (
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "요청한 Deployment를 해당 역할에 사용할 수 없습니다",
        ),
    }[error.code]
    return _error_response(
        status_code=status_code,
        code=error.code,
        message=message,
    )


async def deployment_already_exists_error_handler(
    request: Request,
    error: DeploymentAlreadyExistsError,
) -> JSONResponse:
    """중복 Deployment 추가를 안전한 409 응답으로 변환합니다."""

    del request, error
    return _error_response(
        status_code=status.HTTP_409_CONFLICT,
        code="DEPLOYMENT_ALREADY_EXISTS",
        message="같은 ID의 Deployment가 이미 존재합니다",
    )


async def deployment_not_found_error_handler(
    request: Request,
    error: DeploymentNotFoundError,
) -> JSONResponse:
    """수정 대상이 없거나 경로 종류와 다르면 안전한 404를 반환합니다."""

    del request, error
    return _error_response(
        status_code=status.HTTP_404_NOT_FOUND,
        code="DEPLOYMENT_NOT_FOUND",
        message="요청한 Deployment를 찾을 수 없습니다",
    )


async def deployment_must_be_disabled_error_handler(
    request: Request,
    error: DeploymentMustBeDisabledError,
) -> JSONResponse:
    """활성 Deployment 삭제 요청을 안전한 409 응답으로 변환합니다."""

    del request, error
    return _error_response(
        status_code=status.HTTP_409_CONFLICT,
        code="DEPLOYMENT_MUST_BE_DISABLED",
        message="Deployment를 비활성화한 후 삭제해야 합니다",
    )


async def backend_validation_error_handler(
    request: Request,
    error: BackendValidationError,
) -> JSONResponse:
    """Adapter 설정 계약 오류를 원문 없이 안전한 422로 변환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code=error.code,
        message="Deployment 설정이 Adapter 계약과 다릅니다",
    )


async def deployment_activation_error_handler(
    request: Request,
    error: DeploymentActivationError,
) -> JSONResponse:
    """복원에 성공한 Snapshot 활성화 실패를 안전한 503으로 변환합니다."""

    del request, error
    return _error_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="DEPLOYMENT_ACTIVATION_FAILED",
        message="Deployment 설정을 실행 상태로 적용하지 못했습니다",
    )


async def deployment_rollback_error_handler(
    request: Request,
    error: DeploymentRollbackError,
) -> JSONResponse:
    """활성화 실패 뒤 복원까지 실패한 상태를 안전한 500으로 변환합니다."""

    del request, error
    return _error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="DEPLOYMENT_ROLLBACK_FAILED",
        message="Deployment 설정 복구에 실패했습니다",
    )


async def deployment_storage_error_handler(
    request: Request,
    error: DeploymentStorageError,
) -> JSONResponse:
    """Registry 파일 읽기·쓰기 실패를 안전한 500 응답으로 변환합니다."""

    del request, error
    return _error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="DEPLOYMENT_STORAGE_FAILED",
        message="Deployment 설정 저장소 작업에 실패했습니다",
    )


async def backend_configuration_error_handler(
    request: Request,
    error: BackendConfigurationError,
) -> JSONResponse:
    """Adapter 설정 오류를 안전한 공통 500 응답으로 변환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=error.code,
        message="모델 서버 실행 설정이 올바르지 않습니다",
    )


async def backend_timeout_error_handler(
    request: Request,
    error: BackendTimeoutError,
) -> JSONResponse:
    """Adapter 제한시간 오류를 안전한 공통 504 응답으로 변환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        code=error.code,
        message="모델 서버 응답 제한 시간을 초과했습니다",
    )


async def backend_transport_error_handler(
    request: Request,
    error: BackendTransportError,
) -> JSONResponse:
    """Adapter 전송 오류를 안전한 공통 502 응답으로 변환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code=error.code,
        message="모델 서버 호출 또는 응답 처리에 실패했습니다",
    )


async def backend_response_error_handler(
    request: Request,
    error: BackendResponseError,
) -> JSONResponse:
    """Adapter 응답 오류를 안전한 공통 502 응답으로 변환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code=error.code,
        message="모델 서버 호출 또는 응답 처리에 실패했습니다",
    )


async def backend_input_too_large_error_handler(
    request: Request,
    error: BackendInputTooLargeError,
) -> JSONResponse:
    """모델이 원문 전체를 처리할 수 없는 경우 안전한 413을 반환합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        code=error.code,
        message="요청이 모델의 전체 처리 한도를 초과했습니다",
    )


async def backend_error_handler(
    request: Request,
    error: BackendError,
) -> JSONResponse:
    """세부 분류가 없는 Backend 오류도 안전한 502로 축약합니다."""

    del request
    return _error_response(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code=error.code,
        message="모델 서버 호출 또는 응답 처리에 실패했습니다",
    )


def register_error_handlers(application: FastAPI) -> None:
    """LPL API의 공통 예외 Handler를 애플리케이션에 등록합니다."""

    application.add_exception_handler(
        RequestValidationError,
        request_validation_error_handler,
    )
    application.add_exception_handler(
        DeploymentResolutionError,
        deployment_resolution_error_handler,
    )
    application.add_exception_handler(
        DeploymentAlreadyExistsError,
        deployment_already_exists_error_handler,
    )
    application.add_exception_handler(
        DeploymentNotFoundError,
        deployment_not_found_error_handler,
    )
    application.add_exception_handler(
        DeploymentMustBeDisabledError,
        deployment_must_be_disabled_error_handler,
    )
    application.add_exception_handler(
        BackendValidationError,
        backend_validation_error_handler,
    )
    application.add_exception_handler(
        DeploymentActivationError,
        deployment_activation_error_handler,
    )
    application.add_exception_handler(
        DeploymentRollbackError,
        deployment_rollback_error_handler,
    )
    application.add_exception_handler(
        DeploymentStorageError,
        deployment_storage_error_handler,
    )
    application.add_exception_handler(
        BackendConfigurationError,
        backend_configuration_error_handler,
    )
    application.add_exception_handler(
        BackendTimeoutError,
        backend_timeout_error_handler,
    )
    application.add_exception_handler(
        BackendTransportError,
        backend_transport_error_handler,
    )
    application.add_exception_handler(
        BackendInputTooLargeError,
        backend_input_too_large_error_handler,
    )
    application.add_exception_handler(
        BackendResponseError,
        backend_response_error_handler,
    )
    application.add_exception_handler(
        BackendError,
        backend_error_handler,
    )


__all__ = [
    "backend_validation_error_handler",
    "backend_configuration_error_handler",
    "backend_error_handler",
    "backend_input_too_large_error_handler",
    "backend_response_error_handler",
    "backend_timeout_error_handler",
    "backend_transport_error_handler",
    "deployment_activation_error_handler",
    "deployment_already_exists_error_handler",
    "deployment_must_be_disabled_error_handler",
    "deployment_not_found_error_handler",
    "deployment_resolution_error_handler",
    "deployment_rollback_error_handler",
    "deployment_storage_error_handler",
    "raise_api_error",
    "register_error_handlers",
    "REQUEST_VALIDATION_ERROR_RESPONSES",
    "request_validation_error_handler",
]
