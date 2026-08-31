"""LLM Backend 구현체가 따라야 하는 공통 인터페이스를 정의합니다."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.generation import LlmResult
from app.schemas.registry import DeploymentConfig


LlmMessage = dict[str, object]
LlmMessages = list[LlmMessage]
LlmParameters = dict[str, object]
LlmOutputSchema = dict[str, object]


@runtime_checkable
class LlmBackend(Protocol):
    """서로 다른 LLM API를 공통 generate 인터페이스로 변환합니다."""

    async def generate(
        self,
        messages: LlmMessages,
        deployment: DeploymentConfig,
        parameters: LlmParameters,
        output_schema: LlmOutputSchema | None = None,
    ) -> LlmResult:
        """메시지를 처리하고 정규화된 LLM 결과를 반환합니다."""

        ...


__all__ = [
    "LlmBackend",
    "LlmMessage",
    "LlmMessages",
    "LlmOutputSchema",
    "LlmParameters",
    "LlmResult",
]
