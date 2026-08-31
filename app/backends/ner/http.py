"""공통 HTTP 계약을 사용하는 외부 NER 서버 Adapter를 제공합니다."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import ValidationError

from app.backends.errors import (
    BackendConfigurationError,
    BackendError,
    BackendInputTooLargeError,
    BackendResponseError,
    BackendTimeoutError,
    BackendTransportError,
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
from app.schemas.detection import (
    Detection,
    policy_id_for_detection_type,
)
from app.schemas.registry import DeploymentConfig


HttpNerBackendErrorCode = Literal[
    "HTTP_NER_CONFIG_INVALID",
    "HTTP_NER_REQUEST_FAILED",
    "HTTP_NER_TIMEOUT",
    "HTTP_NER_HTTP_ERROR",
    "NER_INPUT_TOO_LONG",
    "HTTP_NER_RESPONSE_INVALID",
    "HTTP_NER_RESPONSE_TOO_LARGE",
]

DEFAULT_MAX_NER_RESPONSE_BYTES = 1_048_576
MAX_NER_ERROR_RESPONSE_BYTES = 4_096
_ADAPTER_TYPE = "http_ner"
_RESPONSE_FIELDS = frozenset({"detections"})
_DETECTION_FIELDS = frozenset(
    {"start", "end", "text", "type", "score"}
)


class HttpNerBackendError(BackendError):
    """HTTP NER Adapter의 안전하게 정규화된 기본 오류입니다."""

    def __init__(
        self,
        code: HttpNerBackendErrorCode,
        detail: str,
    ) -> None:
        super().__init__(code, detail)


class HttpNerConfigurationError(
    BackendConfigurationError,
    HttpNerBackendError,
):
    """Deployment 또는 NER 호출 입력이 계약과 다르면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("HTTP_NER_CONFIG_INVALID", detail)


class HttpNerRequestError(
    BackendTransportError,
    HttpNerBackendError,
):
    """NER 서버로 HTTP 요청을 전달하지 못하면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "HTTP_NER_REQUEST_FAILED",
            "NER 서버에 요청을 전송하지 못했습니다",
        )


class HttpNerTimeoutError(
    BackendTimeoutError,
    HttpNerBackendError,
):
    """NER 서버 호출이 Deployment 제한시간을 넘으면 발생합니다."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(
            "HTTP_NER_TIMEOUT",
            f"NER 서버 요청이 {timeout_ms}ms를 초과했습니다",
        )


class HttpNerHttpError(
    BackendResponseError,
    HttpNerBackendError,
):
    """NER 서버가 성공이 아닌 HTTP 상태를 반환하면 발생합니다."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            "HTTP_NER_HTTP_ERROR",
            f"NER 서버가 HTTP {status_code} 상태를 반환했습니다",
        )


class HttpNerInputTooLongError(
    BackendInputTooLargeError,
    HttpNerBackendError,
):
    """공통 NER 서버가 원문 전체를 처리할 수 없으면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "NER_INPUT_TOO_LONG",
            "NER 서버가 입력 전체를 처리할 수 없습니다",
        )


