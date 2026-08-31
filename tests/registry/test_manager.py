"""RegistryManager의 원자적 상태 교체와 실패 복구를 검증합니다."""

from __future__ import annotations

from collections import deque
from dataclasses import FrozenInstanceError
from threading import Event, Lock, Thread

import pytest

from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
    RegistryManagerState,
    ReloadStatus,
)
from app.registry.snapshot import ActiveRegistrySnapshot
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact


def _prompt() -> PromptArtifact:
    """Manager 테스트용 고정 Prompt Artifact를 만듭니다."""

    return PromptArtifact.compile(
        "{{ text }} / {{ existing_detections }}",
        renderer=PromptRenderer(),
        template_path="test",
    )


def _title_prompt() -> TitlePromptArtifact:
    """Manager Snapshot에 필요한 정적 제목 Prompt를 만듭니다."""

    return TitlePromptArtifact.compile(
        "제목만 생성하십시오.",
        renderer=PromptRenderer(),
        template_path="title-test",
    )


def _mask_prompt() -> MaskPromptArtifact:
    """Manager Snapshot에 필요한 정적 마스킹 Prompt를 만듭니다."""

    return MaskPromptArtifact.compile(
        "탐지 구간을 마스킹하십시오.",
        renderer=PromptRenderer(),
        template_path="mask-test",
    )


def _snapshot(snapshot_id: str) -> ActiveRegistrySnapshot:
    """Manager 동작 검증에 사용할 빈 Registry Snapshot을 만듭니다."""

    return ActiveRegistrySnapshot(
        snapshot_id=snapshot_id,
        deployments={},
        detection_prompt=_prompt(),
        mask_prompt=_mask_prompt(),
        title_prompt=_title_prompt(),
    )


class ScriptedBuilder:
    """호출 순서대로 Snapshot 또는 예외를 반환합니다."""

    def __init__(
        self,
        *outcomes: ActiveRegistrySnapshot | Exception,
    ) -> None:
        self.outcomes = deque(outcomes)
        self.build_calls = 0

    def build(self) -> ActiveRegistrySnapshot:
        """다음으로 예약된 결과를 반환하거나 예외를 발생시킵니다."""

        self.build_calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_access_before_initialization_is_rejected() -> None:
    """초기화 전에는 Snapshot과 Manager 상태를 조회할 수 없는지 검증합니다."""

    manager = RegistryManager(ScriptedBuilder(_snapshot("snapshot-v1")))

    with pytest.raises(RegistryManagerNotInitializedError):
        _ = manager.state
    with pytest.raises(RegistryManagerNotInitializedError):
        manager.capture()
    with pytest.raises(RegistryManagerNotInitializedError):
        manager.try_reload()


def test_initialize_activates_generation_one_and_is_idempotent() -> None:
    """최초 Snapshot을 한 번만 만들고 generation 1로 활성화하는지 검증합니다."""

    snapshot = _snapshot("snapshot-v1")
    builder = ScriptedBuilder(snapshot)
    manager = RegistryManager(builder)

    initialized = manager.initialize()
    initialized_again = manager.initialize()

    assert initialized.snapshot is snapshot
    assert initialized.generation == 1
    assert initialized.last_success_at.tzinfo is not None
    assert initialized.last_error is None
    assert initialized.last_failure_at is None
    assert initialized_again is initialized
    assert manager.capture() is snapshot
    assert builder.build_calls == 1


def test_manager_state_is_immutable() -> None:
    """호출자가 반환된 Manager 상태를 직접 변경할 수 없는지 검증합니다."""

    manager = RegistryManager(ScriptedBuilder(_snapshot("snapshot-v1")))
    state = manager.initialize()

    with pytest.raises(FrozenInstanceError):
        state.generation = 2


def test_initialize_failure_keeps_manager_uninitialized() -> None:
    """최초 후보 생성 실패 시 불완전한 상태를 활성화하지 않는지 검증합니다."""

    manager = RegistryManager(
        ScriptedBuilder(ValueError("잘못된 초기 Registry"))
    )

    with pytest.raises(ValueError, match="잘못된 초기 Registry"):
        manager.initialize()
    with pytest.raises(RegistryManagerNotInitializedError):
        manager.capture()


def test_reload_applies_changed_snapshot_and_increments_generation() -> None:
    """Snapshot ID가 바뀐 정상 후보만 다음 generation으로 교체하는지 검증합니다."""

    first = _snapshot("snapshot-v1")
    second = _snapshot("snapshot-v2")
    manager = RegistryManager(ScriptedBuilder(first, second))
    initial_state = manager.initialize()

    result = manager.try_reload()

    assert result.status is ReloadStatus.APPLIED
    assert result.error is None
    assert result.state.snapshot is second
    assert result.state.generation == 2
    assert result.state.last_success_at >= initial_state.last_success_at
    assert manager.state is result.state
    assert manager.capture() is second


