"""GLiNER 서버 환경변수 설정의 기본값과 오류를 검증합니다."""

from __future__ import annotations

import pytest

from services.gliner_ner.gliner_ner_server.settings import (
    DEFAULT_BACKBONE_NAME,
    DEFAULT_BACKBONE_REVISION,
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_REVISION,
    GlinerServerSettings,
)


def test_settings_use_pinned_model_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경변수가 없으면 고정한 모델과 revision을 사용합니다."""

    for name in (
        "GLINER_MODEL_NAME",
        "GLINER_MODEL_REVISION",
        "GLINER_BACKBONE_NAME",
        "GLINER_BACKBONE_REVISION",
        "GLINER_DEVICE",
        "GLINER_CACHE_DIR",
        "GLINER_MAX_TEXT_CHARACTERS",
        "GLINER_LOCAL_FILES_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = GlinerServerSettings.from_environment()

    assert settings.model_name == DEFAULT_MODEL_NAME
    assert settings.model_revision == DEFAULT_MODEL_REVISION
    assert settings.backbone_name == DEFAULT_BACKBONE_NAME
    assert settings.backbone_revision == DEFAULT_BACKBONE_REVISION
    assert settings.device == "cuda"
    assert settings.local_files_only is False


def test_settings_accept_explicit_cpu_and_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """개발 환경에서는 CPU와 사전 예열된 오프라인 캐시를 선택할 수 있습니다."""

    monkeypatch.setenv("GLINER_DEVICE", "cpu")
    monkeypatch.setenv("GLINER_LOCAL_FILES_ONLY", "true")
    monkeypatch.setenv("GLINER_MAX_TEXT_CHARACTERS", "5000")

    settings = GlinerServerSettings.from_environment()

    assert settings.device == "cpu"
    assert settings.local_files_only is True
    assert settings.max_text_characters == 5000


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GLINER_DEVICE", "gpu"),
        ("GLINER_MAX_TEXT_CHARACTERS", "0"),
        ("GLINER_MAX_TEXT_CHARACTERS", "not-an-integer"),
        ("GLINER_LOCAL_FILES_ONLY", "yes"),
    ],
)
def test_settings_reject_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """모호하거나 안전하지 않은 환경변수 값을 시작 시 거부합니다."""

    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        GlinerServerSettings.from_environment()
