"""LLM Backend 공통 인터페이스와 구현체를 제공합니다."""

from app.backends.llm.base import (
    LlmBackend,
    LlmMessage,
    LlmMessages,
    LlmOutputSchema,
    LlmParameters,
    LlmResult,
)
from app.backends.llm.mock import (
    DEFAULT_MOCK_LLM_RESULT,
    MockLlmBackend,
    MockLlmConfigurationError,
)
from app.backends.llm.openai_compatible import (
    DEFAULT_MAX_RESPONSE_BYTES,
    OpenAICompatibleBackendError,
    OpenAICompatibleBackendErrorCode,
    OpenAICompatibleConfigurationError,
    OpenAICompatibleHttpError,
    OpenAICompatibleLlmBackend,
    OpenAICompatibleRequestError,
    OpenAICompatibleResponseError,
    OpenAICompatibleResponseTooLargeError,
    OpenAICompatibleTimeoutError,
)

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MOCK_LLM_RESULT",
    "LlmBackend",
    "LlmMessage",
    "LlmMessages",
    "LlmOutputSchema",
    "LlmParameters",
    "LlmResult",
    "MockLlmBackend",
    "MockLlmConfigurationError",
    "OpenAICompatibleBackendError",
    "OpenAICompatibleBackendErrorCode",
    "OpenAICompatibleConfigurationError",
    "OpenAICompatibleHttpError",
    "OpenAICompatibleLlmBackend",
    "OpenAICompatibleRequestError",
    "OpenAICompatibleResponseError",
    "OpenAICompatibleResponseTooLargeError",
    "OpenAICompatibleTimeoutError",
]
