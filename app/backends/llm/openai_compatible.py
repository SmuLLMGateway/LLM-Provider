"""OpenAI Chat Completions 호환 서버를 호출하는 LLM Backend입니다."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import ValidationError

from app.backends.errors import (
    BackendConfigurationError,
    BackendError,
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
)
from app.backends.llm.base import (
    LlmMessages,
    LlmOutputSchema,
    LlmParameters,
)
from app.backends.llm.input_validation import (
    LlmInputValidationError,
    normalize_llm_inputs,
)
from app.backends.validation import (
    BackendDeploymentValidationError,
    validate_runnable_deployment,
)
from app.core.json_codec import (
    StrictJsonDecodeError,
    StrictJsonEncodeError,
    dump_json_utf8,
    load_strict_json,
)
from app.schemas.generation import LlmResult, LlmTokenUsage
from app.schemas.registry import DeploymentConfig


OpenAICompatibleBackendErrorCode = Literal[
    "OPENAI_COMPATIBLE_CONFIG_INVALID",
    "OPENAI_COMPATIBLE_REQUEST_FAILED",
    "OPENAI_COMPATIBLE_TIMEOUT",
    "OPENAI_COMPATIBLE_HTTP_ERROR",
    "OPENAI_COMPATIBLE_RESPONSE_INVALID",
    "OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE",
]

DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_ADAPTER_TYPE = "openai_compatible"
_CHAT_COMPLETIONS_PATH = "/chat/completions"
_RESERVED_PARAMETERS = frozenset(
    {"model", "messages", "stream", "n", "response_format"}
)


class OpenAICompatibleBackendError(BackendError):
    """OpenAI 호환 Backend의 안전하게 정규화된 기본 오류입니다."""

    def __init__(
        self,
        code: OpenAICompatibleBackendErrorCode,
        detail: str,
    ) -> None:
        super().__init__(code, detail)


class OpenAICompatibleConfigurationError(
    BackendConfigurationError,
    OpenAICompatibleBackendError,
):
    """Deployment 또는 호출 파라미터가 실행 계약과 다르면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            "OPENAI_COMPATIBLE_CONFIG_INVALID",
            detail,
        )


class OpenAICompatibleRequestError(
    BackendTransportError,
    OpenAICompatibleBackendError,
):
    """모델 서버로 HTTP 요청을 전송하지 못하면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "OPENAI_COMPATIBLE_REQUEST_FAILED",
            "모델 서버에 요청을 전송하지 못했습니다",
        )


class OpenAICompatibleTimeoutError(
    BackendTimeoutError,
    OpenAICompatibleBackendError,
):
    """Deployment에 설정된 제한시간 안에 요청이 끝나지 않으면 발생합니다."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(
            "OPENAI_COMPATIBLE_TIMEOUT",
            f"모델 서버 요청이 {timeout_ms}ms를 초과했습니다",
        )


class OpenAICompatibleHttpError(
    BackendResponseError,
    OpenAICompatibleBackendError,
):
    """모델 서버가 성공이 아닌 HTTP 상태를 반환하면 발생합니다."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            "OPENAI_COMPATIBLE_HTTP_ERROR",
            f"모델 서버가 HTTP {status_code} 상태를 반환했습니다",
        )


class OpenAICompatibleResponseError(
    BackendResponseError,
    OpenAICompatibleBackendError,
):
    """모델 서버 응답을 공통 LLM 결과로 변환할 수 없으면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            "OPENAI_COMPATIBLE_RESPONSE_INVALID",
            detail,
        )


class OpenAICompatibleResponseTooLargeError(
    BackendResponseError,
    OpenAICompatibleBackendError,
):
    """모델 서버의 디코딩된 응답이 허용 크기를 넘으면 발생합니다."""

    def __init__(self, max_response_bytes: int) -> None:
        self.max_response_bytes = max_response_bytes
        super().__init__(
            "OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE",
            "모델 서버 응답이 허용된 크기를 초과했습니다",
        )


