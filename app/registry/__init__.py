"""Deployment Registry의 저장, 조회, 변경과 활성화를 담당합니다."""

from app.registry.deployment_resolver import (
    DeploymentResolutionError,
    DeploymentResolutionErrorCode,
    DeploymentResolver,
)
from app.registry.execution_plan import (
    DetectionExecutionPlan,
    GenerationExecutionPlan,
    MaskingExecutionPlan,
    ResolvedDeployment,
    TitleGenerationExecutionPlan,
)
from app.registry.offline_editor import (
    OfflineRegistryEditor,
    add_deployment_offline,
    update_deployment_offline,
)
from app.registry.file_store import (
    DEFAULT_CONFIG_DIR,
    DuplicateDeploymentIdAcrossFilesError,
    LEGACY_DEPLOYMENTS_FILENAME,
    LLM_DEPLOYMENTS_FILENAME,
    LegacyDeploymentRegistryFileError,
    NER_DEPLOYMENTS_FILENAME,
    RegistryFileStore,
    RegistryMultiKindUpdateError,
    create_registry_files,
)
from app.registry.manager import (
    RegistryManager,
    RegistryManagerNotInitializedError,
    RegistryManagerState,
    ReloadResult,
    ReloadStatus,
)
from app.registry.mutator import (
    DeploymentAlreadyExistsError,
    DeploymentMustBeDisabledError,
    DeploymentNotFoundError,
    RegistryMutator,
)
from app.registry.validator import RegistryValidator
from app.registry.snapshot import ActiveRegistrySnapshot
from app.registry.snapshot_builder import RegistrySnapshotBuilder
from app.registry.store import RegistryReader, RegistryStore

__all__ = [
    "ActiveRegistrySnapshot",
    "DEFAULT_CONFIG_DIR",
    "DuplicateDeploymentIdAcrossFilesError",
    "DeploymentResolutionError",
    "DeploymentResolutionErrorCode",
    "DeploymentResolver",
    "DeploymentAlreadyExistsError",
    "DeploymentMustBeDisabledError",
    "DeploymentNotFoundError",
    "DetectionExecutionPlan",
    "GenerationExecutionPlan",
    "MaskingExecutionPlan",
    "LEGACY_DEPLOYMENTS_FILENAME",
    "LLM_DEPLOYMENTS_FILENAME",
    "LegacyDeploymentRegistryFileError",
    "NER_DEPLOYMENTS_FILENAME",
    "OfflineRegistryEditor",
    "RegistryFileStore",
    "RegistryManager",
    "RegistryManagerNotInitializedError",
    "RegistryManagerState",
    "RegistryMultiKindUpdateError",
    "RegistryMutator",
    "RegistryReader",
    "RegistryStore",
    "RegistrySnapshotBuilder",
    "RegistryValidator",
    "ResolvedDeployment",
    "TitleGenerationExecutionPlan",
    "ReloadResult",
    "ReloadStatus",
    "add_deployment_offline",
    "create_registry_files",
    "update_deployment_offline",
]
