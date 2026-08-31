"""검증된 Registry Snapshot의 활성 상태와 Reload를 관리합니다."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from typing import Protocol

from app.registry.snapshot import ActiveRegistrySnapshot


class SnapshotBuilder(Protocol):
    """파일 전체에서 검증된 후보 Registry Snapshot을 만듭니다."""

    def build(self) -> ActiveRegistrySnapshot:
        """현재 설정으로 완성된 후보 Snapshot을 반환합니다."""


class ReloadStatus(str, Enum):
    """Registry Reload 시도 결과를 나타냅니다."""

    APPLIED = "APPLIED"
    NO_CHANGE = "NO_CHANGE"
    REJECTED = "REJECTED"


class RegistryManagerNotInitializedError(RuntimeError):
    """초기 Snapshot이 활성화되기 전에 상태를 조회하면 발생합니다."""


@dataclass(frozen=True, slots=True)
class RegistryManagerState:
    """현재 활성 Snapshot과 Reload 진단 정보를 함께 보관합니다.

    ``last_success_at``은 후보 Snapshot 전체를 마지막으로 정상 검증한
    시각입니다. 변경 사항이 없는 성공적인 Reload도 이 시각을 갱신합니다.
    ``last_failure_at``은 최근 실패 이력을 위해 성공 후에도 보존합니다.
    """

    snapshot: ActiveRegistrySnapshot
    generation: int
    last_success_at: datetime
    last_error: str | None = None
    last_failure_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """Registry Reload 결과와 그 직후의 Manager 상태를 반환합니다."""

    status: ReloadStatus
    state: RegistryManagerState
    error: Exception | None = None


class RegistryManager:
    """정상 후보만 활성화하고 요청에 불변 Snapshot을 제공합니다."""

    def __init__(self, builder: SnapshotBuilder) -> None:
        self._builder = builder
        self._state: RegistryManagerState | None = None
        self._state_lock = Lock()
        self._reload_lock = Lock()

    @property
    def state(self) -> RegistryManagerState:
        """현재 Manager 상태를 짧은 잠금 안에서 조회합니다."""

        with self._state_lock:
            state = self._state

        if state is None:
            raise RegistryManagerNotInitializedError(
                "RegistryManager가 아직 초기화되지 않았습니다."
            )
        return state

    def initialize(self) -> RegistryManagerState:
        """최초 후보를 검증한 뒤 generation 1로 활성화합니다.

        최초 후보 생성에 실패하면 대체할 정상 Snapshot이 없으므로 원래
        예외를 호출자에게 전달하고 초기화되지 않은 상태를 유지합니다.
        이미 초기화된 경우에는 기존 상태를 그대로 반환합니다.
        """

        with self._reload_lock:
            with self._state_lock:
                current = self._state
            if current is not None:
                return current

            candidate = self._builder.build()
            initialized_state = RegistryManagerState(
                snapshot=candidate,
                generation=1,
                last_success_at=self._now(),
            )

            with self._state_lock:
                self._state = initialized_state

            return initialized_state

    def capture(self) -> ActiveRegistrySnapshot:
        """요청이 끝까지 사용할 현재 활성 Snapshot 참조를 반환합니다."""

        return self.state.snapshot

    def try_reload(self) -> ReloadResult:
        """새 후보 전체가 정상일 때만 활성 Snapshot을 교체합니다."""

        with self._reload_lock:
            current = self.state

            try:
                candidate = self._builder.build()
            except Exception as error:
                rejected_state = replace(
                    current,
                    last_error=self._format_error(error),
                    last_failure_at=self._now(),
                )
                self._replace_state(rejected_state)
                return ReloadResult(
                    status=ReloadStatus.REJECTED,
                    state=rejected_state,
                    error=error,
                )

            if candidate.snapshot_id == current.snapshot.snapshot_id:
                unchanged_state = replace(
                    current,
                    last_success_at=self._now(),
                    last_error=None,
                )
                self._replace_state(unchanged_state)
                return ReloadResult(
                    status=ReloadStatus.NO_CHANGE,
                    state=unchanged_state,
                )

            applied_state = RegistryManagerState(
                snapshot=candidate,
                generation=current.generation + 1,
                last_success_at=self._now(),
                last_failure_at=current.last_failure_at,
            )
            self._replace_state(applied_state)
            return ReloadResult(
                status=ReloadStatus.APPLIED,
                state=applied_state,
            )

    def _replace_state(self, state: RegistryManagerState) -> None:
        """완성된 불변 상태 객체의 참조만 짧은 잠금으로 교체합니다."""

        with self._state_lock:
            self._state = state

    @staticmethod
    def _format_error(error: Exception) -> str:
        """Reload 예외를 상태 조회에 적합한 짧은 문자열로 변환합니다."""

        message = str(error)
        error_name = type(error).__name__
        return f"{error_name}: {message}" if message else error_name

    @staticmethod
    def _now() -> datetime:
        """비교 가능한 UTC 기준 시각을 반환합니다."""

        return datetime.now(timezone.utc)


__all__ = [
    "RegistryManager",
    "RegistryManagerNotInitializedError",
    "RegistryManagerState",
    "ReloadResult",
    "ReloadStatus",
    "SnapshotBuilder",
]
