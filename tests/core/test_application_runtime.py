"""애플리케이션 런타임의 실제 조립과 자원 수명 주기를 검증합니다."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.backends.llm.mock import MockLlmBackend
from app.backends.llm.openai_compatible import (
    OpenAICompatibleLlmBackend,
)
from app.backends.ner.http import HttpNerBackend
from app.core.application_runtime import application_runtime
from app.prompts.policy_prompt_loader import (
    DEFAULT_POLICY_PROMPTS_PATH,
    POLICY_PROMPTS_FILENAME,
)
from app.schemas.detection import (
    ALLOWED_POLICY_IDS,
    DetectResponse,
    Detection,
)
from app.schemas.masking import MaskResponse
from app.schemas.detection_context import (
    OrganizationProfile,
)
from app.schemas.registry import DeploymentConfig


def _write_json(path: Path, data: object) -> None:
    """테스트 Registry 데이터를 UTF-8 JSON 파일로 저장합니다."""

    path.write_text(
        f"{json.dumps(data, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )


def _organization_profile() -> OrganizationProfile:
    """전체 기본 정책을 충족하는 Runtime 테스트 조직 프로필입니다."""

    return OrganizationProfile.model_validate(
        {
            "organization": {
                "name": "ABC 주식회사",
                "aliases": [],
                "type": "PRIVATE",
            },
            "publicContext": {"domains": ["abc.com"], "entities": []},
            "privacy": {"personNameScope": True},
            "securityContext": {
                "internalIpRanges": [],
                "internalDomains": [],
                "internalSystems": [],
                "cloudAssets": [],
                "securityAssets": [],
                "protectTestLogs": True,
            },
            "confidentialTechnologyContext": {"assets": []},
            "thirdPartyContext": {"entities": []},
        }
    )


def _write_runtime_files(tmp_path: Path) -> tuple[Path, Path]:
    """실제 런타임 조립에 필요한 Deployment Registry와 Prompt를 만듭니다."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    _write_json(
        config_dir / "ner_deployments.json",
        {
            "ner-http-a": {
                "baseUrl": "http://127.0.0.1:9001/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        },
    )
    _write_json(
        config_dir / "llm_deployments.json",
        {
            "llm-local-a": {
                "adapterType": "openai_compatible",
                "baseUrl": "http://127.0.0.1:9000/v1",
                "modelName": "local-model-a",
                "timeoutMs": 5000,
                "enabled": True,
            },
        },
    )
    prompt_path = config_dir / "prompts.j2"
    prompt_path.write_text(
        "원문: {{ text }}\n기존 탐지: {{ existing_detections }}\n",
        encoding="utf-8",
    )
    (config_dir / "title_prompt.j2").write_text(
        "대화 제목을 한 줄로 생성하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / "mask_prompt.j2").write_text(
        "탐지 구간을 마스킹하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / POLICY_PROMPTS_FILENAME).write_bytes(
        DEFAULT_POLICY_PROMPTS_PATH.read_bytes()
    )
    return config_dir, prompt_path


def _write_mock_detection_runtime_files(
    tmp_path: Path,
) -> tuple[Path, Path]:
    """표준 NER와 Mock LLM으로 Detection Pipeline 구성을 만듭니다."""

    config_dir, prompt_path = _write_runtime_files(tmp_path)
    _write_json(
        config_dir / "ner_deployments.json",
        {
            "ner-http-a": {
                "baseUrl": "http://127.0.0.1:9001/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        },
    )
    _write_json(
        config_dir / "llm_deployments.json",
        {
            "llm-mock-a": {
                "adapterType": "mock",
                "enabled": True,
            },
        },
    )
    return config_dir, prompt_path


def _write_empty_runtime_files(tmp_path: Path) -> Path:
    """Deployment 없이 시작하는 기본 Registry와 Prompt를 만듭니다."""

    config_dir, _ = _write_runtime_files(tmp_path)
    _write_json(config_dir / "ner_deployments.json", {})
    _write_json(config_dir / "llm_deployments.json", {})
    return config_dir


@pytest.mark.asyncio
async def test_application_runtime_accepts_empty_deployment_registry(
    tmp_path: Path,
) -> None:
    """빈 종류별 Registry도 정상 Snapshot으로 활성화합니다."""

    config_dir = _write_empty_runtime_files(tmp_path)

    async with application_runtime(config_dir=config_dir) as runtime:
        state = runtime.registry_manager.state

        assert state.generation == 1
        assert dict(state.snapshot.deployments) == {}
        assert runtime.adapter_catalog_service.list_adapters(
            "ner"
        ).adapters
        assert runtime.adapter_catalog_service.list_adapters(
            "llm"
        ).adapters
        assert runtime.policy_settings_manager is not None
        assert (
            runtime.policy_settings_manager.capture().enabled_policy_ids
            == ALLOWED_POLICY_IDS
        )
        assert (config_dir / "policy_settings.json").is_file()


@pytest.mark.asyncio
async def test_application_runtime_initializes_and_closes_http_client(
    tmp_path: Path,
) -> None:
    """정상 Registry를 활성화하고 종료 시 공유 HTTP Client를 닫습니다."""

    config_dir, prompt_path = _write_runtime_files(tmp_path)
    captured_client: httpx.AsyncClient | None = None

    async with application_runtime(
        config_dir=config_dir,
    ) as runtime:
        captured_client = runtime.http_client
        state = runtime.registry_manager.state

        assert captured_client.is_closed is False
        assert state.generation == 1
        assert state.snapshot.detection_prompt is not None
        assert (
            state.snapshot.detection_prompt.compiled_template
            .referenced_variables
            == frozenset({"text", "existing_detections"})
        )
        assert set(state.snapshot.deployments) == {
            "ner-http-a",
            "llm-local-a",
        }
        assert runtime.detection_pipeline is not None
        assert runtime.generation_pipeline is not None
        assert runtime.llm_limits_service is not None
        assert runtime.masking_pipeline is not None
        assert runtime.title_generation_pipeline is not None
        assert runtime.deployment_management_service is not None
        assert (
            runtime.deployment_management_service.registry_manager
            is runtime.registry_manager
        )
        assert (
            state.snapshot.mask_prompt.compiled_template
            .referenced_variables
            == frozenset()
        )
        assert (
            state.snapshot.title_prompt.compiled_template
            .referenced_variables
            == frozenset()
        )

    assert captured_client is not None
    assert captured_client.is_closed is True


@pytest.mark.asyncio
async def test_application_runtime_management_service_updates_snapshot(
    tmp_path: Path,
) -> None:
    """Runtime의 공유 관리 서비스가 파일과 Active Snapshot을 함께 갱신합니다."""

    config_dir, _ = _write_mock_detection_runtime_files(tmp_path)

    async with application_runtime(
        config_dir=config_dir,
    ) as runtime:
        created = runtime.deployment_management_service.add_deployment(
            "ner-http-b",
            {
                "kind": "ner",
                "adapterType": "http_ner",
                "baseUrl": "http://127.0.0.1:9002/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            },
        )

        state = runtime.registry_manager.state
        stored = json.loads(
            (config_dir / "ner_deployments.json").read_text(
                encoding="utf-8"
            )
        )

        assert state.generation == 2
        assert state.snapshot.deployments["ner-http-b"] == created
        assert stored["ner-http-b"] == {
            "enabled": True,
            "baseUrl": "http://127.0.0.1:9002/v1/ner/detect",
            "timeoutMs": 5000,
        }


@pytest.mark.asyncio
async def test_application_runtime_executes_detection_pipeline_with_mocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실제 Runtime 조립 결과로 표준 NER와 Mock LLM 탐지를 실행합니다."""

    config_dir, _ = _write_mock_detection_runtime_files(tmp_path)

    async def detect_without_network(
        backend: HttpNerBackend,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """런타임 조립 테스트에서 외부 NER 서버 호출만 대체합니다."""

        del backend, text, deployment
        return []

    monkeypatch.setattr(
        HttpNerBackend,
        "detect",
        detect_without_network,
    )

    async with application_runtime(
        config_dir=config_dir,
    ) as runtime:
        response = await runtime.detection_pipeline.detect(
            text="홍길동은 프로젝트 알파를 담당합니다.",
            ner_deployment_id="ner-http-a",
            llm_deployment_id="llm-mock-a",
            organization_profile=_organization_profile(),
            source_type="CHAT_TEXT",
        )

    assert response == DetectResponse(detections=())


@pytest.mark.asyncio
async def test_application_runtime_executes_empty_masking_path_with_mock(
    tmp_path: Path,
) -> None:
    """실제 Runtime의 마스킹 Plan이 빈 Detection을 LLM 없이 항등 처리합니다."""

    config_dir, _ = _write_mock_detection_runtime_files(tmp_path)

    async with application_runtime(config_dir=config_dir) as runtime:
        response = await runtime.masking_pipeline.mask(
            text="그대로 유지할 원문 😀",
            llm_deployment_id="llm-mock-a",
            detections=(),
        )

    assert response == MaskResponse.model_validate(
        {"maskedText": "그대로 유지할 원문 😀", "replacements": []}
    )


@pytest.mark.asyncio
async def test_application_runtime_registers_default_backend_providers(
    tmp_path: Path,
) -> None:
    """단일 표준 NER와 기본 LLM Provider만 등록합니다."""

    config_dir, _ = _write_runtime_files(tmp_path)

    async with application_runtime(
        config_dir=config_dir,
    ) as runtime:
        registrations = runtime.backend_providers.registrations

        assert set(registrations) == {
            ("ner", "http_ner"),
            ("llm", "mock"),
            ("llm", "openai_compatible"),
        }
        assert isinstance(
            registrations[("ner", "http_ner")].provider,
            HttpNerBackend,
        )
        assert isinstance(
            registrations[("llm", "mock")].provider,
            MockLlmBackend,
        )
        assert isinstance(
            registrations[("llm", "openai_compatible")].provider,
            OpenAICompatibleLlmBackend,
        )

        ner_catalog = runtime.adapter_catalog_service.list_adapters(
            "ner"
        )
        llm_catalog = runtime.adapter_catalog_service.list_adapters(
            "llm"
        )
        assert ner_catalog.adapters == ("http_ner",)
        assert llm_catalog.adapters == (
            "mock",
            "openai_compatible",
        )
