"""첫 사용자 메시지에서 짧은 대화 제목을 생성하는 API입니다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_title_generation_pipeline
from app.api.error_handlers import (
    REQUEST_VALIDATION_ERROR_RESPONSES,
    raise_api_error,
)
from app.backends.provider_registry import BackendProviderLookupError
from app.prompts.prompt_errors import PromptRenderError
from app.registry.manager import RegistryManagerNotInitializedError
from app.schemas.title_generation import (
    GenerateTitleRequest,
    GenerateTitleResponse,
)
from app.services.title_generation_pipeline import (
    TitleBackendResultError,
    TitleGenerationPipeline,
)


router = APIRouter(tags=["title-generation"])


@router.post(
    "/titles",
    response_model=GenerateTitleResponse,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def generate_title(
    request: GenerateTitleRequest,
    pipeline: Annotated[
        TitleGenerationPipeline,
        Depends(get_title_generation_pipeline),
    ],
) -> GenerateTitleResponse:
    """요청이 선택한 로컬 LLM으로 저장 가능한 대화 제목을 만듭니다."""

    try:
        return await pipeline.generate_title(
            text=request.text,
            llm_deployment_id=request.llm_deployment_id,
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
    except PromptRenderError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="TITLE_PROMPT_RENDER_FAILED",
            message="제목 생성 Prompt를 렌더링할 수 없습니다",
        )
    except TitleBackendResultError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="TITLE_BACKEND_RESULT_INVALID",
            message="Backend 결과가 제목 생성 응답 계약과 다릅니다",
        )


__all__ = ["router"]
