"""GLiNER NER 서버의 HTTP 계약과 안전한 오류 응답을 검증합니다."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

from services.gliner_ner.gliner_ner_server.inference import (
    GlinerInferenceService,
    NerInputTooLongError,
    STANDARD_NER_LABELS,
    STANDARD_NER_THRESHOLD,
)
from services.gliner_ner.gliner_ner_server.main import create_app
from services.gliner_ner.gliner_ner_server.settings import (
    GlinerServerSettings,
)


class FakePredictor:
    """모델 다운로드 없이 API 계약을 검증하는 추론 대역입니다."""

    model_name = "test/gliner"
    model_revision = "test-revision"
    device = "cuda"

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[str, ...], float]] = []
        self.input_error: Exception | None = None
        self.inference_error: Exception | None = None

    def ensure_input_supported(
        self,
        text: str,
        labels: Sequence[str],
    ) -> None:
        """설정된 길이 오류가 있으면 실제 추론 전에 발생시킵니다."""

        del text, labels
        if self.input_error is not None:
            raise self.input_error

    def predict_entities(
        self,
        text: str,
        labels: Sequence[str],
        threshold: float,
    ) -> object:
        """호출 인자를 기록하고 지정된 모델 출력을 반환합니다."""

        self.calls.append((text, tuple(labels), threshold))
        if self.inference_error is not None:
            raise self.inference_error
        return self.result


def _settings(**overrides: object) -> GlinerServerSettings:
    """테스트가 호스트 환경변수에 의존하지 않는 설정을 만듭니다."""

    values: dict[str, object] = {
        "model_name": "test/gliner",
        "model_revision": "test-revision",
        "device": "cuda",
        "max_text_characters": 1_000,
    }
    values.update(overrides)
    return GlinerServerSettings(**values)  # type: ignore[arg-type]


def _client(
    predictor: FakePredictor,
    *,
    settings: GlinerServerSettings | None = None,
    load_calls: list[GlinerServerSettings] | None = None,
) -> TestClient:
    """주입한 Predictor를 lifespan에서 한 번 제공하는 Client를 만듭니다."""

    def loader(runtime_settings: GlinerServerSettings) -> GlinerInferenceService:
        if load_calls is not None:
            load_calls.append(runtime_settings)
        return GlinerInferenceService(
            predictor,
            max_text_characters=runtime_settings.max_text_characters,
        )

    return TestClient(
        create_app(
            settings=settings or _settings(),
            service_loader=loader,
        )
    )


def test_health_reports_loaded_model_and_device() -> None:
    """health는 실제로 준비된 모델 revision과 장치를 반환합니다."""

    load_calls: list[GlinerServerSettings] = []
    with _client(FakePredictor([]), load_calls=load_calls) as client:
        first_response = client.get("/health")
        second_response = client.get("/health")

    assert first_response.status_code == 200
    assert first_response.json() == {
        "status": "ok",
        "modelName": "test/gliner",
        "modelRevision": "test-revision",
        "device": "cuda",
    }
    assert second_response.status_code == 200
    assert len(load_calls) == 1


def test_ner_returns_exact_lpl_contract() -> None:
    """모델 출력을 정렬하고 LPL이 요구하는 필드만 반환합니다."""

    text = "서울 김철수"
    predictor = FakePredictor(
        [
            {
                "start": 3,
                "end": 6,
                "text": "김철수",
                "label": "PERSONAL_IDENTITY",
                "score": 0.91,
                "internal": "응답에서 제거할 값",
            },
            {
                "start": 0,
                "end": 2,
                "text": "서울",
                "label": "LOCATION",
                "score": 0.98,
            },
        ]
    )

    with _client(predictor) as client:
        response = client.post(
            "/v1/ner/detect",
            json={"text": text},
        )

    assert response.status_code == 200
    assert response.json() == {
        "detections": [
            {
                "start": 0,
                "end": 2,
                "text": "서울",
                "type": "LOCATION",
                "score": 0.98,
            },
            {
                "start": 3,
                "end": 6,
                "text": "김철수",
                "type": "PERSONAL_IDENTITY",
                "score": 0.91,
            },
        ],
    }
    assert predictor.calls == [
        (
            text,
            STANDARD_NER_LABELS,
            STANDARD_NER_THRESHOLD,
        )
    ]


def test_ner_accepts_probe_input_and_empty_result() -> None:
    """LPL Probe의 최소 입력 A도 원문과 빈 배열로 응답합니다."""

    with _client(FakePredictor([])) as client:
        response = client.post(
            "/v1/ner/detect",
            json={"text": "A"},
        )

    assert response.status_code == 200
    assert response.json() == {"detections": []}


def test_legacy_ner_endpoint_is_not_exposed() -> None:
    """모델별 이전 경로를 남겨 공통 계약을 우회하지 않습니다."""

    with _client(FakePredictor([])) as client:
        response = client.post("/ner", json={"text": "A"})

    assert response.status_code == 404


def test_invalid_request_does_not_echo_original_text() -> None:
    """추가 필드가 있어도 검증 오류에 사용자 원문을 복제하지 않습니다."""

    original_text = "외부에 다시 노출하면 안 되는 사용자 원문"
    with _client(FakePredictor([])) as client:
        response = client.post(
            "/v1/ner/detect",
            json={
                "text": original_text,
                "unknown": True,
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": "NER_REQUEST_INVALID",
        "detail": "NER 요청 형식이 올바르지 않습니다",
    }
    assert original_text not in response.text


def test_client_cannot_override_server_detection_policy() -> None:
    """라벨과 임계값은 서버 소유이므로 요청 필드로 받지 않습니다."""

    with _client(FakePredictor([])) as client:
        response = client.post(
            "/v1/ner/detect",
            json={
                "text": "원문",
                "labels": ["사람"],
                "threshold": 0.4,
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "NER_REQUEST_INVALID"


def test_input_too_long_is_fail_closed_before_inference() -> None:
    """모델 truncation 가능성이 있으면 성공 응답 대신 413을 반환합니다."""

    predictor = FakePredictor([])
    predictor.input_error = NerInputTooLongError(384)
    with _client(predictor) as client:
        response = client.post(
            "/v1/ner/detect",
            json={"text": "긴 원문"},
        )

    assert response.status_code == 413
    assert response.json() == {
        "code": "NER_INPUT_TOO_LONG",
        "detail": "NER 입력이 모델의 전체 처리 한도를 초과했습니다",
        "maxTokens": 384,
    }
    assert predictor.calls == []


def test_inference_failure_does_not_echo_original_text() -> None:
    """모델 내부 오류도 원문이나 예외 메시지를 응답에 포함하지 않습니다."""

    original_text = "로그나 응답에 남기지 않을 사용자 원문"
    predictor = FakePredictor([])
    predictor.inference_error = RuntimeError(original_text)
    with _client(predictor) as client:
        response = client.post(
            "/v1/ner/detect",
            json={"text": original_text},
        )

    assert response.status_code == 500
    assert response.json() == {
        "code": "NER_INFERENCE_FAILED",
        "detail": "NER 모델 추론에 실패했습니다",
    }
    assert original_text not in response.text


def test_invalid_model_span_rejects_entire_response() -> None:
    """모델 span과 text가 다르면 일부 결과도 반환하지 않습니다."""

    predictor = FakePredictor(
        [
            {
                "start": 0,
                "end": 2,
                "text": "잘못된 문자열",
                "label": "사람",
                "score": 0.9,
            }
        ]
    )
    with _client(predictor) as client:
        response = client.post(
            "/v1/ner/detect",
            json={"text": "홍길동"},
        )

    assert response.status_code == 500
    assert response.json()["code"] == "NER_INFERENCE_FAILED"
