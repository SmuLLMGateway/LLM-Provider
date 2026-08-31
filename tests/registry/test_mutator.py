"""Deployment 전용 RegistryMutator의 순수 변경 동작을 검증합니다."""

import pytest
from pydantic import ValidationError

from app.registry.mutator import (
    DeploymentAlreadyExistsError,
    DeploymentMustBeDisabledError,
    DeploymentNotFoundError,
    RegistryMutator,
)
from app.schemas.registry import DeploymentConfig, RegistryConfig


def _deployment(
    *,
    enabled: bool = True,
) -> DeploymentConfig:
    """Mutator 테스트용 Mock NER Deployment를 만듭니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": "ner",
            "adapterType": "mock",
            "enabled": enabled,
        }
    )


def _llm_deployment(
    *,
    enabled: bool = True,
) -> DeploymentConfig:
    """실행 설정 보존 검증용 OpenAI 호환 LLM을 만듭니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": "llm",
            "adapterType": "openai_compatible",
            "baseUrl": "http://127.0.0.1:11434/v1",
            "modelName": "qwen3:8b",
            "timeoutMs": 300000,
            "enabled": enabled,
        }
    )


def test_add_deployment_returns_new_registry() -> None:
    """원본을 바꾸지 않고 Deployment가 추가된 Registry를 반환합니다."""

    original = RegistryConfig(deployments={})

    updated = RegistryMutator().add_deployment(
        original,
        deployment_id="ner-a",
        deployment=_deployment(),
    )

    assert original.deployments == {}
    assert updated.deployments == {"ner-a": _deployment()}
    assert updated is not original


def test_add_deployment_rejects_duplicate_id() -> None:
    """기존 Deployment ID를 덮어쓰는 추가 요청을 거부합니다."""

    registry = RegistryConfig(
        deployments={"ner-a": _deployment()}
    )

    with pytest.raises(DeploymentAlreadyExistsError) as error_info:
        RegistryMutator().add_deployment(
            registry,
            deployment_id="ner-a",
            deployment=_deployment(enabled=False),
        )

    assert error_info.value.deployment_id == "ner-a"


@pytest.mark.parametrize("deployment_id", ["", "NER A", "UPPER"])
def test_add_deployment_rejects_invalid_resource_id(
    deployment_id: str,
) -> None:
    """순수 Mutator도 Registry 공통 ID 문법을 우회하지 못합니다."""

    original = RegistryConfig(deployments={})

    with pytest.raises(ValidationError):
        RegistryMutator().add_deployment(
            original,
            deployment_id=deployment_id,
            deployment=_deployment(),
        )

    assert original.deployments == {}


def test_update_deployment_replaces_whole_config() -> None:
    """기존 Deployment를 검증된 새 설정으로 교체합니다."""

    original = RegistryConfig(
        deployments={"ner-a": _deployment()}
    )
    replacement = _deployment(enabled=False)

    updated = RegistryMutator().update_deployment(
        original,
        deployment_id="ner-a",
        deployment=replacement,
    )

    assert original.deployments["ner-a"].enabled is True
    assert updated.deployments["ner-a"] == replacement


def test_update_deployment_rejects_unknown_id() -> None:
    """등록되지 않은 Deployment 수정 요청을 거부합니다."""

    with pytest.raises(DeploymentNotFoundError) as error_info:
        RegistryMutator().update_deployment(
            RegistryConfig(deployments={}),
            deployment_id="missing",
            deployment=_deployment(),
        )

    assert error_info.value.deployment_id == "missing"


def test_update_deployment_rejects_kind_change() -> None:
    """기존 ID를 유지한 채 NER와 LLM 종류를 바꾸지 못하게 합니다."""

    registry = RegistryConfig(
        deployments={"ner-a": _deployment()}
    )
    llm = DeploymentConfig.model_validate(
        {
            "kind": "llm",
            "adapterType": "mock",
            "enabled": True,
        }
    )

    with pytest.raises(DeploymentNotFoundError) as error_info:
        RegistryMutator().update_deployment(
            registry,
            deployment_id="ner-a",
            deployment=llm,
        )

    assert error_info.value.deployment_id == "ner-a"
    assert registry.deployments["ner-a"].kind == "ner"


