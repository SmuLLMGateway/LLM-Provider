"""마스킹용 고정 System Prompt Artifact 계약을 검증합니다."""

from dataclasses import fields
from hashlib import sha256
from unittest.mock import patch

import pytest

from app.prompts.mask_prompt_artifact import (
    MASK_PROMPT_VARIABLES,
    MaskPromptArtifact,
)
from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptTemplateSyntaxError,
    PromptTemplateTooLargeError,
    PromptVariableContractError,
)
from app.prompts.prompt_limits import PromptLimits
from app.prompts.prompt_renderer import PromptRenderer


_TEMPLATE_PATH = "config/mask_prompt.j2"
_VALID_SOURCE = "모든 target을 정확히 한 번 마스킹하십시오."


def _compile(
    source: object = _VALID_SOURCE,
    *,
    renderer: PromptRenderer | None = None,
) -> MaskPromptArtifact:
    """공개 compile API로 테스트용 마스킹 Artifact를 만듭니다."""

    return MaskPromptArtifact.compile(
        source,
        renderer=renderer or PromptRenderer(),
        template_path=_TEMPLATE_PATH,
    )


def test_compile_builds_static_artifact_with_sha256() -> None:
    """외부 변수가 없고 본문 해시가 정확한 Artifact를 만듭니다."""

    artifact = _compile()

    assert MASK_PROMPT_VARIABLES == frozenset()
    assert artifact.compiled_template.referenced_variables == frozenset()
    assert artifact.content_hash == sha256(
        _VALID_SOURCE.encode("utf-8")
    ).hexdigest()


def test_compile_once_and_render_reuses_compiled_handle() -> None:
    """반복 render가 Prompt를 다시 컴파일하지 않습니다."""

    renderer = PromptRenderer()
    with patch.object(
        renderer,
        "compile_template",
        wraps=renderer.compile_template,
    ) as compile_template:
        artifact = _compile(renderer=renderer)
        first = artifact.render()
        second = artifact.render()

    assert compile_template.call_count == 1
    assert first == _VALID_SOURCE
    assert second == _VALID_SOURCE


@pytest.mark.parametrize(
    "content",
    ["", " \r\n\t", "\ud800", None, b"prompt"],
)
def test_compile_rejects_invalid_content(content: object) -> None:
    """비어 있거나 UTF-8 문자열이 아닌 Prompt를 거부합니다."""

    with pytest.raises(InvalidPromptTemplateContentError) as error_info:
        _compile(content)

    assert error_info.value.template_path == _TEMPLATE_PATH


@pytest.mark.parametrize(
    "source",
    [
        "{{ text }}",
        "{{ targets }}",
        "{{ placeholderNamespace }}",
        "{{ existing_detections }}",
        "고정 지시 {{ user_input }}",
    ],
)
def test_compile_rejects_every_external_variable(source: str) -> None:
    """사용자 원문과 target을 System Prompt에 렌더링하지 못하게 합니다."""

    with pytest.raises(PromptVariableContractError) as error_info:
        _compile(source)

    error = error_info.value
    assert error.template_path == _TEMPLATE_PATH
    assert error.required_variables == MASK_PROMPT_VARIABLES
    assert error.missing_variables == frozenset()
    assert error.unexpected_variables


def test_compile_allows_jinja_local_values_only() -> None:
    """외부 Context가 아닌 템플릿 내부 상수는 허용합니다."""

    artifact = _compile(
        "{% set instruction = '마스킹' %}{{ instruction }}"
    )

    assert artifact.render() == "마스킹"
    assert artifact.compiled_template.referenced_variables == frozenset()


def test_compile_preserves_syntax_error_line() -> None:
    """Jinja 문법 오류의 줄 번호를 보존합니다."""

    with pytest.raises(PromptTemplateSyntaxError) as error_info:
        _compile("첫 줄\n{% if true %}")

    assert error_info.value.line_number == 2


def test_compile_enforces_utf8_byte_limit() -> None:
    """문자 수가 아니라 UTF-8 byte 수로 Prompt 상한을 적용합니다."""

    renderer = PromptRenderer(
        limits=PromptLimits(max_template_bytes=6)
    )
    with pytest.raises(PromptTemplateTooLargeError) as error_info:
        _compile("가나다", renderer=renderer)

    assert error_info.value.actual_bytes == 9
    assert error_info.value.max_bytes == 6


def test_artifact_does_not_store_or_repr_prompt_source() -> None:
    """Snapshot Artifact에 별도 Prompt 원문 필드를 남기지 않습니다."""

    marker = "SENSITIVE_MASK_PROMPT_MARKER"
    artifact = _compile(f"{marker}: 마스킹하십시오.")

    assert {field.name for field in fields(MaskPromptArtifact)} == {
        "compiled_template",
        "content_hash",
    }
    assert not hasattr(artifact, "source")
    assert marker not in repr(artifact)


def test_constructor_rechecks_static_variable_contract() -> None:
    """수동 생성으로 무변수 System Prompt 계약을 우회하지 못합니다."""

    compiled = PromptRenderer().compile_template("{{ text }}")

    with pytest.raises(ValueError, match="외부 변수"):
        MaskPromptArtifact(
            compiled_template=compiled,
            content_hash="0" * 64,
        )


@pytest.mark.parametrize(
    "content_hash",
    ["", "ABC", "g" * 64, "0" * 63, "0" * 65],
)
def test_constructor_rejects_invalid_content_hash(
    content_hash: str,
) -> None:
    """해시는 정확한 SHA-256 소문자 16진수여야 합니다."""

    compiled = PromptRenderer().compile_template(_VALID_SOURCE)

    with pytest.raises(ValueError, match="SHA-256"):
        MaskPromptArtifact(
            compiled_template=compiled,
            content_hash=content_hash,
        )

