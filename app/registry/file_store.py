"""NER·LLM Deployment JSON을 분리해서 관리하는 Registry 저장소입니다."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from app.core.registry_file_coordinator import (
    DEFAULT_REGISTRY_FILE_COORDINATOR,
    RegistryFileCoordinator,
)
from app.core.json_codec import dump_json_utf8, load_strict_json
from app.registry.validator import RegistryValidator
from app.registry.store import RegistryUpdater
from app.schemas.registry import (
    DeploymentConfig,
    DeploymentKind,
    LlmDeploymentRegistryFile,
    NerDeploymentRegistryFile,
    RegistryConfig,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
NER_DEPLOYMENTS_FILENAME = "ner_deployments.json"
LLM_DEPLOYMENTS_FILENAME = "llm_deployments.json"
LEGACY_DEPLOYMENTS_FILENAME = "deployments.json"


class LegacyDeploymentRegistryFileError(RuntimeError):
    """이전 단일 Deployment 파일이 남아 있으면 발생합니다."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            "이전 deployments.json을 종류별 Registry 파일로 "
            "마이그레이션해야 합니다."
        )


class DuplicateDeploymentIdAcrossFilesError(ValueError):
    """NER와 LLM 파일에 같은 Deployment ID가 함께 있으면 발생합니다."""

    def __init__(self, deployment_ids: set[str]) -> None:
        self.deployment_ids = tuple(sorted(deployment_ids))
        super().__init__(
            "NER와 LLM Deployment ID는 전체 Registry에서 고유해야 합니다: "
            f"{', '.join(self.deployment_ids)}"
        )


class RegistryMultiKindUpdateError(RuntimeError):
    """한 Store 갱신에서 NER와 LLM 파일을 동시에 바꾸면 발생합니다."""

    def __init__(self) -> None:
        super().__init__(
            "한 Registry 갱신에서는 NER 또는 LLM 한 종류만 변경할 수 있습니다."
        )


