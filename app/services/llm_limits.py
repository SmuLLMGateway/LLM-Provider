"""LLM Deployment의 모델·실행 컨텍스트 한도를 안전하게 조회합니다."""

from __future__ import annotations

import asyncio
from typing import Protocol

import httpx

from app.core.json_codec import (
    StrictJsonDecodeError,
    StrictJsonEncodeError,
    dump_json_utf8,
    load_strict_json,
)
from app.schemas.deployments import (
    LlmDeploymentLimitsResponse,
    LlmLimitsSource,
)
from app.schemas.registry import DeploymentConfig
from app.services.registry_snapshot_provider import RegistrySnapshotProvider


DEFAULT_MAX_LIMITS_RESPONSE_BYTES = 1_048_576


class LlmLimitsNotFoundError(LookupError):
    """조회할 LLM Deployment가 없거나 종류가 다르면 발생합니다."""

    code = "DEPLOYMENT_NOT_FOUND"

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(f"{self.code}: {deployment_id}")


class LlmLimitsProvider(Protocol):
    """Generation Pipeline이 같은 실행 계획의 한도를 조회하는 계약입니다."""

    async def resolve_limits(
        self,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> LlmDeploymentLimitsResponse:
        """검증된 LLM Deployment의 유효 한도를 반환합니다."""

        ...


class LlmLimitsService:
    """Registry 설정과 선택적인 Ollama Native API 정보를 조합합니다."""

    def __init__(
        self,
        *,
        registry_manager: RegistrySnapshotProvider,
        http_client: httpx.AsyncClient,
        max_response_bytes: int = DEFAULT_MAX_LIMITS_RESPONSE_BYTES,
    ) -> None:
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes는 1 이상의 정수여야 합니다")
        self._registry_manager = registry_manager
        self._http_client = http_client
        self._max_response_bytes = max_response_bytes

    async def get_limits(
        self,
        *,
        deployment_id: str,
    ) -> LlmDeploymentLimitsResponse:
        """현재 Snapshot에서 LLM Deployment를 찾아 한도를 반환합니다."""

        snapshot = self._registry_manager.capture()
        deployment = snapshot.deployments.get(deployment_id)
        if deployment is None or deployment.kind != "llm":
            raise LlmLimitsNotFoundError(deployment_id)
        return await self.resolve_limits(
            deployment_id=deployment_id,
            deployment=deployment,
        )

    async def resolve_limits(
        self,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> LlmDeploymentLimitsResponse:
        """Registry 값과 조회 가능한 Ollama 한도 중 가장 작은 값을 사용합니다."""

        if deployment.kind != "llm":
            raise LlmLimitsNotFoundError(deployment_id)

        model_limit: int | None = None
        runtime_limit: int | None = None
        native_urls = _ollama_native_urls(deployment)
        if native_urls is not None and deployment.model_name is not None:
            show_url, ps_url = native_urls
            show_data, ps_data = await asyncio.gather(
                self._read_json(
                    "POST",
                    show_url,
                    payload={
                        "model": deployment.model_name,
                        "verbose": False,
                    },
                ),
                self._read_json("GET", ps_url),
            )
            model_limit = _parse_ollama_model_limit(show_data)
            runtime_limit = _parse_ollama_runtime_limit(
                ps_data,
                model_name=deployment.model_name,
            )

        candidates: list[tuple[int, LlmLimitsSource]] = []
        if deployment.context_window_tokens is not None:
            candidates.append(
                (deployment.context_window_tokens, "registry")
            )
        if runtime_limit is not None:
            candidates.append((runtime_limit, "ollama_runtime"))
        if model_limit is not None:
            candidates.append((model_limit, "ollama_model"))

        effective: int | None = None
        source: LlmLimitsSource = "unknown"
        if candidates:
            effective, source = min(candidates, key=lambda item: item[0])

        return LlmDeploymentLimitsResponse(
            deploymentId=deployment_id,
            modelContextWindowTokens=model_limit,
            runtimeContextWindowTokens=runtime_limit,
            effectiveContextWindowTokens=effective,
            source=source,
        )

    async def _read_json(
        self,
        method: str,
        url: httpx.URL,
        *,
        payload: dict[str, object] | None = None,
    ) -> object | None:
        """자동 조회 실패를 실행 오류로 승격하지 않고 크기 제한 JSON만 읽습니다."""

        content: bytes | None = None
        if payload is not None:
            try:
                content = dump_json_utf8(payload)
            except StrictJsonEncodeError:
                return None

        try:
            async with self._http_client.stream(
                method,
                url,
                content=content,
                headers=(
                    {"content-type": "application/json"}
                    if content is not None
                    else None
                ),
                timeout=2.0,
            ) as response:
                if not response.is_success:
                    return None
                response_body = bytearray()
                async for chunk in response.aiter_bytes():
                    if (
                        len(response_body) + len(chunk)
                        > self._max_response_bytes
                    ):
                        return None
                    response_body.extend(chunk)
        except httpx.HTTPError:
            return None

        try:
            return load_strict_json(bytes(response_body))
        except StrictJsonDecodeError:
            return None


def _ollama_native_urls(
    deployment: DeploymentConfig,
) -> tuple[httpx.URL, httpx.URL] | None:
    """Ollama의 OpenAI 호환 `/v1` 주소에서 Native 조회 URL을 만듭니다."""

    if deployment.base_url is None:
        return None
    base_url = httpx.URL(deployment.base_url)
    if base_url.path.rstrip("/") != "/v1":
        return None
    host = (base_url.host or "").lower()
    if base_url.port != 11434 and "ollama" not in host:
        return None
    return (
        base_url.copy_with(path="/api/show", query=None, fragment=None),
        base_url.copy_with(path="/api/ps", query=None, fragment=None),
    )


def _positive_integer(value: object) -> int | None:
    """bool을 제외한 양의 정수만 한도 값으로 사용합니다."""

    return value if type(value) is int and value > 0 else None


def _parse_ollama_model_limit(data: object) -> int | None:
    """`/api/show`의 architecture별 context_length 최댓값을 반환합니다."""

    if type(data) is not dict:
        return None
    model_info = data.get("model_info")
    if type(model_info) is not dict:
        return None
    limits = tuple(
        limit
        for key, value in model_info.items()
        if type(key) is str
        and key.endswith(".context_length")
        and (limit := _positive_integer(value)) is not None
    )
    return max(limits) if limits else None


def _parse_ollama_runtime_limit(
    data: object,
    *,
    model_name: str,
) -> int | None:
    """`/api/ps`에서 선택한 실행 모델의 context_length를 반환합니다."""

    if type(data) is not dict:
        return None
    models = data.get("models")
    if type(models) is not list:
        return None
    for item in models:
        if type(item) is not dict:
            continue
        if model_name not in {item.get("name"), item.get("model")}:
            continue
        return _positive_integer(item.get("context_length"))
    return None


__all__ = [
    "DEFAULT_MAX_LIMITS_RESPONSE_BYTES",
    "LlmLimitsNotFoundError",
    "LlmLimitsProvider",
    "LlmLimitsService",
]
