"""Deployment 조회와 안전한 온라인 관리 API를 제공합니다."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Path, Response, status
from pydantic import ValidationError

from app.api.dependencies import (
    get_deployment_catalog,
    get_deployment_management,
    get_deployment_probe,
    get_llm_limits_service,
)
from app.api.error_handlers import (
    REQUEST_VALIDATION_ERROR_RESPONSES,
    raise_api_error,
)
from app.api.request_validation import format_validation_error_message
from app.backends.provider_registry import BackendProviderLookupError
from app.policies.span_validator import DetectionSpanValidationError
from app.registry.manager import RegistryManagerNotInitializedError
from app.schemas.deployments import (
    DeploymentDetail,
    DeploymentEnabledUpdateRequest,
    DeploymentListResponse,
    DeploymentProbeResponse,
    DeploymentWriteRequest,
    LlmDeploymentCreateRequest,
    LlmDeploymentDetail,
    LlmDeploymentLimitsResponse,
    LlmDeploymentUpdateRequest,
    NerDeploymentCreateRequest,
    NerDeploymentDetail,
    NerDeploymentUpdateRequest,
)
from app.schemas.registry import DeploymentKind, ResourceId
from app.services.deployment_probe import (
    DeploymentProbeService,
    LlmProbeBackendResultError,
)
from app.services.deployment_catalog import (
    DeploymentCatalogNotFoundError,
    DeploymentCatalogService,
)
from app.services.deployment_management import DeploymentManagementService
from app.services.llm_limits import (
    LlmLimitsNotFoundError,
    LlmLimitsService,
)


router = APIRouter(prefix="/deployments", tags=["deployments"])


def _list_deployments(
    *,
    kind: DeploymentKind,
    catalog: DeploymentCatalogService,
) -> DeploymentListResponse:
    """한 종류의 Deployment 목록을 공통 오류 계약으로 조회합니다."""

    try:
        deployments = catalog.list_deployments(kind=kind)
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )
    return DeploymentListResponse(deployments=deployments)


def _get_deployment(
    *,
    deployment_id: str,
    kind: DeploymentKind,
    catalog: DeploymentCatalogService,
) -> DeploymentDetail:
    """ID와 종류가 일치하는 상세 정보를 공통 오류 계약으로 조회합니다."""

    try:
        return catalog.get_deployment(
            deployment_id,
            kind=kind,
        )
    except DeploymentCatalogNotFoundError:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="DEPLOYMENT_NOT_FOUND",
            message="요청한 Deployment를 찾을 수 없습니다",
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )


def _write_deployment(
    *,
    deployment_id: str,
    operation: Literal["add", "update"],
    request: DeploymentWriteRequest,
    management_service: DeploymentManagementService,
) -> DeploymentDetail:
    """경로별 요청 모델을 내부 설정으로 바꾸고 관리 오류를 처리합니다."""

    try:
        deployment = request.to_deployment_config()
    except ValidationError as error:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="REQUEST_VALIDATION_FAILED",
            message=format_validation_error_message(error),
        )

    try:
        if operation == "add":
            updated = management_service.add_deployment(
                deployment_id,
                deployment,
            )
        else:
            updated = management_service.update_deployment(
                deployment_id,
                deployment,
            )
    except ValidationError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DEPLOYMENT_STORAGE_FAILED",
            message="Deployment 설정 저장소 작업에 실패했습니다",
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )
    except OSError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DEPLOYMENT_STORAGE_FAILED",
            message="Deployment 설정 저장소 작업에 실패했습니다",
        )

    return DeploymentCatalogService.build_detail(
        deployment_id,
        updated,
    )


def _set_deployment_enabled(
    *,
    deployment_id: str,
    kind: DeploymentKind,
    request: DeploymentEnabledUpdateRequest,
    management_service: DeploymentManagementService,
) -> DeploymentDetail:
    """기존 실행 설정을 보존한 채 활성 상태만 안전하게 변경합니다."""

    try:
        updated = management_service.set_deployment_enabled(
            deployment_id,
            kind=kind,
            enabled=request.enabled,
        )
    except ValidationError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DEPLOYMENT_STORAGE_FAILED",
            message="Deployment 설정 저장소 작업에 실패했습니다",
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )
    except OSError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DEPLOYMENT_STORAGE_FAILED",
            message="Deployment 설정 저장소 작업에 실패했습니다",
        )

    return DeploymentCatalogService.build_detail(
        deployment_id,
        updated,
    )


def _delete_deployment(
    *,
    deployment_id: str,
    kind: DeploymentKind,
    management_service: DeploymentManagementService,
) -> Response:
    """비활성 Deployment를 안전하게 삭제하고 빈 204 응답을 반환합니다."""

    try:
        management_service.delete_deployment(
            deployment_id,
            kind=kind,
        )
    except ValidationError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DEPLOYMENT_STORAGE_FAILED",
            message="Deployment 설정 저장소 작업에 실패했습니다",
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )
    except OSError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="DEPLOYMENT_STORAGE_FAILED",
            message="Deployment 설정 저장소 작업에 실패했습니다",
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/ner",
    response_model=DeploymentListResponse,
    response_model_exclude_none=True,
)
async def list_ner_deployments(
    catalog: Annotated[
        DeploymentCatalogService,
        Depends(get_deployment_catalog),
    ],
) -> DeploymentListResponse:
    """NER Deployment 요약 목록을 ID 순서로 반환합니다."""

    return _list_deployments(kind="ner", catalog=catalog)


@router.post(
    "/ner",
    response_model=NerDeploymentDetail,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def add_ner_deployment(
    request: NerDeploymentCreateRequest,
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> NerDeploymentDetail:
    """새 NER Deployment를 저장하고 즉시 Active Snapshot에 반영합니다."""

    return cast(
        NerDeploymentDetail,
        _write_deployment(
            deployment_id=request.deployment_id,
            operation="add",
            request=request,
            management_service=management_service,
        ),
    )


@router.get(
    "/ner/{deployment_id}",
    response_model=NerDeploymentDetail,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def get_ner_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="조회할 NER Deployment ID"),
    ],
    catalog: Annotated[
        DeploymentCatalogService,
        Depends(get_deployment_catalog),
    ],
) -> NerDeploymentDetail:
    """ID와 종류가 일치하는 NER Deployment 상세를 반환합니다."""

    return cast(
        NerDeploymentDetail,
        _get_deployment(
            deployment_id=deployment_id,
            kind="ner",
            catalog=catalog,
        ),
    )


@router.put(
    "/ner/{deployment_id}",
    response_model=NerDeploymentDetail,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def update_ner_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="전체 설정을 교체할 NER Deployment ID"),
    ],
    request: NerDeploymentUpdateRequest,
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> NerDeploymentDetail:
    """기존 NER Deployment 전체 설정을 교체하고 즉시 활성화합니다."""

    return cast(
        NerDeploymentDetail,
        _write_deployment(
            deployment_id=deployment_id,
            operation="update",
            request=request,
            management_service=management_service,
        ),
    )


@router.delete(
    "/ner/{deployment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def delete_ner_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="삭제할 NER Deployment ID"),
    ],
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> Response:
    """비활성 NER Deployment를 삭제하고 즉시 반영합니다."""

    return _delete_deployment(
        deployment_id=deployment_id,
        kind="ner",
        management_service=management_service,
    )


@router.patch(
    "/ner/{deployment_id}/enabled",
    response_model=NerDeploymentDetail,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def set_ner_deployment_enabled(
    deployment_id: Annotated[
        ResourceId,
        Path(description="활성 상태를 변경할 NER Deployment ID"),
    ],
    request: DeploymentEnabledUpdateRequest,
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> NerDeploymentDetail:
    """기존 NER Deployment의 활성 상태만 변경하고 즉시 반영합니다."""

    return cast(
        NerDeploymentDetail,
        _set_deployment_enabled(
            deployment_id=deployment_id,
            kind="ner",
            request=request,
            management_service=management_service,
        ),
    )


@router.post(
    "/ner/{deployment_id}/probe",
    response_model=DeploymentProbeResponse,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def probe_ner_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="연결 상태를 검사할 NER Deployment ID"),
    ],
    probe_service: Annotated[
        DeploymentProbeService,
        Depends(get_deployment_probe),
    ],
) -> DeploymentProbeResponse:
    """고정된 최소 입력으로 NER Backend의 실제 요청·응답을 검사합니다."""

    try:
        return await probe_service.probe_ner(
            deployment_id=deployment_id,
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
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="NER_PROBE_RESULT_INVALID",
            message="NER Backend Probe 결과가 응답 계약과 다릅니다",
        )


@router.get(
    "/llm",
    response_model=DeploymentListResponse,
    response_model_exclude_none=True,
)
async def list_llm_deployments(
    catalog: Annotated[
        DeploymentCatalogService,
        Depends(get_deployment_catalog),
    ],
) -> DeploymentListResponse:
    """LLM Deployment 요약 목록을 ID 순서로 반환합니다."""

    return _list_deployments(kind="llm", catalog=catalog)


@router.post(
    "/llm",
    response_model=LlmDeploymentDetail,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def add_llm_deployment(
    request: LlmDeploymentCreateRequest,
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> LlmDeploymentDetail:
    """새 LLM Deployment를 저장하고 즉시 Active Snapshot에 반영합니다."""

    return cast(
        LlmDeploymentDetail,
        _write_deployment(
            deployment_id=request.deployment_id,
            operation="add",
            request=request,
            management_service=management_service,
        ),
    )


@router.get(
    "/llm/{deployment_id}",
    response_model=LlmDeploymentDetail,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def get_llm_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="조회할 LLM Deployment ID"),
    ],
    catalog: Annotated[
        DeploymentCatalogService,
        Depends(get_deployment_catalog),
    ],
) -> LlmDeploymentDetail:
    """ID와 종류가 일치하는 LLM Deployment 상세를 반환합니다."""

    return cast(
        LlmDeploymentDetail,
        _get_deployment(
            deployment_id=deployment_id,
            kind="llm",
            catalog=catalog,
        ),
    )


@router.get(
    "/llm/{deployment_id}/limits",
    response_model=LlmDeploymentLimitsResponse,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def get_llm_deployment_limits(
    deployment_id: Annotated[
        ResourceId,
        Path(description="한도를 조회할 LLM Deployment ID"),
    ],
    limits_service: Annotated[
        LlmLimitsService,
        Depends(get_llm_limits_service),
    ],
) -> LlmDeploymentLimitsResponse:
    """Registry와 모델 서버에서 확인한 LLM 컨텍스트 한도를 반환합니다."""

    try:
        return await limits_service.get_limits(
            deployment_id=deployment_id,
        )
    except LlmLimitsNotFoundError:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="DEPLOYMENT_NOT_FOUND",
            message="요청한 Deployment를 찾을 수 없습니다",
        )
    except RegistryManagerNotInitializedError:
        raise_api_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="REGISTRY_NOT_INITIALIZED",
            message="Registry가 아직 준비되지 않았습니다",
        )


@router.put(
    "/llm/{deployment_id}",
    response_model=LlmDeploymentDetail,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def update_llm_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="전체 설정을 교체할 LLM Deployment ID"),
    ],
    request: LlmDeploymentUpdateRequest,
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> LlmDeploymentDetail:
    """기존 LLM Deployment 전체 설정을 교체하고 즉시 활성화합니다."""

    return cast(
        LlmDeploymentDetail,
        _write_deployment(
            deployment_id=deployment_id,
            operation="update",
            request=request,
            management_service=management_service,
        ),
    )


@router.delete(
    "/llm/{deployment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def delete_llm_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="삭제할 LLM Deployment ID"),
    ],
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> Response:
    """비활성 LLM Deployment를 삭제하고 즉시 반영합니다."""

    return _delete_deployment(
        deployment_id=deployment_id,
        kind="llm",
        management_service=management_service,
    )


@router.patch(
    "/llm/{deployment_id}/enabled",
    response_model=LlmDeploymentDetail,
    response_model_exclude_none=True,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
def set_llm_deployment_enabled(
    deployment_id: Annotated[
        ResourceId,
        Path(description="활성 상태를 변경할 LLM Deployment ID"),
    ],
    request: DeploymentEnabledUpdateRequest,
    management_service: Annotated[
        DeploymentManagementService,
        Depends(get_deployment_management),
    ],
) -> LlmDeploymentDetail:
    """기존 LLM Deployment의 활성 상태만 변경하고 즉시 반영합니다."""

    return cast(
        LlmDeploymentDetail,
        _set_deployment_enabled(
            deployment_id=deployment_id,
            kind="llm",
            request=request,
            management_service=management_service,
        ),
    )


@router.post(
    "/llm/{deployment_id}/probe",
    response_model=DeploymentProbeResponse,
    responses=REQUEST_VALIDATION_ERROR_RESPONSES,
)
async def probe_llm_deployment(
    deployment_id: Annotated[
        ResourceId,
        Path(description="연결 상태를 검사할 LLM Deployment ID"),
    ],
    probe_service: Annotated[
        DeploymentProbeService,
        Depends(get_deployment_probe),
    ],
) -> DeploymentProbeResponse:
    """고정된 최소 입력으로 LLM Backend의 실제 요청·응답을 검사합니다."""

    try:
        return await probe_service.probe_llm(
            deployment_id=deployment_id,
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
    except LlmProbeBackendResultError:
        raise_api_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="LLM_PROBE_RESULT_INVALID",
            message="LLM Backend Probe 결과가 공통 계약과 다릅니다",
        )


__all__ = ["router"]
