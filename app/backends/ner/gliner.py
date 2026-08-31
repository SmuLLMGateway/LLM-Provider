"""GLiNER HTTP 서버 응답을 공통 Detection으로 변환합니다."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import (
    ValidationError,
)

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


GlinerNerBackendErrorCode = Literal[
    "GLINER_NER_CONFIG_INVALID",
    "GLINER_NER_REQUEST_FAILED",
    "GLINER_NER_TIMEOUT",
    "GLINER_NER_HTTP_ERROR",
    "GLINER_NER_INPUT_TOO_LONG",
    "GLINER_NER_RESPONSE_INVALID",
    "GLINER_NER_RESPONSE_TOO_LARGE",
]

DEFAULT_MAX_GLINER_NER_RESPONSE_BYTES = 1_048_576
MAX_GLINER_ERROR_RESPONSE_BYTES = 4_096
DEFAULT_GLINER_LABELS = (
    "사람",
    "회사",
    "조직",
    "주소",
    "장소",
)
DEFAULT_GLINER_THRESHOLD = 0.4
_ADAPTER_TYPE = "gliner_http"
_RESPONSE_FIELDS = frozenset({"text", "entities"})
_ENTITY_FIELDS = frozenset(
    {"start", "end", "text", "label", "score"}
)
class GlinerNerBackendError(BackendError):
    """GLiNER NER Adapter의 안전하게 정규화된 기본 오류입니다."""

    def __init__(
        self,
        code: GlinerNerBackendErrorCode,
        detail: str,
    ) -> None:
        super().__init__(code, detail)


class GlinerNerConfigurationError(
    BackendConfigurationError,
    GlinerNerBackendError,
):
    """Deployment 또는 호출 입력이 GLiNER 계약과 다르면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("GLINER_NER_CONFIG_INVALID", detail)


class GlinerNerRequestError(
    BackendTransportError,
    GlinerNerBackendError,
):
    """GLiNER Endpoint에 HTTP 요청을 전달하지 못하면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "GLINER_NER_REQUEST_FAILED",
            "GLiNER 서버에 요청을 전송하지 못했습니다",
        )


class GlinerNerTimeoutError(
    BackendTimeoutError,
    GlinerNerBackendError,
):
    """Deployment 제한시간 안에 GLiNER 호출이 끝나지 않으면 발생합니다."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(
            "GLINER_NER_TIMEOUT",
            f"GLiNER 서버 요청이 {timeout_ms}ms를 초과했습니다",
        )


class GlinerNerHttpError(
    BackendResponseError,
    GlinerNerBackendError,
):
    """GLiNER 서버가 성공이 아닌 HTTP 상태를 반환하면 발생합니다."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            "GLINER_NER_HTTP_ERROR",
            f"GLiNER 서버가 HTTP {status_code} 상태를 반환했습니다",
        )


class GlinerNerInputTooLongError(
    BackendInputTooLargeError,
    GlinerNerBackendError,
):
    """GLiNER 서버가 원문 전체를 자르지 않고 처리할 수 없으면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "GLINER_NER_INPUT_TOO_LONG",
            "GLiNER 서버가 입력 전체를 처리할 수 없습니다",
        )