class HttpNerResponseError(
    BackendResponseError,
    HttpNerBackendError,
):
    """NER 서버 응답을 공통 Detection으로 변환할 수 없으면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("HTTP_NER_RESPONSE_INVALID", detail)


class HttpNerResponseTooLargeError(
    BackendResponseError,
    HttpNerBackendError,
):
    """디코딩된 NER 응답이 허용 크기를 넘으면 발생합니다."""

    def __init__(self, max_response_bytes: int) -> None:
        self.max_response_bytes = max_response_bytes
        super().__init__(
            "HTTP_NER_RESPONSE_TOO_LARGE",
            "NER 서버 응답이 허용된 크기를 초과했습니다",
        )


class HttpNerBackend:
    """등록된 외부 NER Endpoint를 공통 HTTP 계약으로 호출합니다."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_response_bytes: int = DEFAULT_MAX_NER_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("client는 httpx.AsyncClient여야 합니다")
        if (
            type(max_response_bytes) is not int
            or max_response_bytes < 1
        ):
            raise ValueError(
                "max_response_bytes는 1 이상의 정수여야 합니다"
            )
        self._client = client
        self._max_response_bytes = max_response_bytes

    @property
    def max_response_bytes(self) -> int:
        """디코딩된 NER 응답에 적용할 최대 byte 크기를 반환합니다."""

        return self._max_response_bytes

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """원문을 외부 NER 서버에 보내고 결과를 공통 형식으로 변환합니다."""

        endpoint, timeout_ms = self._resolve_deployment(deployment)
        request_body = self._serialize_request(text)
        response_body = await self._request(
            endpoint=endpoint,
            request_body=request_body,
            timeout_ms=timeout_ms,
        )
        response_data = self._decode_response(response_body)
        return self._normalize_response(response_data)

    async def _request(
        self,
        *,
        endpoint: httpx.URL,
        request_body: bytes,
        timeout_ms: int,
    ) -> bytes:
        """응답 크기를 제한하면서 NER Endpoint에 POST 요청을 보냅니다."""

        failure: HttpNerBackendError | None = None
        try:
            async with self._client.stream(
                "POST",
                endpoint,
                content=request_body,
                headers={"content-type": "application/json"},
                timeout=timeout_ms / 1_000,
                follow_redirects=False,
            ) as response:
                if not response.is_success:
                    if (
                        response.status_code == 413
                        and await self._is_input_too_long_response(response)
                    ):
                        raise HttpNerInputTooLongError()
                    raise HttpNerHttpError(response.status_code)

                return await self._read_response_body(
                    response,
                    max_bytes=self._max_response_bytes,
                )
        except httpx.TimeoutException:
            failure = HttpNerTimeoutError(timeout_ms)
        except httpx.RequestError:
            failure = HttpNerRequestError()

        if failure is not None:
            raise failure from None
        raise RuntimeError("도달할 수 없는 HTTP NER 요청 상태입니다")

    @staticmethod
    async def _read_response_body(
        response: httpx.Response,
        *,
        max_bytes: int,
    ) -> bytes:
        """Streaming 응답을 지정한 byte 상한까지만 읽습니다."""

        response_body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(response_body) + len(chunk) > max_bytes:
                raise HttpNerResponseTooLargeError(max_bytes)
            response_body.extend(chunk)
        return bytes(response_body)

    @classmethod
    async def _is_input_too_long_response(
        cls,
        response: httpx.Response,
    ) -> bool:
        """공통 NER 서버의 제한된 413 계약만 장문 입력으로 인정합니다."""

        try:
            response_body = await cls._read_response_body(
                response,
                max_bytes=MAX_NER_ERROR_RESPONSE_BYTES,
            )
            payload = load_strict_json(
                response_body,
                max_bytes=MAX_NER_ERROR_RESPONSE_BYTES,
            )
        except (
            HttpNerResponseTooLargeError,
            StrictJsonDecodeError,
        ):
            return False

        if type(payload) is not dict or payload.keys() not in (
            {"code", "detail"},
            {"code", "detail", "maxTokens"},
        ):
            return False
        if (
            payload["code"] != "NER_INPUT_TOO_LONG"
            or type(payload["detail"]) is not str
        ):
            return False
        if "maxTokens" not in payload:
            return True
        return (
            type(payload["maxTokens"]) is int
            and payload["maxTokens"] > 0
        )

    @staticmethod
    def _resolve_deployment(
        deployment: DeploymentConfig,
    ) -> tuple[httpx.URL, int]:
        """Deployment를 검증하고 전체 NER Endpoint URL을 반환합니다."""

        if not isinstance(deployment, DeploymentConfig):
            raise HttpNerConfigurationError(
                "deployment는 DeploymentConfig여야 합니다"
            )
        try:
            validate_runnable_deployment(
                deployment,
                expected_kind="ner",
                expected_adapter_type=_ADAPTER_TYPE,
            )
        except BackendDeploymentValidationError as error:
            raise HttpNerConfigurationError(str(error)) from None

        if deployment.base_url is None:
            raise HttpNerConfigurationError(
                "Deployment baseUrl이 필요합니다"
            )
        if deployment.timeout_ms is None:
            raise HttpNerConfigurationError(
                "Deployment timeoutMs가 필요합니다"
            )
        if deployment.model_name is not None:
            raise HttpNerConfigurationError(
                "HTTP NER Deployment에는 modelName을 사용할 수 없습니다"
            )

        endpoint = httpx.URL(deployment.base_url)
        if endpoint.query or endpoint.fragment:
            raise HttpNerConfigurationError(
                "Deployment baseUrl에는 query나 fragment를 사용할 수 없습니다"
            )
        if endpoint.username or endpoint.password:
            raise HttpNerConfigurationError(
                "Deployment baseUrl에 인증정보를 포함할 수 없습니다"
            )
        return endpoint, deployment.timeout_ms

    @staticmethod
    def _serialize_request(text: str) -> bytes:
        """원문만 포함하는 고정 요청 객체를 엄격한 JSON으로 만듭니다."""

        if type(text) is not str:
            raise HttpNerConfigurationError(
                "text는 문자열이어야 합니다"
            )

        request_body = b""
        serialization_failed = False
        try:
            request_body = dump_json_utf8({"text": text})
        except StrictJsonEncodeError:
            serialization_failed = True
        if serialization_failed:
            del text
            raise HttpNerConfigurationError(
                "text는 유효한 UTF-8 JSON 문자열이어야 합니다"
            ) from None
        return request_body

    def _decode_response(self, response_body: bytes) -> object:
        """응답 원문을 보존하지 않고 엄격한 JSON으로 해석합니다."""

        response_data: object = None
        decode_error: StrictJsonDecodeError | None = None
        try:
            response_data = load_strict_json(
                response_body,
                max_bytes=self._max_response_bytes,
            )
        except StrictJsonDecodeError as error:
            decode_error = error

        if decode_error is None:
            return response_data

        del response_body, response_data
        if decode_error.code == "TOO_LARGE":
            raise HttpNerResponseTooLargeError(
                self._max_response_bytes
            ) from None
        raise HttpNerResponseError(
            "NER 서버 응답이 유효한 UTF-8 JSON이 아닙니다"
        ) from None

    @classmethod
    def _normalize_response(
        cls,
        response_data: object,
    ) -> list[Detection]:
        """엄격한 공통 응답 객체를 Detection 목록으로 변환합니다."""

        if (
            type(response_data) is not dict
            or response_data.keys() != _RESPONSE_FIELDS
        ):
            del response_data
            raise HttpNerResponseError(
                "응답은 detections 필드만 가진 객체여야 합니다"
            )

        detection_items = response_data["detections"]
        if type(detection_items) is not list:
            del response_data, detection_items
            raise HttpNerResponseError(
                "응답 detections는 배열이어야 합니다"
            )

        detections: list[Detection] = []
        for index, item in enumerate(detection_items):
            detection = cls._normalize_item(item, index=index)
            detections.append(detection)
        return detections

    @staticmethod
    def _normalize_item(
        item: object,
        *,
        index: int,
    ) -> Detection:
        """응답 항목 하나의 필드를 제한하고 출처를 NER로 고정합니다."""

        if (
            type(item) is not dict
            or item.keys() != _DETECTION_FIELDS
        ):
            del item
            raise HttpNerResponseError(
                f"detections[{index}]가 공통 NER 응답 계약과 다릅니다"
            )

        detection_data = dict(item)
        try:
            detection_data["policyId"] = policy_id_for_detection_type(
                detection_data["type"]
            )
        except KeyError:
            del item, detection_data
            raise HttpNerResponseError(
                f"detections[{index}].type이 정책에 없습니다"
            ) from None
        detection_data["source"] = "ner"
        detection: Detection | None = None
        validation_failed = False
        try:
            detection = Detection.model_validate(detection_data)
            detection.text.encode("utf-8")
            detection.type.encode("utf-8")
        except (ValidationError, UnicodeEncodeError):
            validation_failed = True

        if validation_failed:
            del item, detection_data, detection
            raise HttpNerResponseError(
                f"detections[{index}]가 공통 Detection 형식과 다릅니다"
            ) from None
        if detection is None:
            raise RuntimeError("HTTP NER Detection 검증 결과가 없습니다")
        return detection


__all__ = [
    "DEFAULT_MAX_NER_RESPONSE_BYTES",
    "MAX_NER_ERROR_RESPONSE_BYTES",
    "HttpNerBackend",
    "HttpNerBackendError",
    "HttpNerBackendErrorCode",
    "HttpNerConfigurationError",
    "HttpNerHttpError",
    "HttpNerInputTooLongError",
    "HttpNerRequestError",
    "HttpNerResponseError",
    "HttpNerResponseTooLargeError",
    "HttpNerTimeoutError",
]
