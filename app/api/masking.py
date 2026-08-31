"""Local LLM 기반 민감정보 마스킹 HTTP API를 제공합니다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_masking_pipeline
from app.api.error_handlers import (
    REQUEST_VALIDATION_ERROR_RESPONSES,
    raise_api_error,
)
from app.backends.provider_registry import BackendProviderLookupError
from app.policies.span_validator import DetectionSpanValidationError
from app.prompts.prompt_errors import PromptContextTooLargeError, PromptRenderError
from app.registry.manager import RegistryManagerNotInitializedError
from app.schemas.masking import MaskRequest, MaskResponse
from app.services.llm_masking_output_parser import LlmMaskingOutputError
from app.services.llm_masking_output_validator import LlmMaskingValidationError
from app.services.masking_pipeline import (
    MaskingBackendResultError,
    MaskingInputSerializationError,
    MaskingNamespaceError,
    MaskingPipeline,
)


router = APIRouter(tags=["masking"])


@router.post(
    "/mask",
    response_model=MaskResponse,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def mask(
    request: MaskRequest,
    pipeline: Annotated[MaskingPipeline, Depends(get_masking_pipeline)],
) -> MaskResponse:
    """선택한 Local LLM으로 Detection 전체를 마스킹하고 결과를 검증합니다."""

    try:
        return await pipeline.mask(
            text=request.text,
            llm_deployment_id=request.llm_deployment_id,
            detections=request.detections,
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
    except DetectionSpanValidationError:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="MASK_DETECTIONS_INVALID",
            message="마스킹 대상이 원문 Detection 계약과 다릅니다",
        )
    except LlmMaskingOutputError as error:
        raise_api_error(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=error.code,
            message="LLM 마스킹 출력이 응답 계약과 다릅니다",
        )
    except LlmMaskingValidationError as error:
        raise_api_error(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=error.code,
            message="LLM 마스킹 결과가 보안 검증을 통과하지 못했습니다",
        )
    except PromptContextTooLargeError:
        raise_api_error(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="MASK_REQUEST_TOO_LARGE",
            message="마스킹 요청이 처리 가능한 크기를 초과했습니다",
        )
    except PromptRenderError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="MASK_PROMPT_RENDER_FAILED",
            message="마스킹 Prompt를 렌더링할 수 없습니다",
        )
    except MaskingBackendResultError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="MASKING_BACKEND_RESULT_INVALID",
            message="Backend 결과가 마스킹 응답 계약과 다릅니다",
        )
    except (MaskingInputSerializationError, MaskingNamespaceError):
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="MASKING_INTERNAL_VALIDATION_FAILED",
            message="마스킹 실행 입력을 안전하게 준비할 수 없습니다",
        )


__all__ = ["router"]
