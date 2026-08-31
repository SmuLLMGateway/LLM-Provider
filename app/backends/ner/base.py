"""NER Backend 구현체가 따라야 하는 공통 인터페이스를 정의합니다."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.detection import Detection
from app.schemas.registry import DeploymentConfig


@runtime_checkable
class NerBackend(Protocol):
    """전용 NER 또는 NER 서버 결과를 공통 Detection으로 반환합니다."""

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """원문에서 개체를 탐지해 정규화된 Detection 목록을 반환합니다."""

        ...


__all__ = ["NerBackend"]
