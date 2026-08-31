"""Deployment 변경 저장, Snapshot 활성화와 실패 복구를 검증합니다."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from app.backends.provider_registry import BackendProviderRegistry
from app.core.registry_file_coordinator import RegistryFileCoordinator
from app.registry.file_store import RegistryFileStore
from app.registry.manager import RegistryManager, ReloadStatus
from app.registry.mutator import (
    DeploymentAlreadyExistsError,
    DeploymentMustBeDisabledError,
    DeploymentNotFoundError,
    RegistryMutator,
)
from app.registry.store import RegistryStore
from app.registry.snapshot_builder import RegistrySnapshotBuilder
from app.registry.validator import RegistryValidator
from app.prompts.prompt_loader import PromptLoader
from app.prompts.policy_prompt_loader import (
    DEFAULT_POLICY_PROMPTS_PATH,
    POLICY_PROMPTS_FILENAME,
)
from app.prompts.prompt_renderer import PromptRenderer
from app.schemas.registry import DeploymentConfig, RegistryConfig
from app.services.deployment_management import (
    DeploymentActivationError,
    DeploymentManagementService,
    DeploymentRollbackError,
    DeploymentStorageError,
)


class InMemoryRegistryStore:
    """실제 Store와 같은 검증·갱신 계약을 메모리에서 제공합니다."""

    def __init__(
        self,
        registry: RegistryConfig | None = None,
        *,
        fail_on_update_call: int | None = None,
    ) -> None:
        self.validator = RegistryValidator()
        self._registry = registry or RegistryConfig(deployments={})
        self._lock = Lock()
        self.update_calls = 0
        self.fail_on_update_call = fail_on_update_call

    def load(self) -> RegistryConfig:
        """호출자가 내부 맵을 공유하지 않도록 현재 설정을 복사해 반환합니다."""

        with self._lock:
            return RegistryConfig(
                deployments=dict(self._registry.deployments)
            )

    def update(
        self,
        updater: Callable[[RegistryConfig], RegistryConfig],
    ) -> RegistryConfig:
        """Updater 결과를 검증한 뒤 한 번에 현재 설정으로 교체합니다."""

        with self._lock:
            self.update_calls += 1
            if self.update_calls == self.fail_on_update_call:
                raise OSError("복원 저장 실패")

            working = RegistryConfig(
                deployments=dict(self._registry.deployments)
            )
            updated = RegistryConfig.model_validate(updater(working))
            self.validator.validate_registry(updated)
            self._registry = RegistryConfig(
                deployments=dict(updated.deployments)
            )
            return updated


class ScriptedReloadManager:
    """호출 순서대로 Reload 결과 또는 예외를 반환합니다."""

    def __init__(
        self,
        *outcomes: ReloadStatus | tuple[ReloadStatus, Exception] | Exception,
    ) -> None:
        self.outcomes = deque(outcomes)
        self.try_reload_calls = 0

    def try_reload(self) -> SimpleNamespace:
        """다음으로 예약된 Reload 결과를 반환하거나 예외를 발생시킵니다."""

        self.try_reload_calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            status, error = outcome
            return SimpleNamespace(status=status, error=error)
        return SimpleNamespace(status=outcome, error=None)


class OppositeKindMutationReloadManager:
    """후보 거부 직전에 반대 종류 설정이 저장되는 경쟁을 재현합니다."""

    def __init__(self, store: InMemoryRegistryStore) -> None:
        self.store = store
        self.try_reload_calls = 0

    def try_reload(self) -> SimpleNamespace:
        """첫 Reload에서 LLM을 추가하고 후보를 거부한 뒤 복원은 승인합니다."""

        self.try_reload_calls += 1
        if self.try_reload_calls == 1:
            self.store.update(
                lambda registry: RegistryConfig(
                    deployments={
                        **registry.deployments,
                        "llm-external": DeploymentConfig.model_validate(
                            _llm()
                        ),
                    }
                )
            )
            return SimpleNamespace(
                status=ReloadStatus.REJECTED,
                error=RuntimeError("후보 거부"),
            )
        return SimpleNamespace(
            status=ReloadStatus.NO_CHANGE,
            error=None,
        )


class BlockingReloadManager:
    """첫 Reload를 멈춰 관리 작업이 겹치는지 확인할 수 있게 합니다."""

    def __init__(self) -> None:
        self.first_reload_started = Event()
        self.allow_first_reload_to_finish = Event()
        self.try_reload_calls = 0
        self._lock = Lock()

    def try_reload(self) -> SimpleNamespace:
        """첫 호출만 외부 신호가 올 때까지 기다린 뒤 성공을 반환합니다."""

        with self._lock:
            self.try_reload_calls += 1
            call_number = self.try_reload_calls

        if call_number == 1:
            self.first_reload_started.set()
            assert self.allow_first_reload_to_finish.wait(timeout=2)
        return SimpleNamespace(
            status=ReloadStatus.APPLIED,
            error=None,
        )


class TrackingRegistryMutator(RegistryMutator):
    """두 번째 Deployment 변경이 저장용 Mutator에 진입했는지 기록합니다."""

    def __init__(
        self,
        validator: RegistryValidator,
        second_mutation_entered: Event,
    ) -> None:
        super().__init__(validator)
        self.second_mutation_entered = second_mutation_entered

    def add_deployment(
        self,
        registry: RegistryConfig,
        *,
        deployment_id: str,
        deployment: DeploymentConfig,
    ) -> RegistryConfig:
        """ner-b 변경이 실제 Mutator 호출까지 도달하면 신호를 남깁니다."""

        if deployment_id == "ner-b":
            self.second_mutation_entered.set()
        return super().add_deployment(
            registry,
            deployment_id=deployment_id,
            deployment=deployment,
        )


def _ner(*, enabled: bool = True) -> dict[str, object]:
    """Management Service 테스트용 공통 HTTP NER 설정을 만듭니다."""

    return {
        "kind": "ner",
        "adapterType": "http_ner",
        "baseUrl": "http://localhost:9100/v1/ner/detect",
        "timeoutMs": 5000,
        "enabled": enabled,
    }


def _llm(*, enabled: bool = True) -> dict[str, object]:
    """Management Service 테스트용 Mock LLM 설정을 만듭니다."""

    return {
        "kind": "llm",
        "adapterType": "mock",
        "enabled": enabled,
    }


def _registry_with_ner(
    *,
    enabled: bool = True,
) -> RegistryConfig:
    """ner-a가 들어 있는 초기 Registry를 만듭니다."""

    return RegistryConfig(
        deployments={
            "ner-a": DeploymentConfig.model_validate(
                _ner(enabled=enabled)
            )
        }
    )


def _service(
    store: InMemoryRegistryStore,
    manager: (
        ScriptedReloadManager
        | OppositeKindMutationReloadManager
        | BlockingReloadManager
    ),
    *,
    mutator: RegistryMutator | None = None,
) -> DeploymentManagementService:
    """테스트 대역을 실제 Management Service에 연결합니다."""

    return DeploymentManagementService(
        store=cast(RegistryStore, store),
        registry_manager=cast(RegistryManager, manager),
        mutator=mutator,
    )


def test_add_deployment_saves_and_activates_candidate() -> None:
    """추가가 성공하면 파일 저장과 Snapshot Reload를 모두 완료합니다."""

    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    created = _service(store, manager).add_deployment(
        "ner-a",
        _ner(),
    )

    assert store.load().deployments["ner-a"] == created
    assert manager.try_reload_calls == 1
    assert store.update_calls == 1


def test_update_deployment_accepts_no_change_reload() -> None:
    """동일 Snapshot인 NO_CHANGE도 정상적인 관리 작업으로 처리합니다."""

    store = InMemoryRegistryStore(_registry_with_ner())
    manager = ScriptedReloadManager(ReloadStatus.NO_CHANGE)

    updated = _service(store, manager).update_deployment(
        "ner-a",
        _ner(),
    )

    assert updated.enabled is True
    assert store.load().deployments["ner-a"] == updated
    assert manager.try_reload_calls == 1
    assert store.update_calls == 1


def test_set_deployment_enabled_saves_and_activates_candidate() -> None:
    """활성 상태 전용 변경도 저장 후 Snapshot을 한 번 Reload합니다."""

    initial = _registry_with_ner(enabled=True)
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    updated = _service(store, manager).set_deployment_enabled(
        "ner-a",
        kind="ner",
        enabled=False,
    )

    assert updated == initial.deployments["ner-a"].model_copy(
        update={"enabled": False}
    )
    assert store.load().deployments["ner-a"] == updated
    assert manager.try_reload_calls == 1
    assert store.update_calls == 1


def test_set_deployment_enabled_accepts_idempotent_reload() -> None:
    """같은 활성 상태를 다시 요청해도 NO_CHANGE를 성공으로 처리합니다."""

    initial = _registry_with_ner(enabled=True)
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(ReloadStatus.NO_CHANGE)

    updated = _service(store, manager).set_deployment_enabled(
        "ner-a",
        kind="ner",
        enabled=True,
    )

    assert updated == initial.deployments["ner-a"]
    assert store.load() == initial
    assert manager.try_reload_calls == 1
    assert store.update_calls == 1


def test_set_enabled_activation_rejection_restores_previous_value() -> None:
    """활성화 후보가 거부되면 이전 enabled 값으로 저장소를 복원합니다."""

    activation_error = RuntimeError("Provider가 등록되지 않았습니다")
    initial = _registry_with_ner(enabled=False)
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(
        (ReloadStatus.REJECTED, activation_error),
        ReloadStatus.NO_CHANGE,
    )

    with pytest.raises(DeploymentActivationError) as error_info:
        _service(store, manager).set_deployment_enabled(
            "ner-a",
            kind="ner",
            enabled=True,
        )

    error = error_info.value
    assert error.operation == "set_enabled"
    assert error.activation_error is activation_error
    assert store.load() == initial
    assert store.load().deployments["ner-a"].enabled is False
    assert store.update_calls == 2
    assert manager.try_reload_calls == 2


def test_delete_inactive_deployment_saves_and_activates_candidate() -> None:
    """비활성 Deployment 삭제를 저장하고 새 Snapshot을 한 번 활성화합니다."""

    initial = _registry_with_ner(enabled=False)
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    result = _service(store, manager).delete_deployment(
        "ner-a",
        kind="ner",
    )

    assert result is None
    assert store.load().deployments == {}
    assert store.update_calls == 1
    assert manager.try_reload_calls == 1


def test_delete_active_deployment_does_not_save_or_reload() -> None:
    """활성 Deployment 삭제 거부 시 후보를 저장하거나 Reload하지 않습니다."""

    initial = _registry_with_ner(enabled=True)
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    with pytest.raises(DeploymentMustBeDisabledError) as error_info:
        _service(store, manager).delete_deployment(
            "ner-a",
            kind="ner",
        )

    assert error_info.value.deployment_id == "ner-a"
    assert store.load() == initial
    assert store.update_calls == 1
    assert manager.try_reload_calls == 0


def test_delete_activation_rejection_restores_deleted_deployment() -> None:
    """삭제 후보 Snapshot이 거부되면 파일 상태를 이전 값으로 복원합니다."""

    activation_error = RuntimeError("삭제 후보 거부")
    initial = _registry_with_ner(enabled=False)
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(
        (ReloadStatus.REJECTED, activation_error),
        ReloadStatus.NO_CHANGE,
    )

    with pytest.raises(DeploymentActivationError) as error_info:
        _service(store, manager).delete_deployment(
            "ner-a",
            kind="ner",
        )

    error = error_info.value
    assert error.operation == "delete"
    assert error.activation_error is activation_error
    assert store.load() == initial
    assert store.load().deployments["ner-a"].enabled is False
    assert store.update_calls == 2
    assert manager.try_reload_calls == 2


def test_delete_rollback_preserves_opposite_kind_mutation() -> None:
    """실패한 NER 삭제 복원은 그 사이 저장된 LLM Deployment를 보존합니다."""

    initial = _registry_with_ner(enabled=False)
    store = InMemoryRegistryStore(initial)
    manager = OppositeKindMutationReloadManager(store)

    with pytest.raises(DeploymentActivationError):
        _service(store, manager).delete_deployment(
            "ner-a",
            kind="ner",
        )

    restored = store.load()
    assert restored.deployments["ner-a"] == initial.deployments["ner-a"]
    assert restored.deployments["llm-external"].kind == "llm"
    assert manager.try_reload_calls == 2


@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        ("add", DeploymentAlreadyExistsError),
        ("update", DeploymentNotFoundError),
    ],
)
def test_editor_error_does_not_reload_or_change_registry(
    operation: str,
    expected_error: type[Exception],
) -> None:
    """중복 추가와 없는 ID 수정은 저장이나 Reload 전에 실패합니다."""

    initial = _registry_with_ner()
    store = InMemoryRegistryStore(initial)
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)
    service = _service(store, manager)

    with pytest.raises(expected_error):
        if operation == "add":
            service.add_deployment("ner-a", _ner(enabled=False))
        else:
            service.update_deployment("missing", _ner())

    assert store.load() == initial
    assert manager.try_reload_calls == 0
    assert store.update_calls == 1


def test_invalid_input_does_not_save_or_reload() -> None:
    """Pydantic 입력 오류는 Registry 파일 작업 전에 그대로 전달합니다."""

    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    with pytest.raises(ValidationError):
        _service(store, manager).add_deployment(
            "ner-a",
            {
                "kind": "ner",
                "adapterType": "http_ner",
                "baseUrl": "http://localhost:9100/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": "yes",
            },
        )

    assert store.load().deployments == {}
    assert manager.try_reload_calls == 0
    assert store.update_calls == 0


@pytest.mark.parametrize("deployment_id", ["", "NER A", "UPPER"])
def test_invalid_deployment_id_does_not_save_or_reload(
    deployment_id: str,
) -> None:
    """잘못된 ID는 Registry 조회·저장과 Snapshot Reload 전에 거부합니다."""

    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    with pytest.raises(ValidationError):
        _service(store, manager).add_deployment(
            deployment_id,
            _ner(),
        )

    assert store.load().deployments == {}
    assert store.update_calls == 0
    assert manager.try_reload_calls == 0


def test_service_uses_store_validator_for_default_mutator() -> None:
    """온라인 변경과 Store 전체 검증이 같은 Registry 정책을 사용합니다."""

    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    service = _service(store, manager)

    assert service.mutator.validator is store.validator


def test_rejected_activation_restores_previous_registry() -> None:
    """후보 Snapshot이 거부되면 이전 파일을 복원하고 다시 Reload합니다."""

    activation_error = RuntimeError("Provider가 등록되지 않았습니다")
    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(
        (ReloadStatus.REJECTED, activation_error),
        ReloadStatus.NO_CHANGE,
    )

    with pytest.raises(DeploymentActivationError) as error_info:
        _service(store, manager).add_deployment("ner-a", _ner())

    error = error_info.value
    assert error.code == "DEPLOYMENT_ACTIVATION_FAILED"
    assert error.deployment_id == "ner-a"
    assert error.operation == "add"
    assert error.activation_error is activation_error
    assert error.__cause__ is activation_error
    assert "Provider가 등록되지 않았습니다" not in str(error)
    assert store.load().deployments == {}
    assert store.update_calls == 2
    assert manager.try_reload_calls == 2


def test_reload_exception_also_restores_previous_registry() -> None:
    """Reload 호출 자체의 예외도 후보 활성화 실패로 보고 복원합니다."""

    activation_error = RuntimeError("Reload 실행 실패")
    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(
        activation_error,
        ReloadStatus.NO_CHANGE,
    )

    with pytest.raises(DeploymentActivationError) as error_info:
        _service(store, manager).add_deployment("ner-a", _ner())

    assert error_info.value.activation_error is activation_error
    assert store.load().deployments == {}
    assert manager.try_reload_calls == 2


def test_rollback_restores_only_changed_kind() -> None:
    """NER 복원 중 별도로 저장된 LLM 설정은 제거하지 않습니다."""

    store = InMemoryRegistryStore()
    manager = OppositeKindMutationReloadManager(store)

    with pytest.raises(DeploymentActivationError):
        _service(store, manager).add_deployment("ner-a", _ner())

    registry = store.load()
    assert "ner-a" not in registry.deployments
    assert registry.deployments["llm-external"].kind == "llm"
    assert manager.try_reload_calls == 2


def test_store_load_error_is_wrapped_as_storage_error() -> None:
    """변경 전 Registry 조회 실패를 요청 설정 오류로 가장하지 않습니다."""

    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)
    original_load = store.load

    def fail_load() -> RegistryConfig:
        """저장소 자체의 조회 실패를 재현합니다."""

        raise RuntimeError("내부 Registry 손상")

    store.load = fail_load  # type: ignore[method-assign]
    with pytest.raises(DeploymentStorageError) as error_info:
        _service(store, manager).add_deployment("ner-a", _ner())
    store.load = original_load  # type: ignore[method-assign]

    error = error_info.value
    assert error.code == "DEPLOYMENT_STORAGE_FAILED"
    assert isinstance(error.storage_error, RuntimeError)
    assert "내부 Registry 손상" not in str(error)
    assert manager.try_reload_calls == 0


def test_real_manager_rejection_restores_file_and_active_snapshot(
    tmp_path: Path,
) -> None:
    """Provider가 없는 실제 후보를 거부하고 파일과 활성 상태를 함께 복원합니다."""

    config_dir = tmp_path / "config"
    coordinator = RegistryFileCoordinator()
    validator = RegistryValidator()
    store = RegistryFileStore(
        config_dir,
        validator=validator,
        coordinator=coordinator,
    )
    store.initialize()
    prompt_path = config_dir / "prompts.j2"
    title_prompt_path = config_dir / "title_prompt.j2"
    prompt_path.write_text(
        "{{ text }}\n{{ existing_detections }}\n",
        encoding="utf-8",
    )
    title_prompt_path.write_text(
        "제목만 생성하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / "mask_prompt.j2").write_text(
        "탐지 구간을 마스킹하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / POLICY_PROMPTS_FILENAME).write_bytes(
        DEFAULT_POLICY_PROMPTS_PATH.read_bytes()
    )
    renderer = PromptRenderer()
    builder = RegistrySnapshotBuilder(
        store,
        loader=PromptLoader(
            prompt_path,
            limits=renderer.limits,
        ),
        title_loader=PromptLoader(
            title_prompt_path,
            limits=renderer.limits,
        ),
        renderer=renderer,
        registry_validator=validator,
        coordinator=coordinator,
        backend_providers=BackendProviderRegistry(),
    )
    manager = RegistryManager(builder)
    initial_state = manager.initialize()
    service = DeploymentManagementService(
        store=store,
        registry_manager=manager,
    )
    llm_file_before = store.llm_deployments_path.read_bytes()

    with pytest.raises(DeploymentActivationError):
        service.add_deployment("ner-a", _ner())

    restored_state = manager.state
    assert store.load().deployments == {}
    assert restored_state.snapshot is initial_state.snapshot
    assert restored_state.generation == initial_state.generation
    assert restored_state.last_error is None
    assert restored_state.last_failure_at is not None
    assert store.llm_deployments_path.read_bytes() == llm_file_before


def test_restore_save_failure_raises_rollback_error() -> None:
    """이전 파일 저장이 실패하면 정상 복구로 가장하지 않습니다."""

    activation_error = RuntimeError("활성화 실패")
    store = InMemoryRegistryStore(fail_on_update_call=2)
    manager = ScriptedReloadManager(
        (ReloadStatus.REJECTED, activation_error)
    )

    with pytest.raises(DeploymentRollbackError) as error_info:
        _service(store, manager).add_deployment("ner-a", _ner())

    error = error_info.value
    assert error.code == "DEPLOYMENT_ROLLBACK_FAILED"
    assert error.activation_error is activation_error
    assert isinstance(error.rollback_error, OSError)
    assert "복원 저장 실패" not in str(error)
    assert "ner-a" in store.load().deployments
    assert manager.try_reload_calls == 1


def test_restore_reload_rejection_raises_rollback_error() -> None:
    """파일 복원 뒤 Snapshot 재검증 실패도 별도 복구 오류로 구분합니다."""

    activation_error = RuntimeError("활성화 실패")
    rollback_error = RuntimeError("복원 Snapshot 실패")
    store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(
        (ReloadStatus.REJECTED, activation_error),
        (ReloadStatus.REJECTED, rollback_error),
    )

    with pytest.raises(DeploymentRollbackError) as error_info:
        _service(store, manager).add_deployment("ner-a", _ner())

    error = error_info.value
    assert error.activation_error is activation_error
    assert error.rollback_error is rollback_error
    assert store.load().deployments == {}
    assert manager.try_reload_calls == 2


def test_management_operations_are_serialized() -> None:
    """첫 활성화가 끝나기 전에는 다음 변경이 Mutator에 진입하지 않습니다."""

    store = InMemoryRegistryStore()
    manager = BlockingReloadManager()
    second_mutation_entered = Event()
    second_call_requested = Event()
    mutator = TrackingRegistryMutator(
        store.validator,
        second_mutation_entered,
    )
    service = _service(store, manager, mutator=mutator)
    errors: list[Exception] = []

    def add(deployment_id: str, requested: Event | None = None) -> None:
        """Thread에서 한 Deployment를 추가하고 예외를 수집합니다."""

        if requested is not None:
            requested.set()
        try:
            service.add_deployment(deployment_id, _ner())
        except Exception as error:
            errors.append(error)

    first_thread = Thread(target=add, args=("ner-a",))
    second_thread = Thread(
        target=add,
        args=("ner-b", second_call_requested),
    )
    first_thread.start()
    assert manager.first_reload_started.wait(timeout=2)
    second_thread.start()
    assert second_call_requested.wait(timeout=2)
    assert not second_mutation_entered.wait(timeout=0.1)

    manager.allow_first_reload_to_finish.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert set(store.load().deployments) == {"ner-a", "ner-b"}
    assert manager.try_reload_calls == 2


def test_service_rejects_mutator_with_different_validator() -> None:
    """Service와 Mutator에 서로 다른 Registry 정책을 조립하지 못하게 합니다."""

    service_store = InMemoryRegistryStore()
    manager = ScriptedReloadManager(ReloadStatus.APPLIED)

    with pytest.raises(ValueError, match="같은 RegistryValidator"):
        _service(
            service_store,
            manager,
            mutator=RegistryMutator(
                validator=RegistryValidator()
            ),
        )
