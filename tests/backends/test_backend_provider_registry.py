"""실제 Backend 구현체를 선택하는 BackendProviderRegistry를 검증합니다."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.backends import (
    BackendProviderContractError,
    BackendProviderLookupError,
    BackendProviderRegistration,
    BackendProviderRegistry,
    DuplicateBackendProviderRegistrationError,
    InvalidBackendProviderError,
    LlmBackend,
    LlmResult,
    NerBackend,
)
from app.backends.backend_registry import (
    BackendRegistration,
    BackendRegistry,
)
from app.schemas.detection import Detection
from app.schemas.registry import DeploymentConfig, RegistryConfig


class FakeNerProvider:
    """Protocol을 상속하지 않고 NER Backend 계약만 구현합니다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, DeploymentConfig]] = []

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """호출 인자를 기록하고 정규화된 Detection을 반환합니다."""

        self.calls.append((text, deployment))
        return [
            Detection(
                start=0,
                end=3,
                text=text[:3],
                type="PERSONAL_IDENTITY",
                policyId="P01",
                source="ner",
                score=0.9,
            )
        ]


class FakeLlmProvider:
    """Protocol을 상속하지 않고 LLM Backend 계약만 구현합니다."""

    def __init__(self) -> None:
        self.calls: list[
            tuple[
                list[dict[str, object]],
                DeploymentConfig,
                dict[str, object],
                dict[str, object] | None,
            ]
        ] = []

    async def generate(
        self,
        messages: list[dict[str, object]],
        deployment: DeploymentConfig,
        parameters: dict[str, object],
        output_schema: dict[str, object] | None = None,
    ) -> LlmResult:
        """호출 인자를 기록하고 공통 LLM 결과를 반환합니다."""

        self.calls.append(
            (messages, deployment, parameters, output_schema)
        )
        return LlmResult(
            text="생성 결과",
            model_name=deployment.model_name,
            finish_reason="stop",
        )


class MissingBackendMethod:
    """NER와 LLM 실행 메서드를 모두 제공하지 않는 잘못된 구현체입니다."""


class NonCallableDetectProvider:
    """detect 이름은 있지만 호출할 수 없는 잘못된 NER 구현체입니다."""

    detect = None


class NerOnlyProvider:
    """LLM 등록에 사용할 generate 메서드가 없는 NER 전용 객체입니다."""

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        """빈 NER 결과를 반환합니다."""

        del text, deployment
        return []


def _deployment(
    *,
    kind: str,
    adapter_type: str,
    enabled: bool = True,
) -> DeploymentConfig:
    """Provider Registry 테스트용 Deployment를 생성합니다."""

    data: dict[str, object] = {
        "kind": kind,
        "adapterType": adapter_type,
        "enabled": enabled,
    }
    if kind == "llm":
        data.update(
            {
                "baseUrl": "http://localhost:9000",
                "modelName": "model-a",
                "timeoutMs": 5000,
            }
        )
    return DeploymentConfig.model_validate(data)


def _registry_config(
    deployments: dict[str, DeploymentConfig],
) -> RegistryConfig:
    """Deployment coverage 검증에 필요한 최소 Registry를 생성합니다."""

    return RegistryConfig(
        deployments=deployments,
    )


def test_typed_require_returns_registered_provider_instances() -> None:
    """NER와 LLM 조회 API가 등록한 구현체를 정확한 종류로 반환합니다."""

    ner_provider = FakeNerProvider()
    llm_provider = FakeLlmProvider()
    registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="native_ner",
                provider=ner_provider,
            ),
            BackendProviderRegistration(
                kind="llm",
                adapter_type="native_llm",
                provider=llm_provider,
            ),
        ]
    )

    resolved_ner = registry.require_ner(
        deployment_id="ner-a",
        deployment=_deployment(
            kind="ner",
            adapter_type="native_ner",
        ),
    )
    resolved_llm = registry.require_llm(
        deployment_id="llm-a",
        deployment=_deployment(
            kind="llm",
            adapter_type="native_llm",
        ),
    )

    assert resolved_ner is ner_provider
    assert resolved_llm is llm_provider
    assert registry.ner_backends["native_ner"] is ner_provider
    assert registry.llm_backends["native_llm"] is llm_provider


