"""고정 Prompt Artifact의 단일 컴파일과 변수 계약을 검증합니다."""

from dataclasses import fields
from hashlib import sha256
from unittest.mock import patch

import pytest

from app.prompts.prompt_artifact import (
    FIXED_PROMPT_VARIABLES,
    PromptArtifact,
)
from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptTemplateSyntaxError,
    PromptTemplateTooLargeError,
    PromptVariableContractError,
)
from app.prompts.prompt_limits import PromptLimits
from app.prompts.policy_prompt_catalog import PolicyPromptCatalog
from app.prompts.prompt_renderer import PromptRenderer
from app.schemas.detection import ALLOWED_POLICY_IDS


_TEMPLATE_PATH = "config/prompts.j2"
_VALID_SOURCE = "{{ text }} / {{ existing_detections }}"


def _compile(
    source: object = _VALID_SOURCE,
    *,
    renderer: PromptRenderer | None = None,
) -> PromptArtifact:
    """테스트용 고정 Prompt를 실제 API로 컴파일합니다."""

    return PromptArtifact.compile(
        source,
        renderer=renderer or PromptRenderer(),
        template_path=_TEMPLATE_PATH,
    )


def test_compile_builds_fixed_artifact_with_sha256() -> None:
    """정확한 외부 변수와 본문 해시를 가진 Artifact를 만듭니다."""

    artifact = _compile()

    assert artifact.compiled_template.referenced_variables == (
        FIXED_PROMPT_VARIABLES
    )
    assert artifact.content_hash == sha256(
        _VALID_SOURCE.encode("utf-8")
    ).hexdigest()


def test_compile_invokes_renderer_once_and_render_reuses_handle() -> None:
    """검증, Artifact 생성과 렌더링 사이에 재컴파일하지 않습니다."""

    renderer = PromptRenderer()

    with patch.object(
        renderer,
        "compile_template",
        wraps=renderer.compile_template,
    ) as compile_template:
        artifact = _compile(renderer=renderer)
        first = artifact.render(
            text="홍길동",
            existing_detections="[]",
        )
        second = artifact.render(
            text="프로젝트 알파",
            existing_detections='[{"type":"PERSONAL_IDENTITY"}]',
        )

    assert compile_template.call_count == 1
    assert first == "홍길동 / []"
    assert second == (
        '프로젝트 알파 / [{"type":"PERSONAL_IDENTITY"}]'
    )


def test_render_appends_only_selected_policy_instructions() -> None:
    """기본 Prompt 뒤에 선택된 정책 블록만 고정 순서로 조립합니다."""

    renderer = PromptRenderer()
    catalog = PolicyPromptCatalog.compile(
        {
            policy_id: f"RULE_{policy_id}"
            for policy_id in ALLOWED_POLICY_IDS
        },
        source_path="<test-policy-prompts>",
        max_output_bytes=renderer.limits.max_output_bytes,
    )
    artifact = PromptArtifact.compile(
        _VALID_SOURCE,
        renderer=renderer,
        template_path=_TEMPLATE_PATH,
        policy_prompts=catalog,
    )

    rendered = artifact.render(
        text="홍길동",
        existing_detections="{}",
        policy_ids=("P06", "B01"),
    )

    assert rendered.startswith("홍길동 / {}\n\n<policy_instructions>")
    assert "[Policy P06]\nRULE_P06" in rendered
    assert "[Policy B01]\nRULE_B01" in rendered
    assert "[Policy P01]" not in rendered


@pytest.mark.parametrize(
    "content",
    ["", "   \n", "\ud800", None, b"template"],
)
def test_compile_rejects_invalid_template_content(
    content: object,
) -> None:
    """비어 있거나 UTF-8 문자열이 아닌 고정 본문을 거부합니다."""

    with pytest.raises(
        InvalidPromptTemplateContentError
    ) as error_info:
        _compile(content)

    assert error_info.value.template_path == _TEMPLATE_PATH


