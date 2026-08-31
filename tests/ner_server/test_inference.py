"""GLiNER Predictor의 자동 truncation 차단과 출력 검증을 테스트합니다."""

from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from app.schemas.detection import ALLOWED_DETECTION_TYPES as LPL_DETECTION_TYPES
from services.gliner_ner.gliner_ner_server.contracts import (
    ALLOWED_DETECTION_TYPES as NER_DETECTION_TYPES,
)
from services.gliner_ner.gliner_ner_server.inference import (
    BACKBONE_FILES,
    GlinerInferenceService,
    GlinerPredictor,
    NerInputTooLongError,
    NerModelOutputError,
    STANDARD_NER_LABELS,
    STANDARD_NER_THRESHOLD,
    _build_staged_model_directory,
)
from services.gliner_ner.gliner_ner_server.settings import GlinerServerSettings


class FakeTokenizer:
    """prompt가 포함된 subword 길이를 통제하는 Tokenizer 대역입니다."""

    def __init__(self, token_count: int, model_max_length: int) -> None:
        self._token_count = token_count
        self.model_max_length = model_max_length

    def __call__(self, *args: object, **kwargs: object) -> dict[str, list[list[int]]]:
        del args, kwargs
        return {"input_ids": [list(range(self._token_count))]}


class FakeProcessor:
    """GLiNER prompt 준비와 Tokenizer 속성을 제공하는 대역입니다."""

    def __init__(self, tokenizer: FakeTokenizer) -> None:
        self.transformer_tokenizer = tokenizer

    def prepare_inputs(
        self,
        texts: Sequence[Sequence[str]],
        labels: Sequence[str],
    ) -> tuple[list[list[str]], list[int]]:
        del labels
        return [list(texts[0])], [1]


class FakeGlinerModel:
    """실제 패키지 없이 GlinerPredictor의 길이 검사를 재현합니다."""

    def __init__(
        self,
        *,
        words: Sequence[str],
        max_words: int,
        subword_tokens: int,
        max_subwords: int,
    ) -> None:
        self._words = list(words)
        self.config = SimpleNamespace(max_len=max_words)
        self.data_processor = FakeProcessor(
            FakeTokenizer(subword_tokens, max_subwords)
        )
        self.model = SimpleNamespace(
            token_rep_layer=SimpleNamespace(
                bert_layer=SimpleNamespace(
                    model=SimpleNamespace(
                        config=SimpleNamespace(
                            max_position_embeddings=max_subwords
                        )
                    )
                )
            )
        )
        self.requested_labels: list[str] | None = None

    def prepare_inputs(
        self,
        texts: Sequence[str],
    ) -> tuple[list[list[str]], list[list[int]], list[list[int]]]:
        del texts
        return [self._words], [[]], [[]]

    def predict_entities(
        self,
        text: str,
        labels: Sequence[str],
        *,
        threshold: float,
    ) -> list[dict[str, object]]:
        del threshold
        self.requested_labels = list(labels)
        return [
            {
                "start": 0,
                "end": len(text),
                "text": text,
                "label": labels[0],
                "score": 0.9,
            }
        ]


def _predictor(model: FakeGlinerModel) -> GlinerPredictor:
    """길이 검사용 GLiNER 모델 대역을 Predictor로 감쌉니다."""

    return GlinerPredictor(
        model,
        model_name="test/gliner",
        model_revision="revision",
        device="cuda",
    )


def test_ner_contract_uses_same_policy_types_as_lpl() -> None:
    """별도 NER 이미지와 LPL의 공개 type 계약이 드리프트하지 않습니다."""

    assert NER_DETECTION_TYPES == LPL_DETECTION_TYPES


def test_predictor_rejects_more_words_than_model_limit() -> None:
    """384개를 넘는 word 입력을 GLiNER가 자르기 전에 거부합니다."""

    predictor = _predictor(
        FakeGlinerModel(
            words=["단어"] * 385,
            max_words=384,
            subword_tokens=100,
            max_subwords=512,
        )
    )

    with pytest.raises(NerInputTooLongError) as error_info:
        predictor.ensure_input_supported("원문", ["사람"])

    assert error_info.value.max_tokens == 384