def test_same_adapter_type_can_be_registered_for_both_kinds() -> None:
    """같은 adapterType을 NER와 LLM 구현체에 각각 등록할 수 있습니다."""

    ner_provider = FakeNerProvider()
    llm_provider = FakeLlmProvider()
    registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="native",
                provider=ner_provider,
            ),
            BackendProviderRegistration(
                kind="llm",
                adapter_type="native",
                provider=llm_provider,
            ),
        ]
    )

    assert registry.require_ner(
        deployment_id="ner-native",
        deployment=_deployment(
            kind="ner",
            adapter_type="native",
        ),
    ) is ner_provider
    assert registry.require_llm(
        deployment_id="llm-native",
        deployment=_deployment(
            kind="llm",
            adapter_type="native",
        ),
    ) is llm_provider


def test_duplicate_kind_and_adapter_registration_is_rejected() -> None:
    """동일한 kind와 adapterType 구현체를 두 번 등록하지 못하게 합니다."""

    first = BackendProviderRegistration(
        kind="ner",
        adapter_type="native_ner",
        provider=FakeNerProvider(),
    )
    second = BackendProviderRegistration(
        kind="ner",
        adapter_type="native_ner",
        provider=FakeNerProvider(),
    )

    with pytest.raises(
        DuplicateBackendProviderRegistrationError
    ) as error_info:
        BackendProviderRegistry([first, second])

    assert error_info.value.kind == "ner"
    assert error_info.value.adapter_type == "native_ner"


def test_unknown_provider_and_kind_mismatch_are_distinguished() -> None:
    """미등록 adapter와 다른 kind에만 등록된 adapter 오류를 구분합니다."""

    registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="shared_native",
                provider=FakeLlmProvider(),
            )
        ]
    )

    with pytest.raises(BackendProviderLookupError) as unknown_error:
        registry.require_ner(
            deployment_id="ner-unknown",
            deployment=_deployment(
                kind="ner",
                adapter_type="unknown_native",
            ),
        )

    assert (
        unknown_error.value.code
        == "BACKEND_PROVIDER_NOT_REGISTERED"
    )
    assert unknown_error.value.deployment_id == "ner-unknown"
    assert unknown_error.value.adapter_type == "unknown_native"
    assert unknown_error.value.kind == "ner"
    assert unknown_error.value.available_kinds == ()

    with pytest.raises(BackendProviderLookupError) as mismatch_error:
        registry.require_ner(
            deployment_id="ner-shared",
            deployment=_deployment(
                kind="ner",
                adapter_type="shared_native",
            ),
        )

    assert (
        mismatch_error.value.code
        == "BACKEND_PROVIDER_KIND_MISMATCH"
    )
    assert mismatch_error.value.deployment_id == "ner-shared"
    assert mismatch_error.value.adapter_type == "shared_native"
    assert mismatch_error.value.kind == "ner"
    assert mismatch_error.value.available_kinds == ("llm",)


def test_typed_lookup_rejects_deployment_of_the_wrong_kind() -> None:
    """NER 조회 API에 LLM Deployment를 전달하면 kind 오류를 반환합니다."""

    registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="native_llm",
                provider=FakeLlmProvider(),
            )
        ]
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        registry.require_ner(
            deployment_id="llm-a",
            deployment=_deployment(
                kind="llm",
                adapter_type="native_llm",
            ),
        )

    assert (
        error_info.value.code
        == "BACKEND_PROVIDER_KIND_MISMATCH"
    )
    assert error_info.value.kind == "ner"
    assert error_info.value.available_kinds == ("llm",)


@pytest.mark.parametrize(
    ("kind", "provider", "required_method"),
    [
        ("ner", MissingBackendMethod(), "detect"),
        ("ner", NonCallableDetectProvider(), "detect"),
        ("llm", MissingBackendMethod(), "generate"),
        ("llm", NerOnlyProvider(), "generate"),
    ],
)
def test_registration_rejects_invalid_provider(
    kind: str,
    provider: object,
    required_method: str,
) -> None:
    """선택한 kind의 호출 가능 메서드가 없는 구현체를 거부합니다."""

    with pytest.raises(InvalidBackendProviderError) as error_info:
        BackendProviderRegistration(
            kind=kind,  # type: ignore[arg-type]
            adapter_type="native",
            provider=provider,
        )

    assert error_info.value.kind == kind
    assert error_info.value.adapter_type == "native"
    assert error_info.value.required_method == required_method


def test_provider_registration_requires_matching_config_contract() -> None:
    """Provider마다 동일한 kind와 adapterType 설정 계약이 있어야 합니다."""

    providers = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type="ollama",
                provider=FakeLlmProvider(),
            )
        ]
    )
    matching_contracts = BackendRegistry(
        [
            BackendRegistration(
                kind="llm",
                adapter_type="ollama",
            )
        ]
    )

    providers.validate_contracts(matching_contracts)

    with pytest.raises(BackendProviderContractError) as error_info:
        providers.validate_contracts(
            BackendRegistry(
                [
                    BackendRegistration(
                        kind="ner",
                        adapter_type="ollama",
                    )
                ]
            )
        )

    assert error_info.value.kind == "llm"
    assert error_info.value.adapter_type == "ollama"