class OpenAICompatibleLlmBackend:
    """OpenAI 호환 Chat Completions API를 공통 LLM 계약으로 변환합니다."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError(
                "max_response_bytes는 1 이상의 정수여야 합니다"
            )
        self._client = client
        self._max_response_bytes = max_response_bytes

    @property
    def max_response_bytes(self) -> int:
        """디코딩된 모델 응답에 적용할 최대 byte 크기를 반환합니다."""

        return self._max_response_bytes

    async def generate(
        self,
        messages: LlmMessages,
        deployment: DeploymentConfig,
        parameters: LlmParameters,
        output_schema: LlmOutputSchema | None = None,
    ) -> LlmResult:
        """호환 서버를 호출하고 첫 번째 Chat Completion을 정규화합니다."""

        endpoint, model_name, timeout_ms = self._resolve_deployment(
            deployment
        )
        payload = self._build_payload(
            model_name=model_name,
            messages=messages,
            parameters=parameters,
            output_schema=output_schema,
        )
        request_body = self._serialize_payload(payload)
        response_body = await self._request(
            endpoint=endpoint,
            request_body=request_body,
            timeout_ms=timeout_ms,
        )
        data = self._decode_response(response_body)
        return self._normalize_response(data, model_name)

    async def _request(
        self,
        *,
        endpoint: httpx.URL,
        request_body: bytes,
        timeout_ms: int,
    ) -> bytes:
        """응답 크기를 제한하며 요청하고 외부 예외 참조를 제거합니다."""

        failure: OpenAICompatibleBackendError | None = None
        try:
            async with self._client.stream(
                "POST",
                endpoint,
                content=request_body,
                headers={"content-type": "application/json"},
                timeout=timeout_ms / 1_000,
            ) as response:
                if not response.is_success:
                    raise OpenAICompatibleHttpError(
                        response.status_code
                    )

                response_body = bytearray()
                async for chunk in response.aiter_bytes():
                    if (
                        len(response_body) + len(chunk)
                        > self._max_response_bytes
                    ):
                        raise OpenAICompatibleResponseTooLargeError(
                            self._max_response_bytes
                        )
                    response_body.extend(chunk)
                return bytes(response_body)
        except httpx.TimeoutException:
            failure = OpenAICompatibleTimeoutError(timeout_ms)
        except httpx.RequestError:
            failure = OpenAICompatibleRequestError()

        if failure is not None:
            raise failure from None
        raise RuntimeError("도달할 수 없는 OpenAI 호환 요청 상태입니다")

    @staticmethod
    def _resolve_deployment(
        deployment: DeploymentConfig,
    ) -> tuple[httpx.URL, str, int]:
        """직접 호출에서도 Deployment 실행 조건을 확인하고 URL을 만듭니다."""

        try:
            validate_runnable_deployment(
                deployment,
                expected_kind="llm",
                expected_adapter_type=_ADAPTER_TYPE,
            )
        except BackendDeploymentValidationError as error:
            raise OpenAICompatibleConfigurationError(str(error)) from None
        if deployment.base_url is None:
            raise OpenAICompatibleConfigurationError(
                "Deployment baseUrl이 필요합니다"
            )
        if deployment.model_name is None:
            raise OpenAICompatibleConfigurationError(
                "Deployment modelName이 필요합니다"
            )
        if deployment.timeout_ms is None:
            raise OpenAICompatibleConfigurationError(
                "Deployment timeoutMs가 필요합니다"
            )

        base_url = httpx.URL(deployment.base_url)
        if base_url.query or base_url.fragment:
            raise OpenAICompatibleConfigurationError(
                "Deployment baseUrl에는 query나 fragment를 사용할 수 없습니다"
            )
        if base_url.username or base_url.password:
            raise OpenAICompatibleConfigurationError(
                "Deployment baseUrl에 인증정보를 포함할 수 없습니다"
            )

        endpoint_text = deployment.base_url.rstrip("/")
        if not endpoint_text.endswith(_CHAT_COMPLETIONS_PATH):
            endpoint_text = (
                f"{endpoint_text}{_CHAT_COMPLETIONS_PATH}"
            )

        return (
            httpx.URL(endpoint_text),
            deployment.model_name,
            deployment.timeout_ms,
        )

    @staticmethod
    def _build_payload(
        *,
        model_name: str,
        messages: LlmMessages,
        parameters: LlmParameters,
        output_schema: LlmOutputSchema | None,
    ) -> dict[str, object]:
        """공통 호출 인자를 비스트리밍 Chat Completions 요청으로 변환합니다."""

        normalized_inputs: tuple[
            LlmMessages,
            LlmParameters,
            LlmOutputSchema | None,
        ] | None = None
        input_error_detail: str | None = None
        try:
            normalized_inputs = normalize_llm_inputs(
                messages,
                parameters,
                output_schema,
            )
        except LlmInputValidationError as error:
            input_error_detail = str(error)

        if input_error_detail is not None:
            raise OpenAICompatibleConfigurationError(input_error_detail)
        if normalized_inputs is None:
            raise RuntimeError("LLM 입력 정규화 결과가 없습니다")
        (
            normalized_messages,
            normalized_parameters,
            normalized_output_schema,
        ) = normalized_inputs

        conflicts = _RESERVED_PARAMETERS & normalized_parameters.keys()
        if conflicts:
            raise OpenAICompatibleConfigurationError(
                "parameters에서 Adapter 예약 필드를 사용할 수 없습니다: "
                f"{', '.join(sorted(conflicts))}"
            )

        payload: dict[str, object] = normalized_parameters
        payload.update(
            {
                "model": model_name,
                "messages": normalized_messages,
                "stream": False,
            }
        )

        if normalized_output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "lpl_response",
                    "strict": True,
                    "schema": normalized_output_schema,
                },
            }

        return payload

    @staticmethod
    def _serialize_payload(payload: dict[str, object]) -> bytes:
        """요청 입력만 별도로 JSON 직렬화하고 실패 원인을 보존하지 않습니다."""

        serialized = b""
        serialization_failed = False
        try:
            serialized = dump_json_utf8(payload)
        except StrictJsonEncodeError:
            serialization_failed = True

        if serialization_failed:
            del payload
            raise OpenAICompatibleConfigurationError(
                "messages, parameters와 output_schema는 JSON으로 "
                "직렬화할 수 있어야 합니다"
            ) from None
        return serialized

    @staticmethod
    def _decode_response(response_body: bytes) -> object:
        """응답 JSON 파싱 오류가 원문을 예외에 보존하지 않게 변환합니다."""

        data: object = None
        decoding_failed = False
        try:
            data = load_strict_json(response_body)
        except StrictJsonDecodeError:
            decoding_failed = True

        if decoding_failed:
            del response_body
            raise OpenAICompatibleResponseError(
                "모델 서버 응답이 유효한 UTF-8 JSON이 아닙니다"
            ) from None
        return data

    @classmethod
    def _normalize_response(
        cls,
        data: object,
        fallback_model_name: str,
    ) -> LlmResult:
        """비신뢰 Provider 응답을 엄격한 공통 LLM 결과로 변환합니다."""

        if not isinstance(data, dict):
            raise OpenAICompatibleResponseError(
                "응답의 최상위 값은 객체여야 합니다"
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAICompatibleResponseError(
                "응답 choices는 비어 있지 않은 배열이어야 합니다"
            )

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise OpenAICompatibleResponseError(
                "응답의 첫 번째 choice는 객체여야 합니다"
            )

        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise OpenAICompatibleResponseError(
                "응답 choice.message는 객체여야 합니다"
            )

        content = message.get("content")
        if not isinstance(content, str):
            raise OpenAICompatibleResponseError(
                "응답 choice.message.content는 문자열이어야 합니다"
            )

        model_name = cls._optional_non_empty_string(
            data,
            "model",
            fallback=fallback_model_name,
        )
        finish_reason = cls._optional_non_empty_string(
            first_choice,
            "finish_reason",
        )
        usage = cls._normalize_usage(data.get("usage"))

        validation_failed = False
        try:
            result = LlmResult(
                text=content,
                model_name=model_name,
                finish_reason=finish_reason,
                usage=usage,
            )
        except ValidationError:
            validation_failed = True

        if validation_failed:
            raise OpenAICompatibleResponseError(
                "응답을 공통 LLM 결과로 변환할 수 없습니다"
            ) from None
        return result

    @staticmethod
    def _optional_non_empty_string(
        data: dict[object, object],
        field_name: str,
        *,
        fallback: str | None = None,
    ) -> str | None:
        """선택 문자열의 누락과 null만 허용하고 나머지 타입을 검증합니다."""

        value = data.get(field_name)
        if value is None:
            return fallback
        if not isinstance(value, str) or not value:
            raise OpenAICompatibleResponseError(
                f"응답 {field_name}은 비어 있지 않은 문자열이어야 합니다"
            )
        return value

    @staticmethod
    def _normalize_usage(data: object) -> LlmTokenUsage | None:
        """OpenAI 토큰 필드를 LPL의 공통 토큰 사용량으로 변환합니다."""

        if data is None:
            return None
        if not isinstance(data, dict):
            raise OpenAICompatibleResponseError(
                "응답 usage는 객체이거나 null이어야 합니다"
            )

        validation_failed = False
        try:
            usage = LlmTokenUsage(
                input_tokens=data["prompt_tokens"],
                output_tokens=data["completion_tokens"],
                total_tokens=data["total_tokens"],
            )
        except (KeyError, ValidationError):
            validation_failed = True

        if validation_failed:
            raise OpenAICompatibleResponseError(
                "응답 usage의 토큰 필드가 유효하지 않습니다"
            ) from None
        return usage


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
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
