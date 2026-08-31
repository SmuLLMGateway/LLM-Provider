"""LPL 공통 HTTP NER 계약을 제공하는 GLiNER 애플리케이션입니다."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .contracts import (
    HealthResponse,
    NerRequest,
    NerResponse,
)
from .inference import (
    GlinerInferenceService,
    NerInferenceError,
    NerInputTooLongError,
    load_inference_service,
)
from .settings import (
    GlinerServerSettings,
)


InferenceServiceLoader = Callable[
    [GlinerServerSettings],
    GlinerInferenceService,
]
router = APIRouter()


def _get_inference_service(request: Request) -> GlinerInferenceService:
    """lifespan에서 준비한 공유 추론 서비스를 반환합니다."""

    service = getattr(request.app.state, "inference_service", None)
    if not isinstance(service, GlinerInferenceService):
        raise RuntimeError("NER 추론 서비스가 준비되지 않았습니다")
    return service


@asynccontextmanager
async def application_lifespan(
    application: FastAPI,
) -> AsyncIterator[None]:
    """프로세스 시작 시 모델을 한 번 로드하고 종료 시 참조를 제거합니다."""

    settings = application.state.gliner_settings
    loader = application.state.inference_service_loader
    service = await run_in_threadpool(loader, settings)
    application.state.inference_service = service
    try:
        yield
    finally:
        del application.state.inference_service


async def _handle_request_validation_error(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """Pydantic 오류에 사용자 원문을 다시 포함하지 않습니다."""

    del request, error
    return JSONResponse(
        status_code=422,
        content={
            "code": "NER_REQUEST_INVALID",
            "detail": "NER 요청 형식이 올바르지 않습니다",
        },
    )


async def _handle_input_too_long_error(
    request: Request,
    error: NerInputTooLongError,
) -> JSONResponse:
    """자동 truncation 대신 안전한 크기 오류를 반환합니다."""

    del request
    content: dict[str, object] = {
        "code": "NER_INPUT_TOO_LONG",
        "detail": "NER 입력이 모델의 전체 처리 한도를 초과했습니다",
    }
    if error.max_tokens is not None:
        content["maxTokens"] = error.max_tokens
    return JSONResponse(status_code=413, content=content)


async def _handle_inference_error(
    request: Request,
    error: NerInferenceError,
) -> JSONResponse:
    """모델 예외와 원문을 노출하지 않는 공통 오류를 반환합니다."""

    del request, error
    return JSONResponse(
        status_code=500,
        content={
            "code": "NER_INFERENCE_FAILED",
            "detail": "NER 모델 추론에 실패했습니다",
        },
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
)
def health(request: Request) -> HealthResponse:
    """모델 로딩과 추론 장치 준비가 끝난 상태를 반환합니다."""

    service = _get_inference_service(request)
    return HealthResponse(
        modelName=service.model_name,
        modelRevision=service.model_revision,
        device=service.device,
    )


@router.post(
    "/v1/ner/detect",
    response_model=NerResponse,
    tags=["ner"],
)
def detect(
    payload: NerRequest,
    request: Request,
) -> NerResponse:
    """요청 원문 전체를 서버의 고정 탐지 정책으로 처리합니다."""

    return _get_inference_service(request).detect(payload.text)


def create_app(
    *,
    settings: GlinerServerSettings | None = None,
    service_loader: InferenceServiceLoader = load_inference_service,
) -> FastAPI:
    """설정과 모델 Loader를 주입할 수 있는 애플리케이션을 만듭니다."""

    application = FastAPI(
        title="GLiNER NER Service",
        version="0.1.0",
        lifespan=application_lifespan,
    )
    application.state.gliner_settings = (
        settings or GlinerServerSettings.from_environment()
    )
    application.state.inference_service_loader = service_loader
    application.add_exception_handler(
        RequestValidationError,
        _handle_request_validation_error,
    )
    application.add_exception_handler(
        NerInputTooLongError,
        _handle_input_too_long_error,
    )
    application.add_exception_handler(
        NerInferenceError,
        _handle_inference_error,
    )
    application.include_router(router)

    return application


app = create_app()


__all__ = ["InferenceServiceLoader", "app", "create_app"]
