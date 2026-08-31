"""요청이 선택한 NER·LLM 기반 개인정보 탐지 HTTP API를 제공합니다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_detection_pipeline
from app.api.error_handlers import (
    REQUEST_VALIDATION_ERROR_RESPONSES,
    raise_api_error,
)
from app.backends.provider_registry import BackendProviderLookupError
from app.policies.span_validator import DetectionSpanValidationError
from app.policies.organization_profile_validator import (
    OrganizationProfilePolicyError,
)
from app.registry.manager import RegistryManagerNotInitializedError
from app.prompts.prompt_errors import (
    PromptContextTooLargeError,
    PromptOutputTooLargeError,
    PromptRenderError,
)
from app.schemas.detection import DetectRequest, DetectResponse
from app.schemas.errors import ApiErrorResponse
from app.services.detection_pipeline import (
    DetectionBackendResultError,
    DetectionPipeline,
    DetectionPolicySelectionError,
)
from app.services.llm_detection_output_parser import (
    LlmDetectionOutputError,
)
from app.services.policy_settings import (
    PolicySettingsNotInitializedError,
)


router = APIRouter(tags=["detection"])
_DETECTION_ERROR_RESPONSES = {
    **REQUEST_VALIDATION_ERROR_RESPONSES,
    status.HTTP_413_CONTENT_TOO_LARGE: {
        "model": ApiErrorResponse,
        "description": "탐지 요청 또는 NER 입력이 처리 가능한 크기를 초과함",
    },
}


@router.post(
    "/detect",
    response_model=DetectResponse,
    responses=_DETECTION_ERROR_RESPONSES,
)
async def detect(
    request: DetectRequest,
    pipeline: Annotated[
        DetectionPipeline,
        Depends(get_detection_pipeline),
    ],
) -> DetectResponse:
    """요청에서 명시한 NER와 LLM Deployment로 탐지를 실행합니다."""

    try:
        return await pipeline.detect(
            text=request.text,
            ner_deployment_id=request.ner_deployment_id,
            llm_deployment_id=request.llm_deployment_id,
            organization_profile=request.organization_profile,
            source_type=request.source_type,
            regex_candidates=request.regex_candidates,
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )
    except PolicySettingsNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="POLICY_SETTINGS_NOT_INITIALIZED",
            message="활성 정책 설정이 아직 준비되지 않았습니다",
        )
    except DetectionPolicySelectionError:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="DETECTION_POLICY_DISABLED",
            message="Regex 후보가 비활성 정책을 참조합니다",
        )
    except OrganizationProfilePolicyError:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="ORGANIZATION_PROFILE_INCOMPLETE",
            message="활성 정책에 필요한 조직 기준정보가 누락되었습니다",
        )
    except BackendProviderLookupError as error:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=error.code,
            message="실행할 Backend Provider가 준비되지 않았습니다",
        )
    except LlmDetectionOutputError as error:
        raise_api_error(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code=error.code,
            message="LLM 탐지 출력이 응답 계약과 다릅니다",
        )
    except DetectionSpanValidationError:
        raise_api_error(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="DETECTION_RESULT_INVALID",
            message="Backend 탐지 결과가 응답 계약과 다릅니다",
        )
    except (
        PromptContextTooLargeError,
        PromptOutputTooLargeError,
    ):
        raise_api_error(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="DETECTION_REQUEST_TOO_LARGE",
            message="탐지 요청이 처리 가능한 크기를 초과했습니다",
        )
    except PromptRenderError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DETECTION_PROMPT_RENDER_FAILED",
            message="탐지 Prompt를 렌더링할 수 없습니다",
        )
    except DetectionBackendResultError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DETECTION_BACKEND_RESULT_INVALID",
            message="Backend 결과가 탐지 응답 계약과 다릅니다",
        )


__all__ = ["router"]
