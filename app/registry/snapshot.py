"""검증과 컴파일을 마친 Registry와 Prompt 실행 Snapshot 모델입니다."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.schemas.registry import DeploymentConfig


@dataclass(frozen=True, slots=True)
class ActiveRegistrySnapshot:
    """한 요청이 끝날 때까지 함께 사용할 불변 실행 구성을 보관합니다."""

    snapshot_id: str
    deployments: Mapping[str, DeploymentConfig]
    detection_prompt: PromptArtifact
    mask_prompt: MaskPromptArtifact
    title_prompt: TitlePromptArtifact

    def __post_init__(self) -> None:
        """리소스 맵을 방어 복사한 읽기 전용 Mapping으로 교체합니다."""

        object.__setattr__(
            self,
            "deployments",
            MappingProxyType(dict(self.deployments)),
        )
        if not isinstance(self.detection_prompt, PromptArtifact):
            raise TypeError("detection_prompt는 PromptArtifact여야 합니다")
        if not isinstance(self.mask_prompt, MaskPromptArtifact):
            raise TypeError("mask_prompt는 MaskPromptArtifact여야 합니다")
        if not isinstance(self.title_prompt, TitlePromptArtifact):
            raise TypeError(
                "title_prompt는 TitlePromptArtifact여야 합니다"
            )


__all__ = ["ActiveRegistrySnapshot"]
