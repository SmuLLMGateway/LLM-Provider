"""Registry 파일 Coordinator의 공통 잠금 계약을 검증합니다."""

from pathlib import Path
from threading import Event, Thread

import pytest

from app.core.registry_file_coordinator import (
    DEFAULT_REGISTRY_FILE_COORDINATOR,
    RegistryFileCoordinator,
)
from app.registry.file_store import RegistryFileStore
from app.registry.offline_editor import OfflineRegistryEditor
from app.registry.snapshot_builder import RegistrySnapshotBuilder
from app.registry.validator import RegistryValidator


def test_transaction_lock_is_reentrant() -> None:
    """하나의 관리 작업이 Coordinator Transaction을 중첩할 수 있는지 확인합니다."""

    coordinator = RegistryFileCoordinator()

    with coordinator.transaction():
        with coordinator.transaction():
            pass


def test_transaction_serializes_different_threads() -> None:
    """다른 Thread의 Registry 파일 작업이 동시에 진입하지 못하는지 확인합니다."""

    coordinator = RegistryFileCoordinator()
    worker_attempted = Event()
    worker_entered = Event()

    def enter_transaction() -> None:
        """Coordinator 진입을 시도하고 성공 시 신호를 남깁니다."""

        worker_attempted.set()
        with coordinator.transaction():
            worker_entered.set()

    worker = Thread(target=enter_transaction)
    with coordinator.transaction():
        worker.start()
        assert worker_attempted.wait(timeout=2)
        assert not worker_entered.is_set()

    assert worker_entered.wait(timeout=2)
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_default_file_components_share_one_coordinator(
    tmp_path: Path,
) -> None:
    """Store와 Builder가 기본 Coordinator를 공유하는지 확인합니다."""

    store = RegistryFileStore(tmp_path / "config")
    offline_editor = OfflineRegistryEditor(store)
    builder = RegistrySnapshotBuilder(store)

    assert store.coordinator is DEFAULT_REGISTRY_FILE_COORDINATOR
    assert builder.coordinator is DEFAULT_REGISTRY_FILE_COORDINATOR
    assert builder.registry_validator is store.validator
    assert offline_editor.mutator.validator is store.validator


def test_builder_rejects_reader_coordinator_mismatch(
    tmp_path: Path,
) -> None:
    """Builder와 파일 Reader에 다른 Coordinator를 주입하지 못하게 합니다."""

    reader_coordinator = RegistryFileCoordinator()
    builder_coordinator = RegistryFileCoordinator()
    store = RegistryFileStore(
        tmp_path / "config",
        coordinator=reader_coordinator,
    )

    with pytest.raises(ValueError, match="같은 RegistryFileCoordinator"):
        RegistrySnapshotBuilder(
            store,
            coordinator=builder_coordinator,
        )


def test_builder_rejects_reader_validator_mismatch(
    tmp_path: Path,
) -> None:
    """Reader와 Builder가 서로 다른 RegistryValidator를 사용하지 못하게 합니다."""

    store = RegistryFileStore(tmp_path / "config")

    with pytest.raises(ValueError, match="같은 RegistryValidator"):
        RegistrySnapshotBuilder(
            store,
            registry_validator=RegistryValidator(),
        )