def test_set_deployment_enabled_preserves_all_other_settings() -> None:
    """활성 상태를 바꿔도 기존 Backend 실행 설정은 그대로 보존합니다."""

    deployment = _llm_deployment(enabled=True)
    original = RegistryConfig(
        deployments={"llm-a": deployment}
    )

    updated = RegistryMutator().set_deployment_enabled(
        original,
        deployment_id="llm-a",
        kind="llm",
        enabled=False,
    )

    assert original.deployments["llm-a"] == deployment
    assert updated.deployments["llm-a"] == deployment.model_copy(
        update={"enabled": False}
    )
    assert updated.deployments["llm-a"].adapter_type == (
        deployment.adapter_type
    )
    assert updated.deployments["llm-a"].base_url == deployment.base_url
    assert updated.deployments["llm-a"].model_name == (
        deployment.model_name
    )
    assert updated.deployments["llm-a"].timeout_ms == (
        deployment.timeout_ms
    )


@pytest.mark.parametrize(
    ("deployment_id", "kind"),
    [
        ("missing", "llm"),
        ("llm-a", "ner"),
    ],
)
def test_set_deployment_enabled_hides_missing_and_wrong_kind(
    deployment_id: str,
    kind: str,
) -> None:
    """없는 ID와 경로 종류 불일치는 같은 Not Found 오류로 거부합니다."""

    original = RegistryConfig(
        deployments={"llm-a": _llm_deployment()}
    )

    with pytest.raises(DeploymentNotFoundError) as error_info:
        RegistryMutator().set_deployment_enabled(
            original,
            deployment_id=deployment_id,
            kind=kind,  # type: ignore[arg-type]
            enabled=False,
        )

    assert error_info.value.deployment_id == deployment_id
    assert original.deployments["llm-a"].enabled is True


def test_set_deployment_enabled_is_idempotent() -> None:
    """현재 값과 같은 요청을 반복해도 Registry 내용은 달라지지 않습니다."""

    original = RegistryConfig(
        deployments={"ner-a": _deployment(enabled=True)}
    )

    updated = RegistryMutator().set_deployment_enabled(
        original,
        deployment_id="ner-a",
        kind="ner",
        enabled=True,
    )

    assert updated == original
    assert original.deployments["ner-a"].enabled is True


def test_delete_inactive_deployment_preserves_other_resources() -> None:
    """비활성 대상만 제거하고 원본과 나머지 종류별 Deployment는 보존합니다."""

    deleted = _deployment(enabled=False)
    retained_ner = _deployment(enabled=True)
    retained_llm = _llm_deployment(enabled=True)
    original = RegistryConfig(
        deployments={
            "ner-delete": deleted,
            "ner-retained": retained_ner,
            "llm-retained": retained_llm,
        }
    )

    updated = RegistryMutator().delete_deployment(
        original,
        deployment_id="ner-delete",
        kind="ner",
    )

    assert updated is not original
    assert "ner-delete" not in updated.deployments
    assert updated.deployments == {
        "ner-retained": retained_ner,
        "llm-retained": retained_llm,
    }
    assert original.deployments == {
        "ner-delete": deleted,
        "ner-retained": retained_ner,
        "llm-retained": retained_llm,
    }


def test_delete_active_deployment_is_rejected_without_mutation() -> None:
    """활성 Deployment는 명시적인 정책 오류로 거부하고 원본을 유지합니다."""

    deployment = _deployment(enabled=True)
    original = RegistryConfig(
        deployments={"ner-active": deployment}
    )

    with pytest.raises(DeploymentMustBeDisabledError) as error_info:
        RegistryMutator().delete_deployment(
            original,
            deployment_id="ner-active",
            kind="ner",
        )

    assert error_info.value.code == "DEPLOYMENT_MUST_BE_DISABLED"
    assert error_info.value.deployment_id == "ner-active"
    assert original.deployments == {"ner-active": deployment}


@pytest.mark.parametrize(
    ("deployment_id", "kind"),
    [
        ("missing", "ner"),
        ("llm-inactive", "ner"),
        ("ner-inactive", "llm"),
    ],
)
def test_delete_deployment_hides_missing_and_wrong_kind(
    deployment_id: str,
    kind: str,
) -> None:
    """없는 ID와 경로 종류 불일치는 같은 Not Found 오류로 거부합니다."""

    ner = _deployment(enabled=False)
    llm = _llm_deployment(enabled=False)
    original = RegistryConfig(
        deployments={
            "ner-inactive": ner,
            "llm-inactive": llm,
        }
    )

    with pytest.raises(DeploymentNotFoundError) as error_info:
        RegistryMutator().delete_deployment(
            original,
            deployment_id=deployment_id,
            kind=kind,  # type: ignore[arg-type]
        )

    assert error_info.value.deployment_id == deployment_id
    assert original.deployments == {
        "ner-inactive": ner,
        "llm-inactive": llm,
    }
