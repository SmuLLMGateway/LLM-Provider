"""종류별 Deployment JSON 저장소의 조립·원자성과 검증을 확인합니다."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.json_codec import StrictJsonDecodeError
from app.registry.file_store import (
    DuplicateDeploymentIdAcrossFilesError,
    LegacyDeploymentRegistryFileError,
    RegistryFileStore,
    RegistryMultiKindUpdateError,
    create_registry_files,
)
from app.schemas.registry import DeploymentConfig, RegistryConfig


def _ner_deployment(
    *,
    enabled: bool = True,
) -> DeploymentConfig:
    """파일 저장 테스트용 공통 HTTP NER 설정을 만듭니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": "ner",
            "adapterType": "http_ner",
            "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
            "timeoutMs": 5000,
            "enabled": enabled,
        }
    )


def _llm_deployment(
    *,
    enabled: bool = True,
) -> DeploymentConfig:
    """파일 저장 테스트용 Mock LLM 설정을 만듭니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": "llm",
            "adapterType": "mock",
            "enabled": enabled,
        }
    )


def _add_ner(registry: RegistryConfig) -> RegistryConfig:
    """기존 Registry에 ner-a를 추가한 새 설정을 반환합니다."""

    return RegistryConfig(
        deployments={
            **registry.deployments,
            "ner-a": _ner_deployment(),
        }
    )


def _add_llm(registry: RegistryConfig) -> RegistryConfig:
    """기존 Registry에 llm-a를 추가한 새 설정을 반환합니다."""

    return RegistryConfig(
        deployments={
            **registry.deployments,
            "llm-a": _llm_deployment(),
        }
    )


def _write_json(path: Path, value: object) -> None:
    """테스트 입력을 UTF-8 JSON으로 저장합니다."""

    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding="utf-8",
    )


def test_initialize_creates_two_kind_specific_files(
    tmp_path: Path,
) -> None:
    """초기화가 NER와 LLM 빈 Registry 파일만 생성합니다."""

    config_dir = tmp_path / "config"
    store = RegistryFileStore(config_dir)

    returned = store.initialize()

    assert returned == config_dir
    assert json.loads(
        store.ner_deployments_path.read_text(encoding="utf-8")
    ) == {}
    assert json.loads(
        store.llm_deployments_path.read_text(encoding="utf-8")
    ) == {}
    assert not store.legacy_deployments_path.exists()
    assert not (config_dir / "profiles.json").exists()
    assert store.load() == RegistryConfig(deployments={})


@pytest.mark.parametrize(
    "existing_filename",
    ["ner_deployments.json", "llm_deployments.json"],
)
def test_initialize_does_not_overwrite_or_complete_partial_files(
    tmp_path: Path,
    existing_filename: str,
) -> None:
    """종류별 파일 하나라도 있으면 기존 내용을 보존하고 아무것도 만들지 않습니다."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    existing_path = config_dir / existing_filename
    existing_path.write_text('{"existing": true}\n', encoding="utf-8")

    with pytest.raises(FileExistsError) as error_info:
        RegistryFileStore(config_dir).initialize()

    assert error_info.value.args == (existing_path,)
    assert existing_path.read_text(
        encoding="utf-8"
    ) == '{"existing": true}\n'
    other_filename = (
        "llm_deployments.json"
        if existing_filename == "ner_deployments.json"
        else "ner_deployments.json"
    )
    assert not (config_dir / other_filename).exists()


