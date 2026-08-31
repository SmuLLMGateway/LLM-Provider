"""RegistrySnapshotBuilder의 Deployment·Prompt 후보 조립을 검증합니다."""

import json
from pathlib import Path

import pytest

from app.backends import (
    BackendProviderContractError,
    BackendProviderLookupError,
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.backends.backend_registry import BackendValidationError
from app.registry.snapshot_builder import RegistrySnapshotBuilder
from app.prompts.prompt_limits import PromptLimits
from app.prompts.prompt_loader import (
    PromptLoader,
    PromptTemplateNotFoundError,
)
from app.prompts.prompt_errors import PromptVariableContractError
from app.prompts.prompt_renderer import PromptRenderer
from app.schemas.detection import ALLOWED_POLICY_IDS
from app.schemas.registry import RegistryConfig


class StaticRegistryReader:
    """고정 RegistryConfig를 Builder에 제공하는 Reader입니다."""

    def __init__(self, registry: RegistryConfig) -> None:
        self.registry = registry
        self.load_calls = 0

    def load(self) -> RegistryConfig:
        """현재 Registry를 반환하고 호출 횟수를 기록합니다."""

        self.load_calls += 1
        return self.registry


class StubNerBackend:
    """Provider coverage 테스트용 최소 NER 구현체입니다."""

    async def detect(self, text, deployment):
        """실행 계약만 제공하며 호출되지는 않습니다."""

        del text, deployment
        return []


class StubLlmBackend:
    """Provider coverage 테스트용 최소 LLM 구현체입니다."""

    async def generate(
        self,
        messages,
        deployment,
        parameters,
        output_schema=None,
    ):
        """실행 계약만 제공하며 호출되지는 않습니다."""

        del messages, deployment, parameters, output_schema
        raise NotImplementedError


def _registry(
    *,
    llm_model_name: str = "model-a",
) -> RegistryConfig:
    """Builder 테스트에 사용할 유효한 Deployment Registry를 만듭니다."""

    return RegistryConfig.model_validate(
        {
            "deployments": {
                "ner-a": {
                    "kind": "ner",
                    "adapterType": "mock",
                    "enabled": True,
                },
                "llm-a": {
                    "kind": "llm",
                    "adapterType": "openai_compatible",
                    "baseUrl": "http://localhost:9000/v1",
                    "modelName": llm_model_name,
                    "timeoutMs": 5000,
                    "enabled": True,
                },
            }
        }
    )


def _write_policy_prompts(
    prompts_dir: Path,
    sources: dict[str, str] | None = None,
) -> None:
    """정확한 14개 테스트 정책 Prompt를 저장합니다."""

    (prompts_dir / "policy_prompts.json").write_text(
        json.dumps(
            sources
            if sources is not None
            else {
                policy_id: f"TEST_POLICY_RULE_{policy_id}"
                for policy_id in ALLOWED_POLICY_IDS
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _loader(
    tmp_path: Path,
    source: str = "{{ text }} / {{ existing_detections }}",
    *,
    mask_source: str = "탐지 구간을 마스킹하십시오.",
    title_source: str = "제목만 생성하십시오.",
    policy_sources: dict[str, str] | None = None,
) -> PromptLoader:
    """탐지·제목 Prompt 파일을 만들고 탐지 Loader를 반환합니다."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    prompt_path = prompts_dir / "prompts.j2"
    prompt_path.write_text(
        source,
        encoding="utf-8",
    )
    (prompts_dir / "title_prompt.j2").write_text(
        title_source,
        encoding="utf-8",
    )
    (prompts_dir / "mask_prompt.j2").write_text(
        mask_source,
        encoding="utf-8",
    )
    _write_policy_prompts(prompts_dir, policy_sources)
    return PromptLoader(prompt_path)


def _builder(
    tmp_path: Path,
    *,
    registry: RegistryConfig | None = None,
    source: str = "{{ text }} / {{ existing_detections }}",
    mask_source: str = "탐지 구간을 마스킹하십시오.",
    title_source: str = "제목만 생성하십시오.",
    policy_sources: dict[str, str] | None = None,
    providers: BackendProviderRegistry | None = None,
) -> RegistrySnapshotBuilder:
    """유효한 Reader와 Prompt Loader를 사용하는 Builder를 만듭니다."""

    return RegistrySnapshotBuilder(
        StaticRegistryReader(registry or _registry()),
        loader=_loader(
            tmp_path,
            source,
            mask_source=mask_source,
            title_source=title_source,
            policy_sources=policy_sources,
        ),
        backend_providers=providers,
    )


def test_builds_snapshot_with_deployments_and_prompt(
    tmp_path: Path,
) -> None:
    """Deployment와 컴파일된 고정 Prompt를 하나의 Snapshot으로 묶습니다."""

    snapshot = _builder(tmp_path).build()

    assert set(snapshot.deployments) == {"ner-a", "llm-a"}
    assert (
        snapshot.detection_prompt.compiled_template.referenced_variables
        == frozenset({"text", "existing_detections"})
    )
    assert (
        snapshot.title_prompt.compiled_template.referenced_variables
        == frozenset()
    )
    assert (
        snapshot.mask_prompt.compiled_template.referenced_variables
        == frozenset()
    )
    assert len(snapshot.snapshot_id) == 64


def test_build_reads_registry_once(
    tmp_path: Path,
) -> None:
    """한 후보 안에서 Deployment 파일을 여러 시점에 다시 읽지 않습니다."""

    reader = StaticRegistryReader(_registry())
    builder = RegistrySnapshotBuilder(
        reader,
        loader=_loader(tmp_path),
    )

    builder.build()

    assert reader.load_calls == 1


def test_snapshot_id_is_deterministic(
    tmp_path: Path,
) -> None:
    """동일한 Deployment와 Prompt 내용은 같은 Snapshot ID를 만듭니다."""

    first = _builder(tmp_path).build()
    second = _builder(tmp_path).build()

    assert first.snapshot_id == second.snapshot_id


def test_snapshot_id_changes_with_deployment(
    tmp_path: Path,
) -> None:
    """Deployment 실행 설정 변경을 Snapshot 차이로 반영합니다."""

    first = _builder(tmp_path).build()
    second = _builder(
        tmp_path,
        registry=_registry(llm_model_name="model-b"),
    ).build()

    assert first.snapshot_id != second.snapshot_id


def test_snapshot_id_changes_with_prompt_source(
    tmp_path: Path,
) -> None:
    """새 Builder는 배포된 Prompt 본문을 Snapshot ID에 반영합니다."""

    first = _builder(tmp_path).build()
    second = _builder(
        tmp_path,
        source="검사: {{ text }} / {{ existing_detections }}",
    ).build()

    assert first.snapshot_id != second.snapshot_id


def test_snapshot_id_changes_with_title_prompt_source(
    tmp_path: Path,
) -> None:
    """제목 Prompt 변경도 새 Builder의 Snapshot ID에 반영됩니다."""

    first = _builder(tmp_path).build()
    second = _builder(
        tmp_path,
        title_source="대화를 안전한 제목으로 요약하십시오.",
    ).build()

    assert first.snapshot_id != second.snapshot_id


def test_snapshot_id_changes_with_mask_prompt_source(
    tmp_path: Path,
) -> None:
    """새 Builder의 마스킹 Prompt도 Snapshot ID에 포함합니다."""

    first = _builder(tmp_path).build()
    second = _builder(
        tmp_path,
        mask_source="모든 target을 안전하게 마스킹하십시오.",
    ).build()

    assert first.snapshot_id != second.snapshot_id


def test_snapshot_id_changes_with_policy_prompt_source(
    tmp_path: Path,
) -> None:
    """정책별 Prompt 변경도 새 Builder의 Snapshot ID에 반영됩니다."""

    first = _builder(tmp_path).build()
    policy_sources = {
        policy_id: f"TEST_POLICY_RULE_{policy_id}"
        for policy_id in ALLOWED_POLICY_IDS
    }
    policy_sources["P01"] = "CHANGED_POLICY_RULE_P01"
    second = _builder(
        tmp_path,
        policy_sources=policy_sources,
    ).build()

    assert first.snapshot_id != second.snapshot_id


def test_builder_keeps_first_prompt_artifact_for_process_lifetime(
    tmp_path: Path,
) -> None:
    """첫 정상 Build 뒤 파일이 바뀌어도 고정 Prompt를 다시 읽지 않습니다."""

    reader = StaticRegistryReader(_registry())
    loader = _loader(tmp_path)
    builder = RegistrySnapshotBuilder(reader, loader=loader)

    first = builder.build()
    first_prompt_hash = first.detection_prompt.content_hash
    first_policy_hash = first.detection_prompt.policy_prompts.content_hash
    first_mask_hash = first.mask_prompt.content_hash
    first_title_hash = first.title_prompt.content_hash
    loader.prompt_path.write_text(
        "변경: {{ text }} / {{ existing_detections }}",
        encoding="utf-8",
    )
    (loader.prompt_path.parent / "title_prompt.j2").write_text(
        "변경된 제목 Prompt",
        encoding="utf-8",
    )
    (loader.prompt_path.parent / "mask_prompt.j2").write_text(
        "변경된 마스킹 Prompt",
        encoding="utf-8",
    )
    changed_policy_sources = {
        policy_id: f"CHANGED_POLICY_RULE_{policy_id}"
        for policy_id in ALLOWED_POLICY_IDS
    }
    _write_policy_prompts(
        loader.prompt_path.parent,
        changed_policy_sources,
    )
    second = builder.build()

    assert second.detection_prompt is first.detection_prompt
    assert second.detection_prompt.content_hash == first_prompt_hash
    assert (
        second.detection_prompt.policy_prompts.content_hash
        == first_policy_hash
    )
    assert second.mask_prompt is first.mask_prompt
    assert second.mask_prompt.content_hash == first_mask_hash
    assert second.title_prompt is first.title_prompt
    assert second.title_prompt.content_hash == first_title_hash
    assert second.snapshot_id == first.snapshot_id

    reader.registry = _registry(llm_model_name="model-b")
    third = builder.build()

    assert third.detection_prompt is first.detection_prompt
    assert third.mask_prompt is first.mask_prompt
    assert third.title_prompt is first.title_prompt
    assert third.snapshot_id != first.snapshot_id


def test_missing_fixed_prompt_is_rejected(
    tmp_path: Path,
) -> None:
    """필수 고정 Prompt 파일이 없으면 Snapshot을 만들지 않습니다."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    builder = RegistrySnapshotBuilder(
        StaticRegistryReader(_registry()),
        loader=PromptLoader(prompts_dir / "prompts.j2"),
    )

    with pytest.raises(PromptTemplateNotFoundError):
        builder.build()


def test_missing_policy_prompt_is_rejected(
    tmp_path: Path,
) -> None:
    """정책 Prompt 파일이 없으면 불완전한 Snapshot을 활성화하지 않습니다."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt_path = prompts_dir / "prompts.j2"
    prompt_path.write_text(
        "{{ text }} / {{ existing_detections }}",
        encoding="utf-8",
    )
    (prompts_dir / "title_prompt.j2").write_text(
        "제목만 생성하십시오.",
        encoding="utf-8",
    )
    (prompts_dir / "mask_prompt.j2").write_text(
        "탐지 구간을 마스킹하십시오.",
        encoding="utf-8",
    )
    builder = RegistrySnapshotBuilder(
        StaticRegistryReader(_registry()),
        loader=PromptLoader(prompt_path),
    )

    with pytest.raises(PromptTemplateNotFoundError) as error_info:
        builder.build()

    assert error_info.value.template_path.endswith("policy_prompts.json")


def test_missing_title_prompt_is_rejected(
    tmp_path: Path,
) -> None:
    """제목 Prompt가 없으면 불완전한 Snapshot을 활성화하지 않습니다."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt_path = prompts_dir / "prompts.j2"
    prompt_path.write_text(
        "{{ text }} / {{ existing_detections }}",
        encoding="utf-8",
    )
    (prompts_dir / "mask_prompt.j2").write_text(
        "탐지 구간을 마스킹하십시오.",
        encoding="utf-8",
    )
    _write_policy_prompts(prompts_dir)
    builder = RegistrySnapshotBuilder(
        StaticRegistryReader(_registry()),
        loader=PromptLoader(prompt_path),
    )

    with pytest.raises(PromptTemplateNotFoundError) as error_info:
        builder.build()

    assert error_info.value.template_path.endswith(
        "title_prompt.j2"
    )


def test_missing_mask_prompt_is_rejected(
    tmp_path: Path,
) -> None:
    """마스킹 Prompt가 없으면 불완전한 Snapshot을 활성화하지 않습니다."""

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt_path = prompts_dir / "prompts.j2"
    prompt_path.write_text(
        "{{ text }} / {{ existing_detections }}",
        encoding="utf-8",
    )
    (prompts_dir / "title_prompt.j2").write_text(
        "제목만 생성하십시오.",
        encoding="utf-8",
    )
    _write_policy_prompts(prompts_dir)
    builder = RegistrySnapshotBuilder(
        StaticRegistryReader(_registry()),
        loader=PromptLoader(prompt_path),
    )

    with pytest.raises(PromptTemplateNotFoundError) as error_info:
        builder.build()

    assert error_info.value.template_path.endswith("mask_prompt.j2")


def test_prompt_variable_contract_is_validated(
    tmp_path: Path,
) -> None:
    """후속 탐지 Prompt에 기존 탐지 근거 변수가 빠지면 거부합니다."""

    with pytest.raises(PromptVariableContractError) as error_info:
        _builder(tmp_path, source="{{ text }}").build()

    assert error_info.value.missing_variables == frozenset(
        {"existing_detections"}
    )


def test_title_prompt_rejects_external_variables(
    tmp_path: Path,
) -> None:
    """제목 System Prompt가 사용자 입력 변수를 직접 참조하지 못하게 합니다."""

    with pytest.raises(PromptVariableContractError) as error_info:
        _builder(
            tmp_path,
            title_source="{{ text }}",
        ).build()

    assert error_info.value.unexpected_variables == frozenset({"text"})


def test_mask_prompt_rejects_external_variables(
    tmp_path: Path,
) -> None:
    """마스킹 System Prompt에 원문이나 target 변수를 직접 삽입하지 못합니다."""

    with pytest.raises(PromptVariableContractError) as error_info:
        _builder(
            tmp_path,
            mask_source="{{ text }} / {{ targets }}",
        ).build()

    assert error_info.value.unexpected_variables == frozenset(
        {"text", "targets"}
    )


def test_invalid_deployment_contract_stops_snapshot_build(
    tmp_path: Path,
) -> None:
    """등록되지 않은 Adapter 설정은 활성 Snapshot에 들어가지 않습니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "llm-a": {
                    "kind": "llm",
                    "adapterType": "unknown",
                    "enabled": True,
                }
            }
        }
    )

    with pytest.raises(BackendValidationError) as error_info:
        _builder(tmp_path, registry=registry).build()

    assert error_info.value.code == "ADAPTER_NOT_REGISTERED"
    assert error_info.value.deployment_id == "llm-a"


def test_enabled_deployment_requires_provider_when_configured(
    tmp_path: Path,
) -> None:
    """Provider coverage를 켜면 모든 활성 Deployment 구현체를 요구합니다."""

    providers = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="mock",
                provider=StubNerBackend(),
            )
        ]
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        _builder(tmp_path, providers=providers).build()

    assert error_info.value.deployment_id == "llm-a"


def test_matching_providers_allow_snapshot_build(
    tmp_path: Path,
) -> None:
    """설정 계약과 구현체가 모두 있으면 Provider 검증을 통과합니다."""

    providers = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="mock",
                provider=StubNerBackend(),
            ),
            BackendProviderRegistration(
                kind="llm",
                adapter_type="openai_compatible",
                provider=StubLlmBackend(),
            ),
        ]
    )

    snapshot = _builder(tmp_path, providers=providers).build()

    assert set(snapshot.deployments) == {"ner-a", "llm-a"}


def test_provider_without_backend_contract_is_rejected_at_init(
    tmp_path: Path,
) -> None:
    """Provider key와 Deployment 설정 계약의 동시 등록을 강제합니다."""

    providers = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="custom",
                provider=StubLlmBackend(),
            )
        ]
    )

    with pytest.raises(BackendProviderContractError):
        _builder(tmp_path, providers=providers)


def test_builder_rejects_loader_limit_mismatch(
    tmp_path: Path,
) -> None:
    """파일 읽기와 컴파일 단계가 서로 다른 크기 제한을 쓰지 않습니다."""

    loader = _loader(tmp_path)
    renderer = PromptRenderer(
        limits=PromptLimits(max_template_bytes=1024)
    )

    with pytest.raises(ValueError, match="같은 PromptLimits"):
        RegistrySnapshotBuilder(
            StaticRegistryReader(_registry()),
            loader=loader,
            renderer=renderer,
        )
