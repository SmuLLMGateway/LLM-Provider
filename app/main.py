from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api.adapters import router as adapters_router
from app.api.deployments import router as deployments_router
from app.api.detection import router as detection_router
from app.api.error_handlers import register_error_handlers
from app.api.generation import router as generation_router
from app.api.masking import router as masking_router
from app.api.policies import router as policies_router
from app.api.title_generation import (
    router as title_generation_router,
)
from app.core.application_runtime import (
    ApplicationRuntime,
    application_runtime,
)


class HealthResponse(BaseModel):
    status: Literal["ok"]


RuntimeFactory = Callable[
    [],
    AbstractAsyncContextManager[ApplicationRuntime],
]
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def create_app(
    runtime_factory: RuntimeFactory = application_runtime,
) -> FastAPI:
    """주입 가능한 Runtime factory로 FastAPI 애플리케이션을 만듭니다."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """공유 Runtime을 시작 시 조립하고 종료 시 정리합니다."""

        async with runtime_factory() as runtime:
            application.state.runtime = runtime
            try:
                yield
            finally:
                del application.state.runtime

    application = FastAPI(
        title="LPL Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    register_error_handlers(application)
    application.include_router(adapters_router)
    application.include_router(deployments_router)
    application.include_router(detection_router)
    application.include_router(generation_router)
    application.include_router(masking_router)
    application.include_router(policies_router)
    application.include_router(title_generation_router)
    application.mount(
        "/ui",
        StaticFiles(directory=FRONTEND_DIR, html=True),
        name="test-ui",
    )

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["system"],
    )
    async def health() -> HealthResponse:
        """프로세스가 HTTP 요청을 받을 수 있는지 반환합니다."""

        return HealthResponse(status="ok")

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
    )