def test_default_coverage_skips_disabled_deployment() -> None:
    """기본 coverage 검증은 비활성화된 Deployment를 실행 대상에서 제외합니다."""

    registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="native_ner",
                provider=FakeNerProvider(),
            )
        ]
    )
    config = _registry_config(
        {
            "ner-enabled": _deployment(
                kind="ner",
                adapter_type="native_ner",
            ),
            "llm-disabled": _deployment(
                kind="llm",
                adapter_type="missing_llm",
                enabled=False,
            ),
        }
    )

    registry.validate_registry(config)


def test_enabled_deployment_without_provider_fails_coverage() -> None:
    """활성화된 Deployment에 Provider가 없으면 coverage 검증에 실패합니다."""

    registry = BackendProviderRegistry()
    config = _registry_config(
        {
            "llm-enabled": _deployment(
                kind="llm",
                adapter_type="missing_llm",
            )
        }
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        registry.validate_registry(config)

    assert (
        error_info.value.code
        == "BACKEND_PROVIDER_NOT_REGISTERED"
    )
    assert error_info.value.deployment_id == "llm-enabled"


def test_disabled_deployment_can_be_included_in_strict_coverage() -> None:
    """enabled_only=False이면 비활성화된 Deployment도 Provider를 요구합니다."""

    registry = BackendProviderRegistry()
    config = _registry_config(
        {
            "llm-disabled": _deployment(
                kind="llm",
                adapter_type="missing_llm",
                enabled=False,
            )
        }
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        registry.validate_registry(config, enabled_only=False)

    assert (
        error_info.value.code
        == "BACKEND_PROVIDER_NOT_REGISTERED"
    )
    assert error_info.value.deployment_id == "llm-disabled"


def test_registry_maps_are_read_only_and_input_is_defensively_copied() -> None:
    """등록 입력 변경을 격리하고 외부에 노출한 모든 Mapping을 고정합니다."""

    provider = FakeNerProvider()
    registration = BackendProviderRegistration(
        kind="ner",
        adapter_type="native_ner",
        provider=provider,
    )
    source = [registration]
    registry = BackendProviderRegistry(source)
    source.clear()

    assert registry.require_ner(
        deployment_id="ner-a",
        deployment=_deployment(
            kind="ner",
            adapter_type="native_ner",
        ),
    ) is provider

    with pytest.raises(TypeError):
        registry.registrations[("ner", "other")] = registration  # type: ignore[index]
    with pytest.raises(TypeError):
        registry.ner_backends["other"] = provider  # type: ignore[index]
    with pytest.raises(TypeError):
        registry.llm_backends["other"] = FakeLlmProvider()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        registration.adapter_type = "other"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_structural_fake_providers_can_be_called_without_inheritance() -> None:
    """명시적 상속 없는 구현체도 Protocol 계약으로 조회하고 비동기 호출합니다."""

    ner_provider = FakeNerProvider()
    llm_provider = FakeLlmProvider()
    registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="native",
                provider=ner_provider,
            ),
            BackendProviderRegistration(
                kind="llm",
                adapter_type="native",
                provider=llm_provider,
            ),
        ]
    )
    ner_deployment = _deployment(
        kind="ner",
        adapter_type="native",
    )
    llm_deployment = _deployment(
        kind="llm",
        adapter_type="native",
    )
    messages = [{"role": "user", "content": "홍길동"}]
    parameters = {"temperature": 0}
    output_schema = {"type": "object"}

    assert isinstance(ner_provider, NerBackend)
    assert isinstance(llm_provider, LlmBackend)

    detections = await registry.require_ner(
        deployment_id="ner-native",
        deployment=ner_deployment,
    ).detect("홍길동", ner_deployment)
    result = await registry.require_llm(
        deployment_id="llm-native",
        deployment=llm_deployment,
    ).generate(
        messages,
        llm_deployment,
        parameters,
        output_schema,
    )

    assert detections[0].source == "ner"
    assert detections[0].text == "홍길동"
    assert ner_provider.calls == [("홍길동", ner_deployment)]
    assert result == LlmResult(
        text="생성 결과",
        model_name="model-a",
        finish_reason="stop",
    )
    assert llm_provider.calls == [
        (
            messages,
            llm_deployment,
            parameters,
            output_schema,
        )
    ]
