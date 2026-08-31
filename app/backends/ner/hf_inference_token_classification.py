"""Hugging Face Inference Token Classification을 공통 Detection으로 변환합니다."""

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


HfInferenceNerBackendErrorCode = Literal[
    "HF_INFERENCE_NER_CONFIG_INVALID",
    "HF_INFERENCE_NER_REQUEST_FAILED",
    "HF_INFERENCE_NER_TIMEOUT",
    "HF_INFERENCE_NER_HTTP_ERROR",
    "HF_INFERENCE_NER_RESPONSE_INVALID",
    "HF_INFERENCE_NER_RESPONSE_TOO_LARGE",
]

DEFAULT_MAX_HF_INFERENCE_NER_RESPONSE_BYTES = 1_048_576
_ADAPTER_TYPE = "hf_inference_token_classification"
_COMMON_RESULT_FIELDS = frozenset({"score", "word", "start", "end"})
_KNOWN_RESULT_FIELDS = frozenset(
    {
        "entity",
        "entity_group",
        "index",
        *_COMMON_RESULT_FIELDS,
    }
)


class HfInferenceNerBackendError(BackendError):
    """Hugging Face NER Adapter의 안전하게 정규화된 기본 오류입니다."""

    def __init__(
        self,
        code: HfInferenceNerBackendErrorCode,
        detail: str,
    ) -> None:
        super().__init__(code, detail)


class HfInferenceNerConfigurationError(
    BackendConfigurationError,
    HfInferenceNerBackendError,
):
    """Deployment 또는 호출 입력이 Adapter 계약과 다르면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("HF_INFERENCE_NER_CONFIG_INVALID", detail)


class HfInferenceNerRequestError(
    BackendTransportError,
    HfInferenceNerBackendError,
):
    """Hugging Face NER Endpoint에 요청을 전달하지 못하면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "HF_INFERENCE_NER_REQUEST_FAILED",
            "Hugging Face NER 서버에 요청을 전송하지 못했습니다",
        )


class HfInferenceNerTimeoutError(
    BackendTimeoutError,
    HfInferenceNerBackendError,
):
    """Deployment 제한시간 안에 NER 호출이 끝나지 않으면 발생합니다."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(
            "HF_INFERENCE_NER_TIMEOUT",
            f"Hugging Face NER 서버 요청이 {timeout_ms}ms를 초과했습니다",
        )


class HfInferenceNerHttpError(
    BackendResponseError,
    HfInferenceNerBackendError,
):
    """NER 서버가 성공이 아닌 HTTP 상태를 반환하면 발생합니다."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            "HF_INFERENCE_NER_HTTP_ERROR",
            "Hugging Face NER 서버가 "
            f"HTTP {status_code} 상태를 반환했습니다",
        )


class HfInferenceNerResponseError(
    BackendResponseError,
    HfInferenceNerBackendError,
):
    """응답을 공통 Detection으로 안전하게 변환할 수 없으면 발생합니다."""

    def __init__(self, detail: str) -> None:
        super().__init__("HF_INFERENCE_NER_RESPONSE_INVALID", detail)


class HfInferenceNerResponseTooLargeError(
    BackendResponseError,
    HfInferenceNerBackendError,
):
    """디코딩된 응답이 허용된 최대 byte 크기를 넘으면 발생합니다."""

    def __init__(self, max_response_bytes: int) -> None:
        self.max_response_bytes = max_response_bytes
        super().__init__(
            "HF_INFERENCE_NER_RESPONSE_TOO_LARGE",
            "Hugging Face NER 서버 응답이 허용된 크기를 초과했습니다",
        )