@pytest.mark.parametrize(
    ("source", "missing", "unexpected"),
    [
        (
            "{{ text }}",
            frozenset({"existing_detections"}),
            frozenset(),
        ),
        (
            "{{ existing_detections }}",
            frozenset({"text"}),
            frozenset(),
        ),
        (
            "{{ text }} {{ existing_detections }} {{ language }}",
            frozenset(),
            frozenset({"language"}),
        ),
        (
            "고정 문자열",
            FIXED_PROMPT_VARIABLES,
            frozenset(),
        ),
    ],
)
def test_compile_requires_exact_external_variables(
    source: str,
    missing: frozenset[str],
    unexpected: frozenset[str],
) -> None:
    """외부 변수 집합을 text와 existing_detections로 정확히 고정합니다."""

    with pytest.raises(PromptVariableContractError) as error_info:
        _compile(source)

    error = error_info.value
    assert error.template_path == _TEMPLATE_PATH
    assert error.required_variables == FIXED_PROMPT_VARIABLES
    assert error.missing_variables == missing
    assert error.unexpected_variables == unexpected


def test_compile_ignores_jinja_local_variables() -> None:
    """Jinja 내부 지역 변수는 외부 Context 계약에 포함하지 않습니다."""

    artifact = _compile(
        "{% set label = '탐지' %}"
        "{{ label }}: {{ text }} / {{ existing_detections }}"
    )

    assert artifact.compiled_template.referenced_variables == (
        FIXED_PROMPT_VARIABLES
    )


def test_compile_preserves_template_syntax_line() -> None:
    """고정 Prompt의 문법 오류 줄 번호를 reload 진단에 보존합니다."""

    with pytest.raises(PromptTemplateSyntaxError) as error_info:
        _compile(
            "{{ existing_detections }}\n"
            "{% if text %}"
        )

    assert error_info.value.line_number == 2


def test_compile_enforces_utf8_template_byte_limit() -> None:
    """Artifact 경계에서도 본문 크기를 UTF-8 byte로 제한합니다."""

    renderer = PromptRenderer(
        limits=PromptLimits(max_template_bytes=6)
    )

    with pytest.raises(PromptTemplateTooLargeError) as error_info:
        _compile(
            "가나다{{ text }}{{ existing_detections }}",
            renderer=renderer,
        )

    assert error_info.value.template_path == _TEMPLATE_PATH
    assert error_info.value.actual_bytes > 6
    assert error_info.value.max_bytes == 6


def test_artifact_does_not_store_prompt_source() -> None:
    """활성 Snapshot의 Artifact에는 원본 Prompt 본문 필드를 남기지 않습니다."""

    marker = "SENSITIVE_PROMPT_MARKER"
    source = (
        f"{marker}: {{{{ text }}}} / "
        "{{ existing_detections }}"
    )

    artifact = _compile(source)

    assert {field.name for field in fields(PromptArtifact)} == {
        "compiled_template",
        "content_hash",
        "policy_prompts",
    }
    assert not hasattr(artifact, "source")
    assert marker not in repr(artifact)


def test_artifact_constructor_rechecks_variable_contract() -> None:
    """수동 생성으로 고정 변수 검증을 우회하지 못합니다."""

    compiled = PromptRenderer().compile_template("{{ text }}")

    with pytest.raises(ValueError, match="text와 existing_detections"):
        PromptArtifact(
            compiled_template=compiled,
            content_hash="0" * 64,
            policy_prompts=PolicyPromptCatalog.empty(
                max_output_bytes=1024,
            ),
        )


@pytest.mark.parametrize("content_hash", ["", "ABC", "g" * 64, "0" * 63])
def test_artifact_rejects_invalid_content_hash(
    content_hash: str,
) -> None:
    """Snapshot 식별에 사용할 해시는 정확한 SHA-256 형식이어야 합니다."""

    compiled = PromptRenderer().compile_template(_VALID_SOURCE)

    with pytest.raises(ValueError, match="SHA-256"):
        PromptArtifact(
            compiled_template=compiled,
            content_hash=content_hash,
            policy_prompts=PolicyPromptCatalog.empty(
                max_output_bytes=1024,
            ),
        )
