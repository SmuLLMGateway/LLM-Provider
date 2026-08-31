"""Registry 저장소의 공통 인터페이스를 정의합니다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.schemas.registry import RegistryConfig


RegistryUpdater = Callable[[RegistryConfig], RegistryConfig]


class RegistryReader(Protocol):
    """RegistryConfig 조회 기능을 추상화합니다."""

    def load(self) -> RegistryConfig:
        """현재 RegistryConfig를 불러옵니다."""

        ...


class RegistryStore(RegistryReader, Protocol):
    """RegistryConfig의 조회와 저장 방식을 추상화합니다."""

    def update(self, updater: RegistryUpdater) -> RegistryConfig:
        """읽기·수정·저장을 하나의 쓰기 작업으로 수행합니다."""

        ...


__all__ = ["RegistryReader", "RegistryStore", "RegistryUpdater"]
