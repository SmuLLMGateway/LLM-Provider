"""Registry 파일과 역할별 고정 Prompt를 Active Snapshot으로 조립합니다."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from pydantic import BaseModel

from app.backends.provider_registry import BackendProviderRegistry
from app.core.registry_file_coordinator import (
    DEFAULT_REGISTRY_FILE_COORDINATOR,
    RegistryFileCoordinator,
)
from app.core.json_codec import dump_canonical_json_utf8
from app.registry.snapshot import ActiveRegistrySnapshot
from app.registry.validator import RegistryValidator
from app.registry.store import RegistryReader
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.policy_prompt_catalog import PolicyPromptCatalog
from app.prompts.policy_prompt_loader import (
    POLICY_PROMPTS_FILENAME,
    PolicyPromptLoader,
)
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_loader import (
    MASK_PROMPT_FILENAME,
    TITLE_PROMPT_FILENAME,
    PromptLoader,
)
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import TitlePromptArtifact


class RegistrySnapshotBuilder:
    """Registry와 시작 시 고정한 Prompt들을 실행 준비 후보로 만듭니다."""

    def __init__(
        self,
        reader: RegistryReader,
        loader: PromptLoader | None = None,
        mask_loader: PromptLoader | None = None,
        title_loader: PromptLoader | None = None,
        policy_loader: PolicyPromptLoader | None = None,
        renderer: PromptRenderer | None = None,
        registry_validator: RegistryValidator | None = None,
        coordinator: RegistryFileCoordinator | None = None,
        backend_providers: BackendProviderRegistry | None = None,
    ) -> None:
        self.reader = reader
        self.renderer = (
            renderer if renderer is not None else PromptRenderer()
        )
        self.loader = (
            loader
            if loader is not None
            else PromptLoader(limits=self.renderer.limits)
        )
        self.title_loader = (
            title_loader
            if title_loader is not None
            else PromptLoader(
                self.loader.prompt_path.parent
                / TITLE_PROMPT_FILENAME,
                limits=self.renderer.limits,
            )
        )
        self.mask_loader = (
            mask_loader
            if mask_loader is not None
            else PromptLoader(
                self.loader.prompt_path.parent / MASK_PROMPT_FILENAME,
                limits=self.renderer.limits,
            )
        )
        self.policy_loader = (
            policy_loader
            if policy_loader is not None
            else PolicyPromptLoader(
                self.loader.prompt_path.parent / POLICY_PROMPTS_FILENAME,
                limits=self.renderer.limits,
            )
        )
        self._fixed_detection_prompt: PromptArtifact | None = None
        self._fixed_mask_prompt: MaskPromptArtifact | None = None
        self._fixed_title_prompt: TitlePromptArtifact | None = None
        if (
            self.loader.limits != self.renderer.limits
            or self.mask_loader.limits != self.renderer.limits
            or self.title_loader.limits != self.renderer.limits
            or self.policy_loader.limits != self.renderer.limits
        ):
            raise ValueError(
                "모든 PromptLoader와 PromptRenderer는 같은 "
                "PromptLimits를 사용해야 합니다"
            )
        reader_validator = getattr(reader, "validator", None)
        if (
            registry_validator is not None
            and reader_validator is not None
            and registry_validator is not reader_validator
        ):
            raise ValueError(
                "RegistryReader와 RegistrySnapshotBuilder는 같은 "
                "RegistryValidator를 사용해야 합니다"
            )
        self.registry_validator = (
            registry_validator
            if registry_validator is not None
            else reader_validator or RegistryValidator()
        )
        self.backend_providers = backend_providers
        if self.backend_providers is not None:
            self.backend_providers.validate_contracts(
                self.registry_validator.backend_registry
            )
        reader_coordinator = getattr(reader, "coordinator", None)
        if (
            coordinator is not None
            and reader_coordinator is not None
            and coordinator is not reader_coordinator
        ):
            raise ValueError(
                "RegistryReader와 RegistrySnapshotBuilder는 같은 "
                "RegistryFileCoordinator를 사용해야 합니다"
            )
        self.coordinator = (
            coordinator
            if coordinator is not None
            else reader_coordinator or DEFAULT_REGISTRY_FILE_COORDINATOR
        )

    def build(self) -> ActiveRegistrySnapshot:
        """Registry와 프로세스 고정 Prompt들을 검증해 후보를 조립합니다."""

        with self.coordinator.transaction():
            return self._build_candidate()

    def _build_candidate(self) -> ActiveRegistrySnapshot:
        """공통 파일 Lock 안에서 전체 후보 실행 구성을 조립합니다."""

        registry = self.reader.load()
        self.registry_validator.validate_registry(registry)
        if self.backend_providers is not None:
            self.backend_providers.validate_registry(registry)

        fixed_detection_prompt = self._fixed_detection_prompt
        detection_prompt = (
            fixed_detection_prompt
            if fixed_detection_prompt is not None
            else self._build_detection_prompt_artifact()
        )
        fixed_mask_prompt = self._fixed_mask_prompt
        mask_prompt = (
            fixed_mask_prompt
            if fixed_mask_prompt is not None
            else self._build_mask_prompt_artifact()
        )
        fixed_title_prompt = self._fixed_title_prompt
        title_prompt = (
            fixed_title_prompt
            if fixed_title_prompt is not None
            else self._build_title_prompt_artifact()
        )

        snapshot_id = self._build_snapshot_id(
            registry.deployments,
            detection_prompt=detection_prompt,
            mask_prompt=mask_prompt,
            title_prompt=title_prompt,
        )
        snapshot = ActiveRegistrySnapshot(
            snapshot_id=snapshot_id,
            deployments=registry.deployments,
            detection_prompt=detection_prompt,
            mask_prompt=mask_prompt,
            title_prompt=title_prompt,
        )
        if fixed_detection_prompt is None:
            self._fixed_detection_prompt = detection_prompt
        if fixed_title_prompt is None:
            self._fixed_title_prompt = title_prompt
        if fixed_mask_prompt is None:
            self._fixed_mask_prompt = mask_prompt
        return snapshot

    def _build_detection_prompt_artifact(self) -> PromptArtifact:
        """탐지 Prompt를 최초 정상 후보에서 한 번만 검증·컴파일합니다."""

        source = self.loader.load()
        policy_prompts = PolicyPromptCatalog.compile(
            self.policy_loader.load(),
            source_path=str(self.policy_loader.prompt_path),
            max_output_bytes=self.renderer.limits.max_output_bytes,
        )
        return PromptArtifact.compile(
            source,
            renderer=self.renderer,
            template_path=str(self.loader.prompt_path),
            policy_prompts=policy_prompts,
        )

    def _build_title_prompt_artifact(self) -> TitlePromptArtifact:
        """제목 생성 Prompt를 최초 정상 후보에서 한 번만 컴파일합니다."""

        source = self.title_loader.load()
        return TitlePromptArtifact.compile(
            source,
            renderer=self.renderer,
            template_path=str(self.title_loader.prompt_path),
        )

    def _build_mask_prompt_artifact(self) -> MaskPromptArtifact:
        """마스킹 Prompt를 최초 정상 후보에서 한 번만 컴파일합니다."""

        source = self.mask_loader.load()
        return MaskPromptArtifact.compile(
            source,
            renderer=self.renderer,
            template_path=str(self.mask_loader.prompt_path),
        )

    @classmethod
    def _build_snapshot_id(
        cls,
        deployments: Mapping[str, BaseModel],
        *,
        detection_prompt: PromptArtifact,
        mask_prompt: MaskPromptArtifact,
        title_prompt: TitlePromptArtifact,
    ) -> str:
        """Deployment 설정과 역할별 Prompt 해시로 Snapshot ID를 만듭니다."""

        payload = {
            "deployments": cls._serialize_model_map(deployments),
            "prompts": {
                "detection": cls._serialize_artifact(detection_prompt),
                "masking": cls._serialize_artifact(mask_prompt),
                "title": cls._serialize_artifact(title_prompt),
            },
        }
        canonical_payload = dump_canonical_json_utf8(payload)
        return hashlib.sha256(canonical_payload).hexdigest()

    @staticmethod
    def _serialize_artifact(
        artifact: PromptArtifact | MaskPromptArtifact | TitlePromptArtifact,
    ) -> dict[str, object]:
        """역할별 Prompt Artifact를 Snapshot 해시 입력으로 직렬화합니다."""

        serialized = {
            "contentHash": artifact.content_hash,
        }
        if isinstance(artifact, PromptArtifact):
            serialized["policyContentHash"] = (
                artifact.policy_prompts.content_hash
            )
        return serialized

    @staticmethod
    def _serialize_model_map(
        resources: Mapping[str, BaseModel],
    ) -> dict[str, object]:
        """리소스 맵을 Snapshot 해시에 사용할 JSON 객체로 변환합니다."""

        return {
            resource_id: resource.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
            for resource_id, resource in sorted(resources.items())
        }


__all__ = ["RegistrySnapshotBuilder"]