class RegistryFileStore:
    """종류별 JSON 두 개를 하나의 RegistryConfig로 조립하고 저장합니다."""

    def __init__(
        self,
        config_dir: str | Path = DEFAULT_CONFIG_DIR,
        validator: RegistryValidator | None = None,
        coordinator: RegistryFileCoordinator | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.validator = validator or RegistryValidator()
        self.coordinator = (
            coordinator
            if coordinator is not None
            else DEFAULT_REGISTRY_FILE_COORDINATOR
        )
        self.ner_deployments_path = (
            self.config_dir / NER_DEPLOYMENTS_FILENAME
        )
        self.llm_deployments_path = (
            self.config_dir / LLM_DEPLOYMENTS_FILENAME
        )
        self.legacy_deployments_path = (
            self.config_dir / LEGACY_DEPLOYMENTS_FILENAME
        )

    def initialize(self) -> Path:
        """기존 파일을 덮어쓰지 않고 종류별 빈 Registry 파일을 생성합니다."""

        empty_files = (
            (
                self.ner_deployments_path,
                NerDeploymentRegistryFile.model_validate({}),
            ),
            (
                self.llm_deployments_path,
                LlmDeploymentRegistryFile.model_validate({}),
            ),
        )

        with self.coordinator.transaction():
            self._reject_legacy_file()
            for path, _registry_file in empty_files:
                if path.exists():
                    raise FileExistsError(path)

            self.config_dir.mkdir(parents=True, exist_ok=True)
            created_paths: list[Path] = []
            try:
                for path, registry_file in empty_files:
                    with path.open(
                        "x",
                        encoding="utf-8",
                        newline="\n",
                    ) as output_file:
                        created_paths.append(path)
                        output_file.write(
                            self._serialize(registry_file)
                        )
            except Exception:
                for path in created_paths:
                    path.unlink(missing_ok=True)
                raise

        return self.config_dir

    def load(self) -> RegistryConfig:
        """종류별 JSON 두 개를 읽어 하나의 RegistryConfig로 조립합니다."""

        with self.coordinator.transaction():
            registry = self._load_structural_registry()
            self.validator.validate_registry(registry)
        return registry

    def _load_structural_registry(self) -> RegistryConfig:
        """두 파일의 JSON 구조를 검증하고 kind를 주입해 ID를 병합합니다."""

        self._reject_legacy_file()
        ner_deployments = NerDeploymentRegistryFile.model_validate(
            load_strict_json(self.ner_deployments_path.read_bytes()),
            by_alias=True,
            by_name=False,
        )
        llm_deployments = LlmDeploymentRegistryFile.model_validate(
            load_strict_json(self.llm_deployments_path.read_bytes()),
            by_alias=True,
            by_name=False,
        )
        duplicate_ids = (
            set(ner_deployments.root)
            & set(llm_deployments.root)
        )
        if duplicate_ids:
            raise DuplicateDeploymentIdAcrossFilesError(duplicate_ids)

        return RegistryConfig(
            deployments={
                **ner_deployments.to_runtime_map(),
                **llm_deployments.to_runtime_map(),
            }
        )

    def _reject_legacy_file(self) -> None:
        """이전 단일 파일을 조용히 무시하지 않고 마이그레이션을 요구합니다."""

        if self.legacy_deployments_path.exists():
            raise LegacyDeploymentRegistryFileError(
                self.legacy_deployments_path
            )

    def update(self, updater: RegistryUpdater) -> RegistryConfig:
        """한 종류의 Registry 파일만 갱신해 원자적으로 저장합니다."""

        with self.coordinator.transaction():
            current_registry = self._load_structural_registry()
            working_registry = RegistryConfig(
                deployments=dict(current_registry.deployments)
            )
            updated_registry = RegistryConfig.model_validate(
                updater(working_registry)
            )
            self.validator.validate_registry(updated_registry)
            current_by_kind = self._split_by_kind(current_registry)
            updated_by_kind = self._split_by_kind(updated_registry)
            changed_kinds = [
                kind
                for kind in ("ner", "llm")
                if current_by_kind[kind] != updated_by_kind[kind]
            ]
            if len(changed_kinds) > 1:
                raise RegistryMultiKindUpdateError()
            if changed_kinds:
                changed_kind = changed_kinds[0]
                self._replace_kind_atomically(
                    changed_kind,
                    updated_by_kind[changed_kind],
                )

        return updated_registry

    @staticmethod
    def _split_by_kind(
        registry: RegistryConfig,
    ) -> dict[DeploymentKind, dict[str, DeploymentConfig]]:
        """조립된 Registry를 파일 저장 단위인 NER와 LLM 맵으로 분리합니다."""

        return {
            "ner": {
                deployment_id: deployment
                for deployment_id, deployment
                in registry.deployments.items()
                if deployment.kind == "ner"
            },
            "llm": {
                deployment_id: deployment
                for deployment_id, deployment
                in registry.deployments.items()
                if deployment.kind == "llm"
            },
        }

    def _replace_kind_atomically(
        self,
        kind: DeploymentKind,
        deployments: dict[str, DeploymentConfig],
    ) -> None:
        """선택한 종류의 파일 모델과 경로를 사용해 원자 교체합니다."""

        if kind == "ner":
            path = self.ner_deployments_path
            registry_file = NerDeploymentRegistryFile.from_runtime_map(
                deployments
            )
        else:
            path = self.llm_deployments_path
            registry_file = LlmDeploymentRegistryFile.from_runtime_map(
                deployments
            )
        self._replace_atomically(path, registry_file)

    @staticmethod
    def _serialize(registry_file: BaseModel) -> str:
        """리소스 파일 모델을 JSON 문자열로 변환합니다."""

        data = registry_file.model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        )
        return f"{dump_json_utf8(data, indent=2).decode('utf-8')}\n"

    def _replace_atomically(
        self,
        path: Path,
        registry_file: BaseModel,
    ) -> None:
        """임시 파일에 저장한 리소스 맵을 기존 파일과 교체합니다."""

        temporary_path = path.with_name(
            f".{path.name}.{uuid4().hex}.tmp"
        )

        try:
            with temporary_path.open(
                "x",
                encoding="utf-8",
                newline="\n",
            ) as temporary_file:
                temporary_file.write(self._serialize(registry_file))

            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def create_registry_files(
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
) -> Path:
    """기존 함수 호출 방식을 지원하는 Registry 초기화 함수입니다."""

    return RegistryFileStore(config_dir).initialize()


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "DuplicateDeploymentIdAcrossFilesError",
    "LEGACY_DEPLOYMENTS_FILENAME",
    "LLM_DEPLOYMENTS_FILENAME",
    "LegacyDeploymentRegistryFileError",
    "NER_DEPLOYMENTS_FILENAME",
    "RegistryFileStore",
    "RegistryMultiKindUpdateError",
    "create_registry_files",
]
