"""요청 단위 Registry Snapshot 조회 계약을 정의합니다."""

from __future__ import annotations

from typing import Protocol

from app.registry.snapshot import ActiveRegistrySnapshot


class RegistrySnapshotProvider(Protocol):
    """요청에서 사용할 현재 Active Snapshot을 제공하는 계약입니다."""

    def capture(self) -> ActiveRegistrySnapshot:
        """현재 Active Snapshot 참조를 반환합니다."""

        ...


__all__ = ["RegistrySnapshotProvider"]
