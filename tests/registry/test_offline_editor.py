"""OfflineRegistryEditor의 입력 검증과 파일 저장 경계를 확인합니다."""

from pathlib import Path

import pytest
from pydantic import ValidationError

import app.registry as registry_package
from app.prompts.prompt_loader import PromptLoader
from app.prompts.policy_prompt_loader import (
    DEFAULT_POLICY_PROMPTS_PATH,
    POLICY_PROMPTS_FILENAME,
)
from app.prompts.prompt_renderer import PromptRenderer
from app.registry.offline_editor import (
    OfflineRegistryEditor,
    add_deployment_offline,
    update_deployment_offline,
)
from app.registry.file_store import RegistryFileStore
from app.registry.manager import RegistryManager, ReloadStatus
from app.registry.mutator import (
    DeploymentAlreadyExistsError,
    DeploymentNotFoundError,
    RegistryMutator,
)
from app.registry.snapshot_builder import RegistrySnapshotBuilder
from app.registry.validator import RegistryValidator
from app.schemas.registry import DeploymentConfig


def _initialize(config_dir: Path) -> RegistryFileStore:
    """종류별 빈 Deployment JSON을 만들고 Store를 반환합니다."""

    store = RegistryFileStore(config_dir)
    store.initialize()
    return store


def _ner(*, enabled: bool = True) -> dict[str, object]:
    """Editor 입력용 공통 HTTP NER 설정을 만듭니다."""

    return {
        "kind": "ner",
        "adapterType": "http_ner",
        "baseUrl": "http://localhost:9100/v1/ner/detect",
        "timeoutMs": 5000,
        "enabled": enabled,
    }


def _llm() -> dict[str, object]:
    """Editor 입력용 OpenAI 호환 LLM 설정을 만듭니다."""

    return {
        "kind": "llm",
        "adapterType": "openai_compatible",
        "baseUrl": "http://localhost:9000/v1",
        "modelName": "model-a",
        "timeoutMs": 5000,
        "enabled": True,
    }


def test_add_deployment_validates_and_persists_offline(
    tmp_path: Path,
) -> None:
    """dict 입력을 DeploymentConfig로 검증한 뒤 파일에 추가합니다."""

    store = _initialize(tmp_path / "config")

    created = OfflineRegistryEditor(store).add_deployment(
        "ner-a",
        _ner(),
    )

    assert isinstance(created, DeploymentConfig)
    assert store.load().deployments["ner-a"] == created


def test_add_multiple_deployments(
    tmp_path: Path,
) -> None:
    """NER와 LLM Deployment를 독립된 ID로 연속 추가할 수 있습니다."""

    store = _initialize(tmp_path / "config")
    editor = OfflineRegistryEditor(store)

    editor.add_deployment("ner-a", _ner())
    editor.add_deployment("llm-a", _llm())

    assert set(store.load().deployments) == {"ner-a", "llm-a"}


def test_add_deployment_rejects_duplicate(
    tmp_path: Path,
) -> None:
    """중복 ID 추가가 기존 파일 내용을 덮어쓰지 않습니다."""

    store = _initialize(tmp_path / "config")
    editor = OfflineRegistryEditor(store)
    editor.add_deployment("ner-a", _ner())

    with pytest.raises(DeploymentAlreadyExistsError):
        editor.add_deployment("ner-a", _ner(enabled=False))

    assert store.load().deployments["ner-a"].enabled is True


def test_update_deployment_replaces_config(
    tmp_path: Path,
) -> None:
    """기존 Deployment 전체 설정을 검증된 값으로 교체합니다."""

    store = _initialize(tmp_path / "config")
    editor = OfflineRegistryEditor(store)
    editor.add_deployment("ner-a", _ner())

    updated = editor.update_deployment(
        "ner-a",
        _ner(enabled=False),
    )

    assert updated.enabled is False
    assert store.load().deployments["ner-a"] == updated


def test_update_unknown_deployment_is_rejected(
    tmp_path: Path,
) -> None:
    """등록되지 않은 ID의 수정 요청을 거부합니다."""

    store = _initialize(tmp_path / "config")

    with pytest.raises(DeploymentNotFoundError) as error_info:
        OfflineRegistryEditor(store).update_deployment(
            "missing",
            _ner(),
        )

    assert error_info.value.deployment_id == "missing"


