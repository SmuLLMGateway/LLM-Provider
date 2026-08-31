"""LLM Deployment 컨텍스트 한도 조회와 Ollama fallback을 검증합니다."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.schemas.registry import DeploymentConfig
from app.services.llm_limits import (
    LlmLimitsNotFoundError,
    LlmLimitsService,
)


class StaticRegistryManager:
    """고정 Deployment 맵을 Snapshot처럼 반환합니다."""

    def __init__(self, deployments: dict[str, DeploymentConfig]) -> None:
        self.snapshot = SimpleNamespace(deployments=deployments)
        self.capture_calls = 0

    def capture(self):
        self.capture_calls += 1
        return self.snapshot


def _deployment(
    *,
    base_url: str = "http://ollama:11434/v1",
    model_name: str = "qwen3:8b",
    context_window_tokens: int | None = None,
) -> DeploymentConfig:
    """테스트용 OpenAI 호환 LLM Deployment를 만듭니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": "llm",
            "adapterType": "openai_compatible",
            "baseUrl": base_url,
            "modelName": model_name,
            "timeoutMs": 5000,
            "contextWindowTokens": context_window_tokens,
            "enabled": True,
        }
    )


@pytest.mark.asyncio
async def test_limits_uses_registry_without_native_ollama_path() -> None:
    """Native 조회가 아닌 서버는 Registry 설정만으로 제한을 반환합니다."""

    deployment = _deployment(
        base_url="http://vllm:8000/v1",
        context_window_tokens=32768,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await LlmLimitsService(
            registry_manager=StaticRegistryManager({"llm-a": deployment}),
            http_client=client,
        ).get_limits(deployment_id="llm-a")

    assert requests == []
    assert result.model_context_window_tokens is None
    assert result.runtime_context_window_tokens is None
    assert result.effective_context_window_tokens == 32768
    assert result.source == "registry"


@pytest.mark.asyncio
async def test_limits_combines_ollama_model_runtime_and_registry() -> None:
    """Ollama 모델·실행·Registry 값 중 가장 작은 실제 한도를 선택합니다."""

    deployment = _deployment(
        context_window_tokens=32768,
    )
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/show":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={
                    "model_info": {
                        "qwen.context_length": 131072,
                    }
                },
            )
        if request.url.path == "/api/ps":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3:8b",
                            "context_length": 16384,
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await LlmLimitsService(
            registry_manager=StaticRegistryManager({"llm-a": deployment}),
            http_client=client,
        ).get_limits(deployment_id="llm-a")

    assert set(paths) == {"/api/show", "/api/ps"}
    assert result.model_context_window_tokens == 131072
    assert result.runtime_context_window_tokens == 16384
    assert result.effective_context_window_tokens == 16384
    assert result.source == "ollama_runtime"


@pytest.mark.asyncio
async def test_limits_returns_unknown_when_discovery_and_registry_are_empty() -> None:
    """자동 조회 실패는 실행 오류가 아니라 명시적인 unknown 응답입니다."""

    deployment = _deployment()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await LlmLimitsService(
            registry_manager=StaticRegistryManager({"llm-a": deployment}),
            http_client=client,
        ).get_limits(deployment_id="llm-a")

    assert result.model_context_window_tokens is None
    assert result.runtime_context_window_tokens is None
    assert result.effective_context_window_tokens is None
    assert result.source == "unknown"


@pytest.mark.asyncio
async def test_limits_rejects_unknown_or_non_llm_deployment() -> None:
    """없는 ID와 NER ID는 같은 안전한 조회 실패로 처리합니다."""

    ner = DeploymentConfig.model_validate(
        {
            "kind": "ner",
            "adapterType": "http_ner",
            "baseUrl": "http://ner:8008/v1/ner/detect",
            "timeoutMs": 5000,
            "enabled": True,
        }
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        service = LlmLimitsService(
            registry_manager=StaticRegistryManager({"ner-a": ner}),
            http_client=client,
        )
        for deployment_id in ("missing", "ner-a"):
            with pytest.raises(LlmLimitsNotFoundError):
                await service.get_limits(deployment_id=deployment_id)