def test_predictor_rejects_prompted_subwords_over_tokenizer_limit() -> None:
    """word 수가 작아도 subword가 Encoder 한도를 넘으면 거부합니다."""

    predictor = _predictor(
        FakeGlinerModel(
            words=["매우긴토큰"],
            max_words=384,
            subword_tokens=513,
            max_subwords=512,
        )
    )

    with pytest.raises(NerInputTooLongError) as error_info:
        predictor.ensure_input_supported("원문", ["사람"])

    assert error_info.value.max_tokens == 512


def test_predictor_uses_encoder_limit_when_tokenizer_has_sentinel() -> None:
    """Tokenizer의 거대한 sentinel 대신 실제 Encoder 한도로 차단합니다."""

    model = FakeGlinerModel(
        words=["매우긴토큰"],
        max_words=384,
        subword_tokens=513,
        max_subwords=512,
    )
    model.data_processor.transformer_tokenizer.model_max_length = 10**30
    predictor = _predictor(model)

    with pytest.raises(NerInputTooLongError) as error_info:
        predictor.ensure_input_supported("원문", ["사람"])

    assert error_info.value.max_tokens == 512


def test_predictor_rejects_missing_encoder_limit() -> None:
    """실제 Encoder 한도를 확인할 수 없으면 조용히 추론하지 않습니다."""

    model = FakeGlinerModel(
        words=["단어"],
        max_words=384,
        subword_tokens=10,
        max_subwords=512,
    )
    del model.model.token_rep_layer.bert_layer
    predictor = _predictor(model)

    with pytest.raises(NerModelOutputError):
        predictor.ensure_input_supported("원문", ["사람"])


def test_predictor_translates_fixed_lpl_labels_for_model() -> None:
    """모델에는 영어 의미 라벨을 주고 응답은 정책 코드로 복원합니다."""

    model = FakeGlinerModel(
        words=["홍길동"],
        max_words=384,
        subword_tokens=20,
        max_subwords=512,
    )
    predictor = _predictor(model)

    result = predictor.predict_entities(
        "홍길동",
        list(STANDARD_NER_LABELS),
        0.4,
    )

    assert model.requested_labels == [
        "person name or personal identity",
        "postal address or precise location",
    ]
    assert result == [
        {
            "start": 0,
            "end": 3,
            "text": "홍길동",
            "label": "PERSONAL_IDENTITY",
            "score": 0.9,
        }
    ]


def test_predictor_preserves_non_lpl_custom_labels() -> None:
    """고정 세트가 아닌 요청은 서버가 임의로 번역하지 않습니다."""

    model = FakeGlinerModel(
        words=["문서"],
        max_words=384,
        subword_tokens=20,
        max_subwords=512,
    )
    predictor = _predictor(model)

    result = predictor.predict_entities("문서", ["프로젝트"], 0.4)

    assert model.requested_labels == ["프로젝트"]
    assert result == [
        {
            "start": 0,
            "end": 2,
            "text": "문서",
            "label": "프로젝트",
            "score": 0.9,
        }
    ]


class RawPredictor:
    """출력 정규화만 검증하기 위한 최소 Predictor입니다."""

    model_name = "test/gliner"
    model_revision = "revision"
    device = "cpu"

    def __init__(self, result: object) -> None:
        self.result = result

    def ensure_input_supported(
        self,
        text: str,
        labels: Sequence[str],
    ) -> None:
        del text, labels

    def predict_entities(
        self,
        text: str,
        labels: Sequence[str],
        threshold: float,
    ) -> object:
        del text, labels, threshold
        return self.result


