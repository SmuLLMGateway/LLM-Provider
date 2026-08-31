"""Registry JSON 작업과 고정 Prompt의 최초 읽기를 조율합니다."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock


class RegistryFileCoordinator:
    """Registry 실행 구성의 파일 작업을 프로세스 안에서 직렬화합니다."""

    def __init__(self) -> None:
        self._lock = RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """같은 프로세스의 Registry 파일 작업을 재진입 가능하게 잠급니다."""

        with self._lock:
            yield


DEFAULT_REGISTRY_FILE_COORDINATOR = RegistryFileCoordinator()


__all__ = [
    "DEFAULT_REGISTRY_FILE_COORDINATOR",
    "RegistryFileCoordinator",
]