@pytest.mark.parametrize("deployment_id", ["", "NER A", "UPPER"])
def test_editor_rejects_invalid_resource_id(
    tmp_path: Path,
    deployment_id: str,
) -> None:
    """파일 변경 전에 Deployment ID 문법을 검증합니다."""

    store = _initialize(tmp_path / "config")

    with pytest.raises(ValidationError):
        OfflineRegistryEditor(store).add_deployment(
            deployment_id,
            _ner(),
        )

    assert store.load().deployments == {}


def test_editor_rejects_invalid_deployment_config(
    tmp_path: Path,
) -> None:
    """Pydantic 구조가 잘못된 입력을 저장하지 않습니다."""

    store = _initialize(tmp_path / "config")

    with pytest.raises(ValidationError):
        OfflineRegistryEditor(store).add_deployment(
            "ner-a",
            {
                "kind": "ner",
                "adapterType": "http_ner",
                "baseUrl": "http://localhost:9100/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": "yes",
            },
        )

    assert store.load().deployments == {}


def test_editor_and_store_must_share_validator(
    tmp_path: Path,
) -> None:
    """저장과 변경 단계에 서로 다른 Registry 정책을 주입하지 못합니다."""

    store = RegistryFileStore(
        tmp_path / "config",
        validator=RegistryValidator(),
    )

    with pytest.raises(ValueError, match="같은 RegistryValidator"):
        OfflineRegistryEditor(
            store,
            mutator=RegistryMutator(
                validator=RegistryValidator()
            ),
        )


def test_add_deployment_offline_convenience_function(
    tmp_path: Path,
) -> None:
    """모듈 편의 함수도 동일한 파일 저장 계약을 사용합니다."""

    config_dir = tmp_path / "config"
    RegistryFileStore(config_dir).initialize()

    created = add_deployment_offline(
        "ner-a",
        _ner(),
        config_dir,
    )

    assert RegistryFileStore(config_dir).load().deployments[
        "ner-a"
    ] == created


def test_update_deployment_offline_convenience_function(
    tmp_path: Path,
) -> None:
    """모듈 편의 함수로 기존 Deployment 전체를 교체할 수 있습니다."""

    config_dir = tmp_path / "config"
    RegistryFileStore(config_dir).initialize()
    add_deployment_offline("ner-a", _ner(), config_dir)

    updated = update_deployment_offline(
        "ner-a",
        _ner(enabled=False),
        config_dir,
    )

    assert updated.enabled is False
    assert RegistryFileStore(config_dir).load().deployments[
        "ner-a"
    ] == updated


def test_offline_editor_does_not_activate_running_snapshot(
    tmp_path: Path,
) -> None:
    """오프라인 파일 변경은 실행 중 Snapshot을 자동으로 교체하지 않습니다."""

    config_dir = tmp_path / "config"
    store = _initialize(config_dir)
    prompt_path = config_dir / "prompts.j2"
    title_prompt_path = config_dir / "title_prompt.j2"
    prompt_path.write_text(
        "{{ text }}\n{{ existing_detections }}\n",
        encoding="utf-8",
    )
    title_prompt_path.write_text(
        "제목만 생성하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / "mask_prompt.j2").write_text(
        "탐지 구간을 마스킹하십시오.\n",
        encoding="utf-8",
    )
    (config_dir / POLICY_PROMPTS_FILENAME).write_bytes(
        DEFAULT_POLICY_PROMPTS_PATH.read_bytes()
    )
    renderer = PromptRenderer()
    manager = RegistryManager(
        RegistrySnapshotBuilder(
            store,
            loader=PromptLoader(
                prompt_path,
                limits=renderer.limits,
            ),
            title_loader=PromptLoader(
                title_prompt_path,
                limits=renderer.limits,
            ),
            renderer=renderer,
            registry_validator=store.validator,
            coordinator=store.coordinator,
        )
    )
    initial = manager.initialize()

    OfflineRegistryEditor(store).add_deployment(
        "ner-a",
        _ner(),
    )

    assert manager.capture() is initial.snapshot
    assert manager.capture().deployments == {}

    result = manager.try_reload()

    assert result.status is ReloadStatus.APPLIED
    assert "ner-a" in manager.capture().deployments


def test_registry_package_exposes_only_explicit_offline_editor_names() -> None:
    """모호한 온라인 우회용 Editor와 편의 함수 이름을 공개하지 않습니다."""

    assert (
        registry_package.OfflineRegistryEditor
        is OfflineRegistryEditor
    )
    assert not hasattr(registry_package, "RegistryEditor")
    assert not hasattr(registry_package, "add_deployment")
    assert not hasattr(registry_package, "update_deployment")