class GlinerNerResponseError(
    BackendResponseError,
    GlinerNerBackendError,
):
    """응답을 공통 Detection으로 안전하게 변환할 수 없으면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("GLINER_NER_RESPONSE_INVALID", detail)


class GlinerNerResponseTooLargeError(
    BackendResponseError,
    GlinerNerBackendError,
):
    """디코딩된 응답이 허용된 최대 byte 크기를 넘으면 발생합니다."""

    def __init__(self, max_response_bytes: int) -> None:
        self.max_response_bytes = max_response_bytes
        super().__init__(
            "GLINER_NER_RESPONSE_TOO_LARGE",
            "GLiNER 서버 응답이 허용된 크기를 초과했습니다",
        )


class GlinerNerBackend:
    """등록된 GLiNER HTTP Endpoint를 공통 NER Backend로 제공합니다."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_response_bytes: int = (
            DEFAULT_MAX_GLINER_NER_RESPONSE_BYTES
        ),
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
        """디코딩된 GLiNER 응답에 적용할 최대 byte 크기를 반환합니다."""

        return self._max_response_bytes

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """원문과 내부 고정 라벨을 GLiNER 서버에 보내 정규화합니다."""

        endpoint, timeout_ms = self._resolve_deployment(
            deployment
        )
        request_body = self._serialize_request(text)
        response_body = await self._request(
            endpoint=endpoint,
            request_body=request_body,
            timeout_ms=timeout_ms,
        )
        response_data = self._decode_response(response_body)
        return self._normalize_response(
            response_data,
            original_text=text,
        )

    async def _request(
        self,
        *,
        endpoint: httpx.URL,
        request_body: bytes,
        timeout_ms: int,
    ) -> bytes:
        """Redirect를 막고 디코딩된 응답 크기를 제한하여 POST합니다."""

        failure: GlinerNerBackendError | None = None
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
                        raise GlinerNerInputTooLongError()
                    raise GlinerNerHttpError(response.status_code)

                return await self._read_response_body(
                    response,
                    max_bytes=self._max_response_bytes,
                )
        except httpx.TimeoutException:
            failure = GlinerNerTimeoutError(timeout_ms)
        except httpx.RequestError:
            failure = GlinerNerRequestError()

        if failure is not None:
            raise failure from None
        raise RuntimeError("도달할 수 없는 GLiNER NER 요청 상태입니다")

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
                raise GlinerNerResponseTooLargeError(max_bytes)
            response_body.extend(chunk)
        return bytes(response_body)

    @classmethod
    async def _is_input_too_long_response(
        cls,
        response: httpx.Response,
    ) -> bool:
        """전용 NER 서버의 제한된 413 계약만 장문 입력 오류로 인정합니다."""

        try:
            response_body = await cls._read_response_body(
                response,
                max_bytes=MAX_GLINER_ERROR_RESPONSE_BYTES,
            )
            payload = load_strict_json(
                response_body,
                max_bytes=MAX_GLINER_ERROR_RESPONSE_BYTES,
            )
        except (
            GlinerNerResponseTooLargeError,
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
        """Deployment를 검증하고 GLiNER Endpoint 설정을 반환합니다."""

        if not isinstance(deployment, DeploymentConfig):
            raise GlinerNerConfigurationError(
                "deployment는 DeploymentConfig여야 합니다"
            )
        try:
            validate_runnable_deployment(
                deployment,
                expected_kind="ner",
                expected_adapter_type=_ADAPTER_TYPE,
            )
        except BackendDeploymentValidationError as error:
            raise GlinerNerConfigurationError(str(error)) from None

        if deployment.base_url is None:
            raise GlinerNerConfigurationError(
                "Deployment baseUrl이 필요합니다"
            )
        if deployment.timeout_ms is None:
            raise GlinerNerConfigurationError(
                "Deployment timeoutMs가 필요합니다"
            )
        if deployment.model_name is not None:
            raise GlinerNerConfigurationError(
                "GLiNER Deployment에는 modelName을 사용할 수 없습니다"
            )
        endpoint = httpx.URL(deployment.base_url)
        if endpoint.query or endpoint.fragment:
            raise GlinerNerConfigurationError(
                "Deployment baseUrl에는 query나 fragment를 "
                "사용할 수 없습니다"
            )
        if endpoint.username or endpoint.password:
            raise GlinerNerConfigurationError(
                "Deployment baseUrl에 인증정보를 포함할 수 없습니다"
            )
        return endpoint, deployment.timeout_ms

    @staticmethod
    def _serialize_request(
        text: str,
    ) -> bytes:
        """고정 labels와 threshold를 포함한 GLiNER 요청을 만듭니다."""

        if type(text) is not str:
            raise GlinerNerConfigurationError(
                "text는 문자열이어야 합니다"
            )

        request_body = b""
        serialization_failed = False
        try:
            request_body = dump_json_utf8(
                {
                    "text": text,
                    "labels": list(DEFAULT_GLINER_LABELS),
                    "threshold": DEFAULT_GLINER_THRESHOLD,
                }
            )
        except StrictJsonEncodeError:
            serialization_failed = True

        if serialization_failed:
            del text
            raise GlinerNerConfigurationError(
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
            raise GlinerNerResponseTooLargeError(
                self._max_response_bytes
            ) from None
        raise GlinerNerResponseError(
            "GLiNER 응답이 유효한 UTF-8 JSON이 아닙니다"
        ) from None

    @classmethod
    def _normalize_response(
        cls,
        response_data: object,
        *,
        original_text: str,
    ) -> list[Detection]:
        """GLiNER 응답 객체를 공통 Detection 목록으로 변환합니다."""

        if (
            type(response_data) is not dict
            or response_data.keys() != _RESPONSE_FIELDS
        ):
            del response_data
            raise GlinerNerResponseError(
                "GLiNER 응답은 text와 entities만 가진 객체여야 합니다"
            )

        response_text = response_data["text"]
        entities = response_data["entities"]
        if type(response_text) is not str or response_text != original_text:
            del response_data, response_text
            raise GlinerNerResponseError(
                "GLiNER 응답 원문이 요청 원문과 일치하지 않습니다"
            )
        if type(entities) is not list:
            del response_data, entities
            raise GlinerNerResponseError(
                "GLiNER 응답 entities는 배열이어야 합니다"
            )

        detections: list[Detection] = []
        for index, item in enumerate(entities):
            detections.append(
                cls._normalize_item(
                    item,
                    index=index,
                    original_text=original_text,
                )
            )
        return detections

    @staticmethod
    def _normalize_item(
        item: object,
        *,
        index: int,
        original_text: str,
    ) -> Detection:
        """entity 하나의 엄격한 필드, span과 문자열 일치를 검증합니다."""

        if type(item) is not dict or item.keys() != _ENTITY_FIELDS:
            del item
            raise GlinerNerResponseError(
                f"entities[{index}]의 필드 구성이 올바르지 않습니다"
            )

        start = item["start"]
        end = item["end"]
        entity_text = item["text"]
        label = item["label"]
        score = item["score"]

        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end > len(original_text)
        ):
            del item, start, end
            raise GlinerNerResponseError(
                f"entities[{index}]의 원문 범위가 올바르지 않습니다"
            )
        if (
            type(entity_text) is not str
            or not entity_text
            or entity_text != original_text[start:end]
        ):
            del item, entity_text
            raise GlinerNerResponseError(
                f"entities[{index}]의 text가 원문 범위와 일치하지 않습니다"
            )
        if type(label) is not str or not label:
            del item, label
            raise GlinerNerResponseError(
                f"entities[{index}]의 label이 올바르지 않습니다"
            )

        detection: Detection | None = None
        validation_failed = False
        try:
            entity_text.encode("utf-8")
            label.encode("utf-8")
            detection = Detection(
                start=start,
                end=end,
                text=original_text[start:end],
                type=label,
                policyId=policy_id_for_detection_type(label),
                source="ner",
                score=score,
            )
        except (KeyError, ValidationError, UnicodeEncodeError):
            validation_failed = True

        if validation_failed:
            del item, detection, entity_text, label
            raise GlinerNerResponseError(
                f"entities[{index}]을 Detection으로 변환할 수 없습니다"
            ) from None
        if detection is None:
            raise RuntimeError("GLiNER Detection 검증 결과가 없습니다")
        return detection


__all__ = [
    "DEFAULT_GLINER_LABELS",
    "DEFAULT_GLINER_THRESHOLD",
    "DEFAULT_MAX_GLINER_NER_RESPONSE_BYTES",
    "MAX_GLINER_ERROR_RESPONSE_BYTES",
    "GlinerNerBackend",
    "GlinerNerBackendError",
    "GlinerNerBackendErrorCode",
    "GlinerNerConfigurationError",
    "GlinerNerHttpError",
    "GlinerNerInputTooLongError",
    "GlinerNerRequestError",
    "GlinerNerResponseError",
    "GlinerNerResponseTooLargeError",
    "GlinerNerTimeoutError",
]
