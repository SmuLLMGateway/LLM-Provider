"""전역 활성 정책 설정 FileStore와 Manager를 검증합니다."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.detection import ALLOWED_POLICY_IDS
from app.schemas.policy_settings import PolicySettings
from app.services.policy_settings import (
    POLICY_SETTINGS_FILENAME,
    PolicySettingsFileStore,
    PolicySettingsManager,
    PolicySettingsNotInitializedError,
    PolicySettingsStorageError,
)


def _settings(*policy_ids: str) -> PolicySettings:
    """테스트용 활성 정책 설정을 엄격한 모델로 만듭니다."""

    return PolicySettings.model_validate(
        {"enabledPolicies": list(policy_ids)}
    )


def test_store_initializes_missing_file_with_all_policies(tmp_path) -> None:
    """기존 배포 볼륨에 파일이 없으면 전체 활성 기본값을 원자 생성합니다."""

    store = PolicySettingsFileStore(tmp_path)

    result = store.load_or_initialize()

    assert result.enabled_policy_ids == ALLOWED_POLICY_IDS
    path = tmp_path / POLICY_SETTINGS_FILENAME
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "enabledPolicies": list(ALLOWED_POLICY_IDS)
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"enabledPolicies": []},
        {"enabledPolicies": ["P01", "P01"]},
        {"enabledPolicies": ["UNKNOWN"]},
        {"enabledPolicies": ["P01"], "extra": True},
        {"enabled_policy_ids": ["P01"]},
    ],
)
def test_store_rejects_invalid_existing_file(tmp_path, payload) -> None:
    """빈·중복·미등록·추가·snake_case 파일 설정을 거부합니다."""

    path = tmp_path / POLICY_SETTINGS_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((ValidationError, ValueError)):
        PolicySettingsFileStore(tmp_path).load_or_initialize()


def test_store_replaces_and_loads_canonical_policy_order(tmp_path) -> None:
    """전체 교체한 설정을 다음 로드에서도 고정 순서로 반환합니다."""

    store = PolicySettingsFileStore(tmp_path)
    store.load_or_initialize()
    settings = _settings("B01", "P03", "P01")

    store.replace(settings)

    assert store.load_or_initialize().enabled_policy_ids == (
        "P01",
        "P03",
        "B01",
    )
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_manager_requires_initialization(tmp_path) -> None:
    """활성 상태 생성 전에 조회·교체하지 못합니다."""

    manager = PolicySettingsManager(PolicySettingsFileStore(tmp_path))

    with pytest.raises(PolicySettingsNotInitializedError):
        manager.capture()
    with pytest.raises(PolicySettingsNotInitializedError):
        manager.replace(_settings("P01"))


def test_manager_activates_replacement_and_tracks_generation(tmp_path) -> None:
    """정상 저장 후에만 새 설정 참조와 generation을 교체합니다."""

    manager = PolicySettingsManager(PolicySettingsFileStore(tmp_path))
    first = manager.initialize()
    replacement = _settings("S01", "B02")

    second = manager.replace(replacement)
    unchanged = manager.replace(replacement)

    assert first.generation == 1
    assert second.generation == 2
    assert unchanged is second
    assert manager.capture() is replacement


class FailingStore:
    """초기화 또는 저장 실패를 선택적으로 발생시키는 Store 대역입니다."""

    def __init__(self, *, fail_load: bool = False) -> None:
        self.fail_load = fail_load

    def load_or_initialize(self) -> PolicySettings:
        if self.fail_load:
            raise OSError("load failed")
        return _settings("P01")

    def replace(self, settings: PolicySettings) -> None:
        del settings
        raise OSError("replace failed")


def test_manager_wraps_initialization_storage_error() -> None:
    """초기 파일 오류를 안전한 설정 저장 오류로 분류합니다."""

    manager = PolicySettingsManager(FailingStore(fail_load=True))  # type: ignore[arg-type]

    with pytest.raises(PolicySettingsStorageError):
        manager.initialize()


def test_manager_preserves_active_settings_when_replace_fails() -> None:
    """파일 교체 실패 시 기존 활성 설정을 그대로 유지합니다."""

    manager = PolicySettingsManager(FailingStore())  # type: ignore[arg-type]
    manager.initialize()

    with pytest.raises(PolicySettingsStorageError):
        manager.replace(_settings("P03"))

    assert manager.capture().enabled_policy_ids == ("P01",)