def test_reload_discards_unchanged_candidate() -> None:
    """동일한 Snapshot ID 후보는 활성 객체와 generation을 유지하는지 검증합니다."""

    active = _snapshot("same-content")
    equivalent_candidate = _snapshot("same-content")
    manager = RegistryManager(
        ScriptedBuilder(active, equivalent_candidate)
    )
    initial_state = manager.initialize()

    result = manager.try_reload()

    assert result.status is ReloadStatus.NO_CHANGE
    assert result.state.snapshot is active
    assert result.state.snapshot is not equivalent_candidate
    assert result.state.generation == initial_state.generation
    assert result.state.last_success_at >= initial_state.last_success_at
    assert manager.capture() is active


def test_rejected_reload_keeps_active_snapshot_and_generation() -> None:
    """후보 생성 실패 시 마지막 정상 Snapshot과 generation을 보존하는지 검증합니다."""

    active = _snapshot("snapshot-v1")
    failure = ValueError("Prompt 문법 오류")
    manager = RegistryManager(ScriptedBuilder(active, failure))
    initial_state = manager.initialize()

    result = manager.try_reload()

    assert result.status is ReloadStatus.REJECTED
    assert result.error is failure
    assert result.state.snapshot is active
    assert result.state.generation == initial_state.generation
    assert result.state.last_success_at == initial_state.last_success_at
    assert result.state.last_error == "ValueError: Prompt 문법 오류"
    assert result.state.last_failure_at is not None
    assert manager.capture() is active


def test_success_after_rejection_clears_error_and_keeps_failure_time() -> None:
    """실패 뒤 정상 Reload가 현재 오류를 지우고 최근 실패 이력은 보존하는지 검증합니다."""

    first = _snapshot("snapshot-v1")
    second = _snapshot("snapshot-v2")
    manager = RegistryManager(
        ScriptedBuilder(first, RuntimeError("임시 오류"), second)
    )
    manager.initialize()
    rejected = manager.try_reload()

    applied = manager.try_reload()

    assert applied.status is ReloadStatus.APPLIED
    assert applied.state.snapshot is second
    assert applied.state.generation == 2
    assert applied.state.last_error is None
    assert applied.state.last_failure_at == rejected.state.last_failure_at


def test_builder_runs_without_holding_state_lock() -> None:
    """파일 읽기와 컴파일 중에는 상태 조회 Lock을 점유하지 않는지 검증합니다."""

    first = _snapshot("snapshot-v1")
    second = _snapshot("snapshot-v2")
    manager: RegistryManager

    class LockInspectingBuilder:
        """두 번째 Build에서 Manager의 상태 Lock 점유 여부를 검사합니다."""

        def __init__(self) -> None:
            self.build_calls = 0

        def build(self) -> ActiveRegistrySnapshot:
            """상태 Lock을 즉시 획득할 수 있는지 확인하고 후보를 반환합니다."""

            self.build_calls += 1
            if self.build_calls == 2:
                acquired = manager._state_lock.acquire(blocking=False)
                assert acquired, "Builder 실행 중 state_lock이 점유되었습니다."
                manager._state_lock.release()
            return first if self.build_calls == 1 else second

    builder = LockInspectingBuilder()
    manager = RegistryManager(builder)
    manager.initialize()

    result = manager.try_reload()

    assert result.status is ReloadStatus.APPLIED


def test_reload_builds_are_serialized() -> None:
    """동시에 요청된 Reload가 후보 Snapshot을 하나씩 생성하는지 검증합니다."""

    first_reload_started = Event()
    allow_first_reload_to_finish = Event()
    second_reload_requested = Event()

    class ConcurrentBuilder:
        """동시 Build 수를 기록하고 첫 번째 Reload를 일시 정지합니다."""

        def __init__(self) -> None:
            self.call_count = 0
            self.active_builds = 0
            self.max_active_builds = 0
            self.counter_lock = Lock()

        def build(self) -> ActiveRegistrySnapshot:
            """Build 중첩 횟수를 측정하며 호출별 Snapshot을 반환합니다."""

            with self.counter_lock:
                self.call_count += 1
                call_number = self.call_count
                self.active_builds += 1
                self.max_active_builds = max(
                    self.max_active_builds,
                    self.active_builds,
                )

            if call_number == 2:
                first_reload_started.set()
                assert allow_first_reload_to_finish.wait(timeout=2)

            with self.counter_lock:
                self.active_builds -= 1

            return _snapshot(f"snapshot-v{call_number}")

    builder = ConcurrentBuilder()
    manager = RegistryManager(builder)
    manager.initialize()
    results = []

    def reload_once(started: Event | None = None) -> None:
        """선택한 시작 신호를 남기고 Reload 결과를 수집합니다."""

        if started is not None:
            started.set()
        results.append(manager.try_reload())

    first_thread = Thread(target=reload_once)
    second_thread = Thread(
        target=reload_once,
        args=(second_reload_requested,),
    )
    first_thread.start()
    assert first_reload_started.wait(timeout=2)
    second_thread.start()
    assert second_reload_requested.wait(timeout=2)
    allow_first_reload_to_finish.set()

    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert builder.max_active_builds == 1
    assert [result.status for result in results] == [
        ReloadStatus.APPLIED,
        ReloadStatus.APPLIED,
    ]
    assert manager.state.generation == 3
