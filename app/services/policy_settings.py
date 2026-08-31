"""전역 활성 정책 설정을 원자 저장하고 실행 상태로 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.core.json_codec import dump_json_utf8, load_strict_json
from app.registry.file_store import DEFAULT_CONFIG_DIR
from app.schemas.policy_settings import (
    PolicySettings,
    default_policy_settings,
)


POLICY_SETTINGS_FILENAME = "policy_settings.json"


class PolicySettingsNotInitializedError(RuntimeError):
    """활성 정책 설정이 초기화되기 전에 조회하면 발생합니다."""


class PolicySettingsStorageError(RuntimeError):
    """활성 정책 설정 파일을 읽거나 원자 저장할 수 없을 때 발생합니다."""


class PolicySettingsFileStore:
    """policy_settings.json 하나를 엄격하게 읽고 원자 교체합니다."""

    def __init__(self, config_dir: str | Path = DEFAULT_CONFIG_DIR) -> None:
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / POLICY_SETTINGS_FILENAME
        self._lock = Lock()

    def load_or_initialize(self) -> PolicySettings:
        """기존 설정을 읽고, 없으면 전체 활성 기본 파일을 생성합니다."""

        with self._lock:
            if not self.path.exists():
                settings = default_policy_settings()
                self._replace_atomically(settings)
                return settings
            return self._load()

    def replace(self, settings: PolicySettings) -> None:
        """검증된 설정 전체를 임시 파일 작성 후 원자 교체합니다."""

        if type(settings) is not PolicySettings:
            raise TypeError("settings는 PolicySettings여야 합니다")
        with self._lock:
            self._replace_atomically(settings)

    def _load(self) -> PolicySettings:
        """현재 파일을 엄격한 JSON과 camelCase 계약으로 검증합니다."""

        return PolicySettings.model_validate(
            load_strict_json(self.path.read_bytes()),
            by_alias=True,
            by_name=False,
        )

    def _replace_atomically(self, settings: PolicySettings) -> None:
        """완성된 JSON만 공개 경로로 교체하고 임시 파일을 정리합니다."""

        self.config_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(
            f".{self.path.name}.{uuid4().hex}.tmp"
        )
        payload = settings.model_dump(
            by_alias=True,
            mode="json",
        )
        try:
            with temporary_path.open(
                "x",
                encoding="utf-8",
                newline="\n",
            ) as output_file:
                output_file.write(
                    f"{dump_json_utf8(payload, indent=2).decode('utf-8')}\n"
                )
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class PolicySettingsState:
    """현재 활성 설정과 변경 generation입니다."""

    settings: PolicySettings
    generation: int


class PolicySettingsManager:
    """파일 변경과 활성 불변 설정 참조 교체를 직렬화합니다."""

    def __init__(self, store: PolicySettingsFileStore) -> None:
        self._store = store
        self._state: PolicySettingsState | None = None
        self._state_lock = Lock()
        self._mutation_lock = Lock()

    def initialize(self) -> PolicySettingsState:
        """파일을 로드하거나 기본 생성한 뒤 generation 1로 활성화합니다."""

        with self._mutation_lock:
            with self._state_lock:
                current = self._state
            if current is not None:
                return current
            try:
                settings = self._store.load_or_initialize()
            except Exception as error:
                raise PolicySettingsStorageError(
                    "활성 정책 설정을 초기화할 수 없습니다"
                ) from error
            state = PolicySettingsState(settings=settings, generation=1)
            with self._state_lock:
                self._state = state
            return state

    def capture(self) -> PolicySettings:
        """요청 전체가 사용할 현재 불변 설정을 짧은 잠금으로 반환합니다."""

        with self._state_lock:
            state = self._state
        if state is None:
            raise PolicySettingsNotInitializedError(
                "활성 정책 설정이 아직 초기화되지 않았습니다"
            )
        return state.settings

    def replace(self, settings: PolicySettings) -> PolicySettingsState:
        """정상 파일 저장 후에만 새 활성 설정 참조로 교체합니다."""

        if type(settings) is not PolicySettings:
            raise TypeError("settings는 PolicySettings여야 합니다")
        with self._mutation_lock:
            with self._state_lock:
                current = self._state
            if current is None:
                raise PolicySettingsNotInitializedError(
                    "활성 정책 설정이 아직 초기화되지 않았습니다"
                )
            if settings == current.settings:
                return current
            try:
                self._store.replace(settings)
            except Exception as error:
                raise PolicySettingsStorageError(
                    "활성 정책 설정을 저장할 수 없습니다"
                ) from error
            state = PolicySettingsState(
                settings=settings,
                generation=current.generation + 1,
            )
            with self._state_lock:
                self._state = state
            return state


__all__ = [
    "POLICY_SETTINGS_FILENAME",
    "PolicySettingsFileStore",
    "PolicySettingsManager",
    "PolicySettingsNotInitializedError",
    "PolicySettingsState",
    "PolicySettingsStorageError",
]