class HfInferenceTokenClassificationNerBackend:
    """Hugging Face Inference Token Classification Endpoint를 호출합니다."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_response_bytes: int = (
            DEFAULT_MAX_HF_INFERENCE_NER_RESPONSE_BYTES
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
        """디코딩된 NER 응답에 적용할 최대 byte 크기를 반환합니다."""

        return self._max_response_bytes

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """원문을 Token Classification API에 보내고 결과를 정규화합니다."""

        endpoint, timeout_ms = self._resolve_deployment(deployment)
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

        failure: HfInferenceNerBackendError | None = None
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
                    raise HfInferenceNerHttpError(
                        response.status_code
                    )

                response_body = bytearray()
                async for chunk in response.aiter_bytes():
                    if (
                        len(response_body) + len(chunk)
                        > self._max_response_bytes
                    ):
                        raise HfInferenceNerResponseTooLargeError(
                            self._max_response_bytes
                        )
                    response_body.extend(chunk)
                return bytes(response_body)
        except httpx.TimeoutException:
            failure = HfInferenceNerTimeoutError(timeout_ms)
        except httpx.RequestError:
            failure = HfInferenceNerRequestError()

        if failure is not None:
            raise failure from None
        raise RuntimeError(
            "도달할 수 없는 Hugging Face NER 요청 상태입니다"
        )

    @staticmethod
    def _resolve_deployment(
        deployment: DeploymentConfig,
    ) -> tuple[httpx.URL, int]:
        """Deployment를 검증하고 완성된 Endpoint URL을 반환합니다."""

        if not isinstance(deployment, DeploymentConfig):
            raise HfInferenceNerConfigurationError(
                "deployment는 DeploymentConfig여야 합니다"
            )
        try:
            validate_runnable_deployment(
                deployment,
                expected_kind="ner",
                expected_adapter_type=_ADAPTER_TYPE,
            )
        except BackendDeploymentValidationError as error:
            raise HfInferenceNerConfigurationError(
                str(error)
            ) from None

        if deployment.base_url is None:
            raise HfInferenceNerConfigurationError(
                "Deployment baseUrl이 필요합니다"
            )
        if deployment.timeout_ms is None:
            raise HfInferenceNerConfigurationError(
                "Deployment timeoutMs가 필요합니다"
            )
        if deployment.model_name is not None:
            raise HfInferenceNerConfigurationError(
                "Hugging Face NER Deployment에는 "
                "modelName을 사용할 수 없습니다"
            )

        endpoint = httpx.URL(deployment.base_url)
        if endpoint.query or endpoint.fragment:
            raise HfInferenceNerConfigurationError(
                "Deployment baseUrl에는 query나 fragment를 "
                "사용할 수 없습니다"
            )
        if endpoint.username or endpoint.password:
            raise HfInferenceNerConfigurationError(
                "Deployment baseUrl에 인증정보를 포함할 수 없습니다"
            )
        return endpoint, deployment.timeout_ms

    @staticmethod
    def _serialize_request(text: str) -> bytes:
        """공식 API의 필수 inputs 필드만 가진 엄격한 JSON을 만듭니다."""

        if type(text) is not str:
            raise HfInferenceNerConfigurationError(
                "text는 문자열이어야 합니다"
            )

        request_body = b""
        serialization_failed = False
        try:
            request_body = dump_json_utf8({"inputs": text})
        except StrictJsonEncodeError:
            serialization_failed = True

        if serialization_failed:
            del text
            raise HfInferenceNerConfigurationError(
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
            raise HfInferenceNerResponseTooLargeError(
                self._max_response_bytes
            ) from None
        raise HfInferenceNerResponseError(
            "Hugging Face NER 응답이 유효한 UTF-8 JSON이 아닙니다"
        ) from None

    @classmethod
    def _normalize_response(
        cls,
        response_data: object,
        *,
        original_text: str,
    ) -> list[Detection]:
        """Hugging Face 결과 배열을 공통 Detection 목록으로 바꿉니다."""

        if type(response_data) is not list:
            del response_data
            raise HfInferenceNerResponseError(
                "Hugging Face NER 응답은 배열이어야 합니다"
            )

        detections: list[Detection] = []
        for index, item in enumerate(response_data):
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
        """항목 계약과 원문 span을 검증하고 서버 word는 신뢰하지 않습니다."""

        if type(item) is not dict:
            del item
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]는 객체여야 합니다"
            )

        item_fields = frozenset(item)
        if (
            not _COMMON_RESULT_FIELDS.issubset(item_fields)
            or not item_fields.issubset(_KNOWN_RESULT_FIELDS)
        ):
            del item, item_fields
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]의 필드 구성이 올바르지 않습니다"
            )

        has_entity = "entity" in item
        has_entity_group = "entity_group" in item
        if has_entity == has_entity_group:
            del item
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]에는 entity 또는 "
                "entity_group 중 하나만 있어야 합니다"
            )
        if "index" in item and not has_entity:
            del item
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]의 index는 entity 결과에만 "
                "사용할 수 있습니다"
            )

        label = (
            item["entity"]
            if has_entity
            else item["entity_group"]
        )
        word = item["word"]
        start = item["start"]
        end = item["end"]
        score = item["score"]
        token_index = item.get("index")

        if type(label) is not str or not label:
            del item, label
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]의 개체 유형이 올바르지 않습니다"
            )
        if type(word) is not str or not word:
            del item, word
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}].word가 올바르지 않습니다"
            )
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or end > len(original_text)
        ):
            del item, start, end
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]의 원문 범위가 올바르지 않습니다"
            )
        if (
            "index" in item
            and (
                type(token_index) is not int
                or token_index < 0
            )
        ):
            del item, token_index
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}].index가 올바르지 않습니다"
            )

        detection: Detection | None = None
        validation_failed = False
        try:
            label.encode("utf-8")
            word.encode("utf-8")
            detection = Detection(
                start=start,
                end=end,
                text=original_text[start:end],
                type=label,
                policyId=policy_id_for_detection_type(label),
                source="ner",
                score=score,
            )
            detection.text.encode("utf-8")
        except (KeyError, ValidationError, UnicodeEncodeError):
            validation_failed = True

        if validation_failed:
            del item, detection, label, word
            raise HfInferenceNerResponseError(
                f"응답 항목[{index}]을 Detection으로 변환할 수 없습니다"
            ) from None
        if detection is None:
            raise RuntimeError(
                "Hugging Face NER Detection 검증 결과가 없습니다"
            )
        return detection


__all__ = [
    "DEFAULT_MAX_HF_INFERENCE_NER_RESPONSE_BYTES",
    "HfInferenceNerBackendError",
    "HfInferenceNerBackendErrorCode",
    "HfInferenceNerConfigurationError",
    "HfInferenceNerHttpError",
    "HfInferenceNerRequestError",
    "HfInferenceNerResponseError",
    "HfInferenceNerResponseTooLargeError",
    "HfInferenceNerTimeoutError",
    "HfInferenceTokenClassificationNerBackend",
]
