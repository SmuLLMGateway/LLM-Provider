"""FastAPI 수명 주기에서 공유할 LPL 실행 객체를 조립합니다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.backends.llm.mock import MockLlmBackend
from app.backends.llm.openai_compatible import (
    OpenAICompatibleLlmBackend,
)
from app.backends.ner.http import HttpNerBackend
from app.backends.provider_registry import (
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.core.registry_file_coordinator import RegistryFileCoordinator
from app.registry.deployment_resolver import DeploymentResolver
from app.registry.file_store import (
    DEFAULT_CONFIG_DIR,
    RegistryFileStore,
)
from app.registry.manager import RegistryManager
from app.registry.snapshot_builder import RegistrySnapshotBuilder
from app.registry.validator import RegistryValidator
from app.prompts.prompt_loader import (
    MASK_PROMPT_FILENAME,
    PROMPT_FILENAME,
    TITLE_PROMPT_FILENAME,
    PromptLoader,
)
from app.prompts.prompt_renderer import PromptRenderer
from app.services.adapter_catalog import AdapterCatalogService
from app.services.detection_pipeline import DetectionPipeline
from app.services.deployment_management import (
    DeploymentManagementService,
)
from app.services.generation_pipeline import GenerationPipeline
from app.services.masking_pipeline import MaskingPipeline
from app.services.llm_limits import LlmLimitsService
from app.services.policy_settings import (
    PolicySettingsFileStore,
    PolicySettingsManager,
)
from app.services.title_generation_pipeline import (
    TitleGenerationPipeline,
)


@dataclass(frozen=True, slots=True)
class ApplicationRuntime:
    """애플리케이션 전체 요청이 공유하는 초기화 완료 실행 객체입니다."""

    http_client: httpx.AsyncClient
    registry_manager: RegistryManager
    backend_providers: BackendProviderRegistry
    adapter_catalog_service: AdapterCatalogService
    detection_pipeline: DetectionPipeline
    generation_pipeline: GenerationPipeline
    masking_pipeline: MaskingPipeline
    title_generation_pipeline: TitleGenerationPipeline
    deployment_management_service: DeploymentManagementService
    policy_settings_manager: PolicySettingsManager | None = None
    llm_limits_service: LlmLimitsService | None = None


@asynccontextmanager
async def application_runtime(
    *,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
) -> AsyncIterator[ApplicationRuntime]:
    """Registry와 Backend를 조립하고 HTTP Client의 수명을 관리합니다."""

    coordinator = RegistryFileCoordinator()
    registry_validator = RegistryValidator()
    renderer = PromptRenderer()

    async with httpx.AsyncClient(trust_env=False) as http_client:
        backend_providers = BackendProviderRegistry(
            [
                BackendProviderRegistration(
                    kind="ner",
                    adapter_type="http_ner",
                    provider=HttpNerBackend(http_client),
                ),
                BackendProviderRegistration(
                    kind="llm",
                    adapter_type="mock",
                    provider=MockLlmBackend(),
                ),
                BackendProviderRegistration(
                    kind="llm",
                    adapter_type="openai_compatible",
                    provider=OpenAICompatibleLlmBackend(http_client),
                ),
            ]
        )
        store = RegistryFileStore(
            config_dir,
            validator=registry_validator,
            coordinator=coordinator,
        )
        snapshot_builder = RegistrySnapshotBuilder(
            store,
            loader=PromptLoader(
                Path(config_dir) / PROMPT_FILENAME,
                limits=renderer.limits,
            ),
            mask_loader=PromptLoader(
                Path(config_dir) / MASK_PROMPT_FILENAME,
                limits=renderer.limits,
            ),
            title_loader=PromptLoader(
                Path(config_dir) / TITLE_PROMPT_FILENAME,
                limits=renderer.limits,
            ),
            renderer=renderer,
            registry_validator=registry_validator,
            coordinator=coordinator,
            backend_providers=backend_providers,
        )
        registry_manager = RegistryManager(snapshot_builder)
        registry_manager.initialize()
        policy_settings_manager = PolicySettingsManager(
            PolicySettingsFileStore(config_dir)
        )
        policy_settings_manager.initialize()
        deployment_management_service = DeploymentManagementService(
            store=store,
            registry_manager=registry_manager,
        )
        llm_limits_service = LlmLimitsService(
            registry_manager=registry_manager,
            http_client=http_client,
        )
        deployment_resolver = DeploymentResolver()

        yield ApplicationRuntime(
            http_client=http_client,
            registry_manager=registry_manager,
            backend_providers=backend_providers,
            adapter_catalog_service=AdapterCatalogService(
                registry_validator.backend_registry,
                backend_providers,
            ),
            detection_pipeline=DetectionPipeline(
                registry_manager=registry_manager,
                deployment_resolver=deployment_resolver,
                backend_providers=backend_providers,
                policy_settings_provider=policy_settings_manager,
            ),
            generation_pipeline=GenerationPipeline(
                registry_manager=registry_manager,
                deployment_resolver=deployment_resolver,
                backend_providers=backend_providers,
            ),
            masking_pipeline=MaskingPipeline(
                registry_manager=registry_manager,
                deployment_resolver=deployment_resolver,
                backend_providers=backend_providers,
            ),
            title_generation_pipeline=TitleGenerationPipeline(
                registry_manager=registry_manager,
                deployment_resolver=deployment_resolver,
                backend_providers=backend_providers,
            ),
            deployment_management_service=deployment_management_service,
            policy_settings_manager=policy_settings_manager,
            llm_limits_service=llm_limits_service,
        )


__all__ = [
    "ApplicationRuntime",
    "application_runtime",
]
