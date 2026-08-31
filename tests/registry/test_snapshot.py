"""Deployment와 고정 Prompt를 담은 Active Snapshot을 검증합니다."""

from dataclasses import FrozenInstanceError

import pytest

from app.registry.snapshot import ActiveRegistrySnapshot
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.schemas.registry import DeploymentConfig


def _deployment() -> DeploymentConfig:
    """Snapshot 테스트용 NER Deployment를 만듭니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": "ner",
            "adapterType": "mock",
            "enabled": True,
        }
    )


def _prompt() -> PromptArtifact:
    """Snapshot에 보관할 컴파일된 고정 Prompt를 만듭니다."""

    return PromptArtifact.compile(
        "{{ text }} {{ existing_detections }}",
        renderer=PromptRenderer(),
        template_path="test",
    )


def _title_prompt() -> TitlePromptArtifact:
    """Snapshot에 보관할 컴파일된 제목 Prompt를 만듭니다."""

    return TitlePromptArtifact.compile(
        "제목만 생성하십시오.",
        renderer=PromptRenderer(),
        template_path="title-test",
    )


def _mask_prompt() -> MaskPromptArtifact:
    """Snapshot에 보관할 컴파일된 마스킹 Prompt를 만듭니다."""

    return MaskPromptArtifact.compile(
        "탐지 구간을 마스킹하십시오.",
        renderer=PromptRenderer(),
        template_path="mask-test",
    )


def test_snapshot_defensively_copies_deployments() -> None:
    """생성 후 원본 dict 변경이 활성 Snapshot에 반영되지 않습니다."""

    source = {"ner-a": _deployment()}
    snapshot = ActiveRegistrySnapshot(
        snapshot_id="snapshot-a",
        deployments=source,
        detection_prompt=_prompt(),
        mask_prompt=_mask_prompt(),
        title_prompt=_title_prompt(),
    )

    source.clear()

    assert set(snapshot.deployments) == {"ner-a"}


def test_snapshot_deployments_are_read_only() -> None:
    """활성 Snapshot의 Deployment Mapping을 외부에서 수정하지 못합니다."""

    snapshot = ActiveRegistrySnapshot(
        snapshot_id="snapshot-a",
        deployments={"ner-a": _deployment()},
        detection_prompt=_prompt(),
        mask_prompt=_mask_prompt(),
        title_prompt=_title_prompt(),
    )

    with pytest.raises(TypeError):
        snapshot.deployments["other"] = _deployment()  # type: ignore[index]
    with pytest.raises(TypeError):
        del snapshot.deployments["ner-a"]  # type: ignore[misc]


def test_snapshot_keeps_prompt_artifact_identity() -> None:
    """검증·컴파일된 Prompt Artifact를 복제하지 않고 보관합니다."""

    prompt = _prompt()

    snapshot = ActiveRegistrySnapshot(
        snapshot_id="snapshot-a",
        deployments={},
        detection_prompt=prompt,
        mask_prompt=_mask_prompt(),
        title_prompt=_title_prompt(),
    )

    assert snapshot.detection_prompt is prompt


def test_snapshot_requires_prompt_artifact() -> None:
    """불완전한 Snapshot이 활성화되지 않도록 Prompt 타입을 검증합니다."""

    with pytest.raises(TypeError, match="PromptArtifact"):
        ActiveRegistrySnapshot(
            snapshot_id="snapshot-a",
            deployments={},
            detection_prompt=None,  # type: ignore[arg-type]
            mask_prompt=_mask_prompt(),
            title_prompt=_title_prompt(),
        )


def test_snapshot_requires_title_prompt_artifact() -> None:
    """제목 Prompt가 없는 불완전한 Snapshot을 거부합니다."""

    with pytest.raises(TypeError, match="TitlePromptArtifact"):
        ActiveRegistrySnapshot(
            snapshot_id="snapshot-a",
            deployments={},
            detection_prompt=_prompt(),
            mask_prompt=_mask_prompt(),
            title_prompt=None,  # type: ignore[arg-type]
        )


def test_snapshot_is_frozen() -> None:
    """Snapshot 참조 필드를 요청 처리 중 교체하지 못합니다."""

    snapshot = ActiveRegistrySnapshot(
        snapshot_id="snapshot-a",
        deployments={},
        detection_prompt=_prompt(),
        mask_prompt=_mask_prompt(),
        title_prompt=_title_prompt(),
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.snapshot_id = "changed"  # type: ignore[misc]