def test_initialize_rejects_legacy_single_file(
    tmp_path: Path,
) -> None:
    """이전 deployments.json을 조용히 무시하거나 옆에 새 파일을 만들지 않습니다."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    legacy_path = config_dir / "deployments.json"
    legacy_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(LegacyDeploymentRegistryFileError):
        RegistryFileStore(config_dir).initialize()

    assert legacy_path.read_text(encoding="utf-8") == "{}\n"
    assert not (config_dir / "ner_deployments.json").exists()
    assert not (config_dir / "llm_deployments.json").exists()


def test_initialize_removes_first_file_when_second_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """두 번째 파일 생성 실패 시 먼저 만든 NER 파일도 정리합니다."""

    config_dir = tmp_path / "config"
    original_open = Path.open

    def fail_llm_open(
        path: Path,
        *args: object,
        **kwargs: object,
    ):
        """LLM 파일을 생성하는 두 번째 open만 실패시킵니다."""

        if path.name == "llm_deployments.json":
            raise OSError("LLM 파일 생성 실패")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_llm_open)

    with pytest.raises(OSError, match="LLM 파일 생성 실패"):
        RegistryFileStore(config_dir).initialize()

    assert not (config_dir / "ner_deployments.json").exists()
    assert not (config_dir / "llm_deployments.json").exists()


def test_create_registry_files_uses_split_file_contract(
    tmp_path: Path,
) -> None:
    """편의 함수도 NER와 LLM Registry 파일 두 개를 초기화합니다."""

    config_dir = tmp_path / "config"

    assert create_registry_files(config_dir) == config_dir
    assert (config_dir / "ner_deployments.json").exists()
    assert (config_dir / "llm_deployments.json").exists()
    assert not (config_dir / "deployments.json").exists()


def test_load_merges_kind_specific_raw_maps(
    tmp_path: Path,
) -> None:
    """두 파일의 최상위 원시 ID 맵을 하나의 Registry로 조립합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    _write_json(
        store.ner_deployments_path,
        {
            "ner-a": {
                "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            }
        },
    )
    _write_json(
        store.llm_deployments_path,
        {
            "llm-a": {
                "adapterType": "mock",
                "enabled": False,
            }
        },
    )

    registry = store.load()

    assert registry.deployments == {
        "ner-a": _ner_deployment(),
        "llm-a": _llm_deployment(enabled=False),
    }


def test_load_rejects_removed_model_info(
    tmp_path: Path,
) -> None:
    """기존 파일의 modelInfo를 무시하지 않고 마이그레이션 대상으로 거부합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    _write_json(
        store.ner_deployments_path,
        {
            "ner-a": {
                "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "modelInfo": {
                    "displayName": "개인정보 NER",
                    "description": "테스트용 모델입니다.",
                },
                "enabled": True,
            }
        },
    )

    with pytest.raises(ValidationError):
        store.load()


@pytest.mark.parametrize("field_name", ["adapterConfig", "adapter_config"])
def test_load_rejects_removed_adapter_config(
    tmp_path: Path,
    field_name: str,
) -> None:
    """기존 파일의 Adapter 전용 설정을 무시하지 않고 마이그레이션 대상으로 거부합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    _write_json(
        store.ner_deployments_path,
        {
            "ner-a": {
                "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
                "timeoutMs": 5000,
                field_name: {},
                "enabled": True,
            }
        },
    )

    with pytest.raises(ValidationError):
        store.load()


@pytest.mark.parametrize(
    ("target_kind", "explicit_kind"),
    [
        ("ner", "ner"),
        ("ner", "llm"),
        ("llm", "llm"),
        ("llm", "ner"),
    ],
)
def test_load_rejects_explicit_kind_field(
    tmp_path: Path,
    target_kind: str,
    explicit_kind: str,
) -> None:
    """물리 JSON의 kind는 파일명과 일치하는 값도 허용하지 않습니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    path = (
        store.ner_deployments_path
        if target_kind == "ner"
        else store.llm_deployments_path
    )
    _write_json(
        path,
        {
            "explicit-kind": (
                {
                    "kind": explicit_kind,
                    "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
                    "timeoutMs": 5000,
                    "enabled": True,
                }
                if target_kind == "ner"
                else {
                    "kind": explicit_kind,
                    "adapterType": "mock",
                    "enabled": True,
                }
            )
        },
    )

    with pytest.raises(ValidationError, match="kind"):
        store.load()


def test_ner_file_rejects_legacy_adapter_selection(
    tmp_path: Path,
) -> None:
    """NER 파일은 이전 Adapter 선택 필드를 구조 검증에서 거부합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    _write_json(
        store.ner_deployments_path,
        {
            "misplaced-openai": {
                "adapterType": "openai_compatible",
                "baseUrl": "http://127.0.0.1:9000",
                "modelName": "model-a",
                "timeoutMs": 5000,
                "enabled": True,
            }
        },
    )

    with pytest.raises(ValidationError):
        store.load()


def test_load_rejects_same_id_across_kind_files(
    tmp_path: Path,
) -> None:
    """NER와 LLM 파일 사이에서도 Deployment ID의 전역 고유성을 지킵니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    _write_json(
        store.ner_deployments_path,
        {
            "shared-id": {
                "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
                "timeoutMs": 5000,
                "enabled": True,
            }
        },
    )
    _write_json(
        store.llm_deployments_path,
        {
            "shared-id": {
                "adapterType": "mock",
                "enabled": True,
            }
        },
    )

    with pytest.raises(
        DuplicateDeploymentIdAcrossFilesError
    ) as error_info:
        store.load()

    assert error_info.value.deployment_ids == ("shared-id",)


def test_load_rejects_legacy_file_even_when_split_files_exist(
    tmp_path: Path,
) -> None:
    """새 파일과 함께 남은 이전 파일도 설정 유실 위험 때문에 거부합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    store.legacy_deployments_path.write_text(
        "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(LegacyDeploymentRegistryFileError):
        store.load()


def test_load_rejects_invalid_json_without_rewriting_other_file(
    tmp_path: Path,
) -> None:
    """한 파일의 문법 오류를 자동 복구하지 않고 반대 종류 파일도 보존합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    invalid = b'{"ner-a": '
    llm_before = store.llm_deployments_path.read_bytes()
    store.ner_deployments_path.write_bytes(invalid)

    with pytest.raises(StrictJsonDecodeError):
        store.load()

    assert store.ner_deployments_path.read_bytes() == invalid
    assert store.llm_deployments_path.read_bytes() == llm_before


def test_load_rejects_duplicate_json_keys(
    tmp_path: Path,
) -> None:
    """한 종류 파일 안의 중복 ID를 마지막 값으로 덮어쓰지 않습니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    store.ner_deployments_path.write_text(
        (
            '{"ner-a":{"baseUrl":"http://127.0.0.1:8008/v1/ner/detect",'
            '"timeoutMs":5000,"enabled":true},'
            '"ner-a":{"baseUrl":"http://127.0.0.1:8008/v1/ner/detect",'
            '"timeoutMs":5000,"enabled":false}}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(StrictJsonDecodeError):
        store.load()


def test_load_requires_json_alias_names(
    tmp_path: Path,
) -> None:
    """파일에서 Python snake_case 필드를 사용하면 거부합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    _write_json(
        store.ner_deployments_path,
        {
            "ner-a": {
                "base_url": "http://127.0.0.1:8008/v1/ner/detect",
                "timeout_ms": 5000,
                "enabled": True,
            }
        },
    )

    with pytest.raises(ValidationError):
        store.load()


def test_ner_update_replaces_only_ner_file_atomically(
    tmp_path: Path,
) -> None:
    """NER 변경은 NER 파일만 교체하고 LLM 파일 bytes를 그대로 둡니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    store.update(_add_llm)
    llm_before = store.llm_deployments_path.read_bytes()

    updated = store.update(_add_ner)

    assert updated.deployments["ner-a"] == _ner_deployment()
    assert store.load() == updated
    assert store.llm_deployments_path.read_bytes() == llm_before
    stored = json.loads(
        store.ner_deployments_path.read_text(encoding="utf-8")
    )
    assert "kind" not in stored["ner-a"]
    assert "adapterType" not in stored["ner-a"]
    assert list(
        store.config_dir.glob(".ner_deployments.json.*.tmp")
    ) == []


def test_llm_update_replaces_only_llm_file_atomically(
    tmp_path: Path,
) -> None:
    """LLM 변경은 LLM 파일만 교체하고 NER 파일 bytes를 그대로 둡니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    store.update(_add_ner)
    ner_before = store.ner_deployments_path.read_bytes()

    updated = store.update(_add_llm)

    assert updated.deployments["llm-a"] == _llm_deployment()
    assert store.load() == updated
    assert store.ner_deployments_path.read_bytes() == ner_before
    stored = json.loads(
        store.llm_deployments_path.read_text(encoding="utf-8")
    )
    assert "kind" not in stored["llm-a"]
    assert list(
        store.config_dir.glob(".llm_deployments.json.*.tmp")
    ) == []


def test_multi_kind_update_is_rejected_without_partial_write(
    tmp_path: Path,
) -> None:
    """한 작업에서 두 종류를 바꾸면 두 파일을 모두 보존하고 거부합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    ner_before = store.ner_deployments_path.read_bytes()
    llm_before = store.llm_deployments_path.read_bytes()

    def add_both(registry: RegistryConfig) -> RegistryConfig:
        """NER와 LLM을 동시에 추가한 금지된 갱신 결과를 만듭니다."""

        return RegistryConfig(
            deployments={
                **registry.deployments,
                "ner-a": _ner_deployment(),
                "llm-a": _llm_deployment(),
            }
        )

    with pytest.raises(RegistryMultiKindUpdateError):
        store.update(add_both)

    assert store.ner_deployments_path.read_bytes() == ner_before
    assert store.llm_deployments_path.read_bytes() == llm_before
    assert store.load().deployments == {}


def test_no_change_update_does_not_replace_either_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """내용이 같은 갱신은 어느 종류 파일도 교체하지 않습니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    calls = 0

    def fail_if_called(*args: object) -> None:
        """원자 교체가 잘못 호출되면 횟수를 기록합니다."""

        nonlocal calls
        calls += 1

    monkeypatch.setattr(store, "_replace_atomically", fail_if_called)

    result = store.update(lambda registry: registry)

    assert result == RegistryConfig(deployments={})
    assert calls == 0


def test_updater_error_preserves_both_files(
    tmp_path: Path,
) -> None:
    """Updater가 실패하면 NER와 LLM 파일을 모두 그대로 유지합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    ner_before = store.ner_deployments_path.read_bytes()
    llm_before = store.llm_deployments_path.read_bytes()

    def fail(registry: RegistryConfig) -> RegistryConfig:
        """저장 전에 테스트 예외를 발생시킵니다."""

        del registry
        raise RuntimeError("변경 실패")

    with pytest.raises(RuntimeError, match="변경 실패"):
        store.update(fail)

    assert store.ner_deployments_path.read_bytes() == ner_before
    assert store.llm_deployments_path.read_bytes() == llm_before


def test_invalid_updated_registry_preserves_both_files(
    tmp_path: Path,
) -> None:
    """Backend 계약이 잘못된 갱신은 어느 파일에도 저장하지 않습니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    ner_before = store.ner_deployments_path.read_bytes()
    llm_before = store.llm_deployments_path.read_bytes()

    def invalid(registry: RegistryConfig) -> RegistryConfig:
        """등록되지 않은 Adapter를 사용하는 후보를 만듭니다."""

        del registry
        return RegistryConfig.model_validate(
            {
                "deployments": {
                    "unknown-a": {
                        "kind": "llm",
                        "adapterType": "unknown",
                        "enabled": True,
                    }
                }
            }
        )

    with pytest.raises(ValueError):
        store.update(invalid)

    assert store.ner_deployments_path.read_bytes() == ner_before
    assert store.llm_deployments_path.read_bytes() == llm_before


def test_atomic_replace_failure_preserves_both_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대상 파일 교체 실패 시 원본과 반대 종류 파일 및 임시 정리를 보존합니다."""

    store = RegistryFileStore(tmp_path / "config")
    store.initialize()
    ner_before = store.ner_deployments_path.read_bytes()
    llm_before = store.llm_deployments_path.read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        """OS 파일 교체 실패를 재현합니다."""

        del self, target
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.update(_add_ner)

    assert store.ner_deployments_path.read_bytes() == ner_before
    assert store.llm_deployments_path.read_bytes() == llm_before
    assert list(
        store.config_dir.glob(".ner_deployments.json.*.tmp")
    ) == []
