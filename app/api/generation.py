"""Option A 로컬 LLM 생성 HTTP API를 제공합니다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_generation_pipeline
from app.api.error_handlers import (
    REQUEST_VALIDATION_ERROR_RESPONSES,
    raise_api_error,
)
from app.backends.provider_registry import BackendProviderLookupError
from app.registry.manager import RegistryManagerNotInitializedError
from app.schemas.generation import GenerateRequest, LlmResult
from app.services.generation_pipeline import (
    GenerationBackendResultError,
    GenerationPipeline,
)


router = APIRouter(tags=["generation"])


@router.post(
    "/generate",
    response_model=LlmResult,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def generate(
    request: GenerateRequest,
    pipeline: Annotated[
        GenerationPipeline,
        Depends(get_generation_pipeline),
    ],
) -> LlmResult:
    """요청에서 명시한 LLM Deployment로 로컬 생성을 실행합니다."""

    try:
        return await pipeline.generate(
            text=request.text,
            llm_deployment_id=request.llm_deployment_id,
            previous_text=request.previous_text,
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )
    except BackendProviderLookupError as error:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=error.code,
            message="실행할 Backend Provider가 준비되지 않았습니다",
        )
    except GenerationBackendResultError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="GENERATION_BACKEND_RESULT_INVALID",
            message="Backend 결과가 생성 응답 계약과 다릅니다",
        )


__all__ = ["router"]
