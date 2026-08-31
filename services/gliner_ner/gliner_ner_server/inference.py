"""GLiNER 모델을 한 번 로드하고 출력값을 엄격하게 정규화합니다."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from collections.abc import Mapping, Sequence
from numbers import Integral, Real
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from .contracts import (
    NerDetection,
    NerResponse,
)
from .settings import (
    GlinerServerSettings,
)


MODEL_FILES = (
    "gliner_config.json",
    "model.safetensors",
)
BACKBONE_FILES = (
    "config.json",
    "spm.model",
    "tokenizer_config.json",
)
LPL_LABEL_TRANSLATIONS = {
    "PERSONAL_IDENTITY": "person name or personal identity",
    "LOCATION": "postal address or precise location",
}
STANDARD_NER_LABELS = tuple(LPL_LABEL_TRANSLATIONS)
STANDARD_NER_THRESHOLD = 0.4


class NerInferenceError(RuntimeError):
    """원문을 포함하지 않는 NER 추론 오류의 공통 타입입니다."""


class NerInputTooLongError(NerInferenceError):
    """모델이 원문 일부를 조용히 자르기 전에 요청을 거부합니다."""

    def __init__(self, max_tokens: int | None = None) -> None:
        self.max_tokens = max_tokens
        super().__init__("NER 입력이 모델 제한을 초과했습니다")


class NerModelOutputError(NerInferenceError):
    """모델 출력이 공개 응답 계약으로 변환될 수 없을 때 발생합니다."""


@runtime_checkable
class EntityPredictor(Protocol):
    """실제 GLiNER와 테스트 대역이 공통으로 제공하는 기능입니다."""

    model_name: str
    model_revision: str
    device: str

    def ensure_input_supported(
        self,
        text: str,
        labels: Sequence[str],
    ) -> None:
        """모델이 원문 전체를 자르지 않고 처리할 수 있는지 검증합니다."""

        ...

    def predict_entities(
        self,
        text: str,
        labels: Sequence[str],
        threshold: float,
    ) -> object:
        """모델 고유 형식의 개체 목록을 반환합니다."""

        ...


class GlinerPredictor:
    """GLiNER 객체의 길이 검증과 실제 추론을 캡슐화합니다."""

    def __init__(
        self,
        model: object,
        *,
        model_name: str,
        model_revision: str,
        device: str,
    ) -> None:
        self._model = model
        self.model_name = model_name
        self.model_revision = model_revision
        self.device = device

    def ensure_input_supported(
        self,
        text: str,
        labels: Sequence[str],
    ) -> None:
        """단어와 subword 제한을 모두 확인해 자동 truncation을 차단합니다."""

        model = self._model
        try:
            prepared = model.prepare_inputs([text])  # type: ignore[attr-defined]
            word_sequences = prepared[0]
            words = word_sequences[0]
            max_words = int(model.config.max_len)  # type: ignore[attr-defined]
        except (AttributeError, IndexError, TypeError, ValueError):
            raise NerModelOutputError(
                "모델 입력 길이를 검증할 수 없습니다"
            ) from None

        if len(words) > max_words:
            raise NerInputTooLongError(max_words)

        try:
            processor = model.data_processor  # type: ignore[attr-defined]
            prompted, _ = processor.prepare_inputs(
                word_sequences,
                list(labels),
            )
            tokenizer = processor.transformer_tokenizer
            tokenized = tokenizer(
                prompted,
                is_split_into_words=True,
                truncation=False,
                padding=False,
            )
            input_ids = tokenized["input_ids"][0]
            max_subwords = self._resolve_encoder_max_subwords(model)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise NerModelOutputError(
                "모델 tokenizer 길이를 검증할 수 없습니다"
            ) from None

        if len(input_ids) > max_subwords:
            raise NerInputTooLongError(max_subwords)

    @staticmethod
    def _resolve_encoder_max_subwords(model: object) -> int:
        """GLiNER 내부 Transformer 설정에서 실제 위치 임베딩 한도를 읽습니다."""

        try:
            token_rep_layer = model.model.token_rep_layer  # type: ignore[attr-defined]
            if hasattr(token_rep_layer, "bert_layer"):
                encoder = token_rep_layer.bert_layer.model
            elif hasattr(token_rep_layer, "decoder_layer"):
                encoder = token_rep_layer.decoder_layer.model
            else:
                raise AttributeError
            raw_limit = encoder.config.max_position_embeddings
        except AttributeError:
            raise NerModelOutputError(
                "모델 Encoder 길이를 검증할 수 없습니다"
            ) from None

        if (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, Integral)
            or raw_limit < 1
        ):
            raise NerModelOutputError(
                "모델 Encoder 길이 설정이 올바르지 않습니다"
            )
        return int(raw_limit)

    def predict_entities(
        self,
        text: str,
        labels: Sequence[str],
        threshold: float,
    ) -> object:
        """LPL 라벨을 모델 의미 라벨로 바꿔 추론한 뒤 원래 라벨로 복원합니다."""

        model_labels, response_labels = self._resolve_model_labels(labels)
        result = self._model.predict_entities(  # type: ignore[attr-defined]
            text,
            model_labels,
            threshold=threshold,
        )
        if not response_labels or not isinstance(result, Sequence):
            return result

        normalized_result: list[object] = []
        for item in result:
            if not isinstance(item, Mapping):
                normalized_result.append(item)
                continue
            copied_item = dict(item)
            raw_label = copied_item.get("label")
            if type(raw_label) is str and raw_label in response_labels:
                copied_item["label"] = response_labels[raw_label]
            normalized_result.append(copied_item)
        return normalized_result

    @staticmethod
    def _resolve_model_labels(
        labels: Sequence[str],
    ) -> tuple[list[str], dict[str, str]]:
        """LPL의 고정 한국어 라벨 세트만 영어 의미 라벨로 변환합니다."""

        if (
            len(labels) != len(LPL_LABEL_TRANSLATIONS)
            or frozenset(labels) != frozenset(LPL_LABEL_TRANSLATIONS)
        ):
            return list(labels), {}

        model_labels = [LPL_LABEL_TRANSLATIONS[label] for label in labels]
        response_labels = {
            translated: original
            for original, translated in LPL_LABEL_TRANSLATIONS.items()
        }
        return model_labels, response_labels


class GlinerInferenceService:
    """GPU 호출을 직렬화하고 모델 출력을 신뢰 경계에서 검증합니다."""

    def __init__(
        self,
        predictor: EntityPredictor,
        *,
        max_text_characters: int,
    ) -> None:
        if not isinstance(predictor, EntityPredictor):
            raise TypeError("predictor가 EntityPredictor 계약을 만족하지 않습니다")
        if (
            type(max_text_characters) is not int
            or max_text_characters < 1
        ):
            raise ValueError("max_text_characters는 1 이상의 정수여야 합니다")
        self._predictor = predictor
        self._max_text_characters = max_text_characters
        self._inference_lock = Lock()

    @property
    def model_name(self) -> str:
        """현재 로드한 공개 모델 이름을 반환합니다."""

        return self._predictor.model_name

    @property
    def model_revision(self) -> str:
        """현재 로드한 모델 revision을 반환합니다."""

        return self._predictor.model_revision

    @property
    def device(self) -> str:
        """현재 추론 장치를 반환합니다."""

        return self._predictor.device

    def detect(
        self,
        text: str,
    ) -> NerResponse:
        """서버 소유의 고정 정책으로 원문 전체를 탐지합니다."""

        if len(text) > self._max_text_characters:
            raise NerInputTooLongError()

        labels = STANDARD_NER_LABELS
        try:
            with self._inference_lock:
                self._predictor.ensure_input_supported(text, labels)
                raw_entities = self._predictor.predict_entities(
                    text,
                    labels,
                    STANDARD_NER_THRESHOLD,
                )
        except NerInferenceError:
            raise
        except Exception:
            raise NerInferenceError("NER 모델 추론에 실패했습니다") from None

        return NerResponse(
            detections=self._normalize_entities(
                raw_entities,
                original_text=text,
                allowed_labels=frozenset(labels),
            ),
        )

    @classmethod
    def _normalize_entities(
        cls,
        raw_entities: object,
        *,
        original_text: str,
        allowed_labels: frozenset[str],
    ) -> tuple[NerDetection, ...]:
        """모델 목록을 정규화하고 결정적인 순서로 반환합니다."""

        if (
            not isinstance(raw_entities, Sequence)
            or isinstance(raw_entities, (str, bytes, bytearray))
        ):
            raise NerModelOutputError(
                "NER 모델 출력이 개체 배열이 아닙니다"
            )

        entities = tuple(
            cls._normalize_entity(
                item,
                index=index,
                original_text=original_text,
                allowed_labels=allowed_labels,
            )
            for index, item in enumerate(raw_entities)
        )
        return tuple(
            sorted(
                entities,
                key=lambda entity: (
                    entity.start,
                    entity.end,
                    entity.type,
                    -entity.score,
                ),
            )
        )

    @staticmethod
    def _normalize_entity(
        item: object,
        *,
        index: int,
        original_text: str,
        allowed_labels: frozenset[str],
    ) -> NerDetection:
        """모델 개체 하나의 타입, span, 원문과 점수를 검증합니다."""

        if not isinstance(item, Mapping):
            raise NerModelOutputError(
                f"entities[{index}]가 객체가 아닙니다"
            )

        required_fields = {"start", "end", "text", "label", "score"}
        if not required_fields.issubset(item.keys()):
            raise NerModelOutputError(
                f"entities[{index}]에 필수 필드가 없습니다"
            )

        raw_start = item["start"]
        raw_end = item["end"]
        raw_text = item["text"]
        raw_label = item["label"]
        raw_score = item["score"]

        if (
            isinstance(raw_start, bool)
            or not isinstance(raw_start, Integral)
            or isinstance(raw_end, bool)
            or not isinstance(raw_end, Integral)
        ):
            raise NerModelOutputError(
                f"entities[{index}]의 span이 정수가 아닙니다"
            )
        start = int(raw_start)
        end = int(raw_end)
        if start < 0 or end <= start or end > len(original_text):
            raise NerModelOutputError(
                f"entities[{index}]의 span이 원문 범위를 벗어났습니다"
            )

        expected_text = original_text[start:end]
        if type(raw_text) is not str or raw_text != expected_text:
            raise NerModelOutputError(
                f"entities[{index}]의 text가 원문 span과 다릅니다"
            )
        if (
            type(raw_label) is not str
            or not raw_label.strip()
            or raw_label not in allowed_labels
        ):
            raise NerModelOutputError(
                f"entities[{index}]의 label이 요청 라벨과 다릅니다"
            )
        if (
            isinstance(raw_score, bool)
            or not isinstance(raw_score, Real)
        ):
            raise NerModelOutputError(
                f"entities[{index}]의 score가 숫자가 아닙니다"
            )
        score = float(raw_score)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise NerModelOutputError(
                f"entities[{index}]의 score가 범위를 벗어났습니다"
            )

        try:
            return NerDetection(
                start=start,
                end=end,
                text=expected_text,
                type=raw_label,
                score=score,
            )
        except (ValidationError, UnicodeEncodeError):
            raise NerModelOutputError(
                f"entities[{index}]를 응답으로 변환할 수 없습니다"
            ) from None


def _resolve_device(requested_device: str, torch_module: object) -> str:
    """설정과 CUDA 가용성을 바탕으로 실제 추론 장치를 선택합니다."""

    cuda_available = bool(torch_module.cuda.is_available())  # type: ignore[attr-defined]
    if requested_device == "auto":
        return "cuda" if cuda_available else "cpu"
    if requested_device == "cuda" and not cuda_available:
        raise RuntimeError(
            "GLINER_DEVICE=cuda이지만 CUDA 장치를 사용할 수 없습니다"
        )
    return requested_device


def _download_model_snapshot(
    settings: GlinerServerSettings,
    snapshot_download: object,
) -> Path:
    """고정 revision의 설정과 SafeTensors 가중치만 캐시에 받습니다."""

    snapshot_path = snapshot_download(  # type: ignore[operator]
        repo_id=settings.model_name,
        revision=settings.model_revision,
        cache_dir=str(settings.cache_dir),
        local_files_only=settings.local_files_only,
        allow_patterns=list(MODEL_FILES),
    )
    return Path(snapshot_path)


def _download_backbone_snapshot(
    settings: GlinerServerSettings,
    snapshot_download: object,
) -> Path:
    """내부 Transformer 설정과 Tokenizer를 고정 revision에서 받습니다."""

    snapshot_path = snapshot_download(  # type: ignore[operator]
        repo_id=settings.backbone_name,
        revision=settings.backbone_revision,
        cache_dir=str(settings.cache_dir),
        local_files_only=settings.local_files_only,
        allow_patterns=list(BACKBONE_FILES),
    )
    return Path(snapshot_path)


def _link_or_copy_file(source: Path, destination: Path) -> None:
    """큰 가중치는 복제하지 않도록 hard link를 우선 만들고 필요할 때만 복사합니다."""

    try:
        os.link(source.resolve(strict=True), destination)
    except OSError:
        shutil.copy2(source, destination, follow_symlinks=True)


def _build_staged_model_directory(
    settings: GlinerServerSettings,
    *,
    model_snapshot: Path,
    backbone_snapshot: Path,
) -> Path:
    """원본 캐시는 건드리지 않고 backbone 경로만 고정한 로컬 모델을 조립합니다."""

    config_path = model_snapshot / "gliner_config.json"
    weights_path = model_snapshot / "model.safetensors"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("GLiNER 모델 설정을 읽을 수 없습니다") from error
    if not isinstance(config, dict):
        raise RuntimeError("GLiNER 모델 설정이 JSON 객체가 아닙니다")
    if config.get("model_name") != settings.backbone_name:
        raise RuntimeError(
            "GLiNER 모델이 설정된 backbone과 일치하지 않습니다"
        )
    if not weights_path.is_file():
        raise RuntimeError("GLiNER SafeTensors 가중치가 없습니다")
    missing_backbone_files = [
        file_name
        for file_name in BACKBONE_FILES
        if not (backbone_snapshot / file_name).is_file()
    ]
    if missing_backbone_files:
        raise RuntimeError("GLiNER backbone 설정 또는 Tokenizer가 없습니다")

    identity = "\0".join(
        (
            settings.model_name,
            settings.model_revision,
            settings.backbone_name,
            settings.backbone_revision,
        )
    )
    directory_name = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    staging_root = settings.cache_dir / "gliner-runtime"
    destination = staging_root / directory_name
    expected_backbone = str(backbone_snapshot.resolve(strict=True))

    if destination.is_dir():
        try:
            existing = json.loads(
                (destination / "gliner_config.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, dict)
            and existing.get("model_name") == expected_backbone
            and (destination / "model.safetensors").is_file()
        ):
            return destination
        raise RuntimeError("캐시된 GLiNER 실행 디렉터리가 올바르지 않습니다")

    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = staging_root / f".{directory_name}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        config["model_name"] = expected_backbone
        (temporary / "gliner_config.json").write_text(
            json.dumps(
                config,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        _link_or_copy_file(
            weights_path,
            temporary / "model.safetensors",
        )
        try:
            temporary.rename(destination)
        except FileExistsError:
            pass
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def load_inference_service(
    settings: GlinerServerSettings,
) -> GlinerInferenceService:
    """고정 모델을 한 번 로드하여 요청 간 공유할 서비스를 만듭니다."""

    try:
        import torch
        from gliner import GLiNER
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError(
            "GLiNER 런타임 패키지가 설치되지 않았습니다"
        ) from error

    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    device = _resolve_device(settings.device, torch)
    snapshot_path = _download_model_snapshot(
        settings,
        snapshot_download,
    )
    backbone_path = _download_backbone_snapshot(
        settings,
        snapshot_download,
    )
    staged_model_path = _build_staged_model_directory(
        settings,
        model_snapshot=snapshot_path,
        backbone_snapshot=backbone_path,
    )
    model = GLiNER.from_pretrained(
        str(staged_model_path),
        cache_dir=str(settings.cache_dir),
        local_files_only=True,
        map_location=device,
        compile_torch_model=False,
        quantize=None,
    )
    model.eval()

    return GlinerInferenceService(
        GlinerPredictor(
            model,
            model_name=settings.model_name,
            model_revision=settings.model_revision,
            device=device,
        ),
        max_text_characters=settings.max_text_characters,
    )


__all__ = [
    "BACKBONE_FILES",
    "EntityPredictor",
    "GlinerInferenceService",
    "GlinerPredictor",
    "LPL_LABEL_TRANSLATIONS",
    "MODEL_FILES",
    "NerInferenceError",
    "NerInputTooLongError",
    "NerModelOutputError",
    "STANDARD_NER_LABELS",
    "STANDARD_NER_THRESHOLD",
    "load_inference_service",
]
