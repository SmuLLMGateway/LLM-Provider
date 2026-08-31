"""FastAPI 요청에서 애플리케이션 공유 실행 객체를 조회합니다."""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException, Request, status

from app.core.application_runtime import ApplicationRuntime
from app.services.adapter_catalog import AdapterCatalogService
from app.services.deployment_probe import DeploymentProbeService
from app.services.deployment_catalog import DeploymentCatalogService
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.detection_pipeline import DetectionPipeline
from app.services.generation_pipeline import GenerationPipeline
from app.services.masking_pipeline import MaskingPipeline
from app.services.llm_limits import LlmLimitsService
from app.services.policy_settings import PolicySettingsManager
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


def get_application_runtime(request: Request) -> ApplicationRuntime:
    """Lifespan에서 초기화한 ApplicationRuntime을 반환합니다."""

    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "APPLICATION_RUNTIME_UNAVAILABLE",
                "message": "애플리케이션 실행 구성이 준비되지 않았습니다",
            },
        )
    return cast(ApplicationRuntime, runtime)


def get_adapter_catalog(
    request: Request,
) -> AdapterCatalogService:
    """현재 Runtime과 함께 조립된 Adapter 카탈로그를 반환합니다."""

    return get_application_runtime(request).adapter_catalog_service


def get_generation_pipeline(request: Request) -> GenerationPipeline:
    """현재 애플리케이션의 공유 GenerationPipeline을 반환합니다."""

    return get_application_runtime(request).generation_pipeline


def get_detection_pipeline(request: Request) -> DetectionPipeline:
    """현재 애플리케이션의 공유 DetectionPipeline을 반환합니다."""

    return get_application_runtime(request).detection_pipeline


def get_title_generation_pipeline(
    request: Request,
) -> TitleGenerationPipeline:
    """현재 애플리케이션의 공유 TitleGenerationPipeline을 반환합니다."""

    return get_application_runtime(request).title_generation_pipeline


def get_masking_pipeline(request: Request) -> MaskingPipeline:
    """현재 애플리케이션의 공유 MaskingPipeline을 반환합니다."""

    return get_application_runtime(request).masking_pipeline


def get_deployment_catalog(
    request: Request,
) -> DeploymentCatalogService:
    """현재 Runtime의 Registry를 사용하는 읽기 전용 카탈로그를 만듭니다."""

    return DeploymentCatalogService(
        get_application_runtime(request).registry_manager
    )


def get_deployment_probe(
    request: Request,
) -> DeploymentProbeService:
    """현재 Runtime의 Registry와 Provider로 Probe 서비스를 조립합니다."""

    runtime = get_application_runtime(request)
    return DeploymentProbeService(
        registry_manager=runtime.registry_manager,
        backend_providers=runtime.backend_providers,
    )


def get_llm_limits_service(
    request: Request,
) -> LlmLimitsService:
    """Runtime 전체가 공유하는 LLM 한도 조회 서비스를 반환합니다."""

    service = get_application_runtime(request).llm_limits_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "LLM_LIMITS_UNAVAILABLE",
                "message": "LLM 한도 조회 구성이 준비되지 않았습니다",
            },
        )
    return service


def get_deployment_management(
    request: Request,
) -> DeploymentManagementService:
    """Runtime 전체가 공유하는 Deployment 관리 서비스를 반환합니다."""

    return get_application_runtime(
        request
    ).deployment_management_service


def get_policy_settings_manager(
    request: Request,
) -> PolicySettingsManager:
    """Runtime 전체가 공유하는 활성 정책 설정 Manager를 반환합니다."""

    manager = get_application_runtime(request).policy_settings_manager
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "POLICY_SETTINGS_NOT_INITIALIZED",
                "message": "활성 정책 설정이 아직 준비되지 않았습니다",
            },
        )
    return manager


__all__ = [
    "get_adapter_catalog",
    "get_application_runtime",
    "get_deployment_catalog",
    "get_deployment_management",
    "get_deployment_probe",
    "get_detection_pipeline",
    "get_generation_pipeline",
    "get_llm_limits_service",
    "get_masking_pipeline",
    "get_policy_settings_manager",
    "get_title_generation_pipeline",
]