@pytest.mark.parametrize(
    "invalid_result",
    [
        "not-an-array",
        [{"start": True, "end": 1, "text": "홍", "label": "사람", "score": 0.9}],
        [{"start": 0, "end": 1, "text": "홍", "label": "사람", "score": True}],
        [{"start": 0, "end": 1, "text": "홍", "label": "사람", "score": float("nan")}],
        [{"start": 0, "end": 1, "text": "홍", "label": "미요청", "score": 0.9}],
    ],
)
def test_service_rejects_untrusted_model_output(
    invalid_result: object,
) -> None:
    """boolean 숫자, NaN, 미요청 라벨 등 비신뢰 출력을 거부합니다."""

    service = GlinerInferenceService(
        RawPredictor(invalid_result),
        max_text_characters=100,
    )

    with pytest.raises(NerModelOutputError):
        service.detect("홍길동")


def test_service_owns_fixed_labels_and_threshold() -> None:
    """공통 요청에 없는 추론 정책은 GLiNER 서버가 고정합니다."""

    predictor = RawPredictor([])
    calls: list[tuple[str, tuple[str, ...], float]] = []

    def record_predict(
        text: str,
        labels: Sequence[str],
        threshold: float,
    ) -> object:
        calls.append((text, tuple(labels), threshold))
        return []

    predictor.predict_entities = record_predict  # type: ignore[method-assign]
    service = GlinerInferenceService(
        predictor,
        max_text_characters=100,
    )

    assert service.detect("원문").detections == ()
    assert calls == [
        (
            "원문",
            STANDARD_NER_LABELS,
            STANDARD_NER_THRESHOLD,
        )
    ]


def test_staged_model_pins_backbone_without_modifying_source(tmp_path) -> None:
    """원본 GLiNER config는 유지하고 별도 실행본에 고정 backbone 경로를 씁니다."""

    model_snapshot = tmp_path / "model"
    backbone_snapshot = tmp_path / "backbone"
    cache_dir = tmp_path / "cache"
    model_snapshot.mkdir()
    backbone_snapshot.mkdir()
    for file_name in BACKBONE_FILES:
        (backbone_snapshot / file_name).write_bytes(b"backbone")
    original_config = {
        "model_name": "microsoft/mdeberta-v3-base",
        "max_len": 384,
    }
    (model_snapshot / "gliner_config.json").write_text(
        json.dumps(original_config),
        encoding="utf-8",
    )
    (model_snapshot / "model.safetensors").write_bytes(b"weights")
    settings = GlinerServerSettings(cache_dir=cache_dir)

    staged = _build_staged_model_directory(
        settings,
        model_snapshot=model_snapshot,
        backbone_snapshot=backbone_snapshot,
    )

    source_config = json.loads(
        (model_snapshot / "gliner_config.json").read_text(encoding="utf-8")
    )
    staged_config = json.loads(
        (staged / "gliner_config.json").read_text(encoding="utf-8")
    )
    assert source_config == original_config
    assert staged_config["model_name"] == str(backbone_snapshot.resolve())
    assert (staged / "model.safetensors").read_bytes() == b"weights"
    assert (
        _build_staged_model_directory(
            settings,
            model_snapshot=model_snapshot,
            backbone_snapshot=backbone_snapshot,
        )
        == staged
    )


def test_staged_model_rejects_unexpected_backbone(tmp_path) -> None:
    """GLiNER 설정이 예상하지 않은 backbone을 가리키면 시작을 거부합니다."""

    model_snapshot = tmp_path / "model"
    backbone_snapshot = tmp_path / "backbone"
    model_snapshot.mkdir()
    backbone_snapshot.mkdir()
    for file_name in BACKBONE_FILES:
        (backbone_snapshot / file_name).write_bytes(b"backbone")
    (model_snapshot / "gliner_config.json").write_text(
        json.dumps({"model_name": "unexpected/backbone"}),
        encoding="utf-8",
    )
    (model_snapshot / "model.safetensors").write_bytes(b"weights")

    with pytest.raises(RuntimeError, match="backbone"):
        _build_staged_model_directory(
            GlinerServerSettings(cache_dir=tmp_path / "cache"),
            model_snapshot=model_snapshot,
            backbone_snapshot=backbone_snapshot,
        )
