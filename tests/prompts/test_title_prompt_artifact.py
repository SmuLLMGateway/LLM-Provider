"""제목 생성용 고정 Prompt Artifact의 계약을 검증합니다."""

from dataclasses import fields
from hashlib import sha256
from unittest.mock import patch

import pytest

from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptTemplateSyntaxError,
    PromptTemplateTooLargeError,
    PromptVariableContractError,
)
from app.prompts.prompt_limits import PromptLimits
from app.prompts.prompt_renderer import PromptRenderer
from app.prompts.title_prompt_artifact import (
    TITLE_PROMPT_VARIABLES,
    TitlePromptArtifact,
)


_TEMPLATE_PATH = "config/title_prompt.j2"
_VALID_SOURCE = "대화의 핵심 주제를 짧은 제목으로 출력하세요."


def _compile(
    source: object = _VALID_SOURCE,
    *,
    renderer: PromptRenderer | None = None,
) -> TitlePromptArtifact:
    """테스트용 제목 Prompt를 실제 공개 API로 컴파일합니다."""

    return TitlePromptArtifact.compile(
        source,
        renderer=renderer or PromptRenderer(),
        template_path=_TEMPLATE_PATH,
    )


def test_compile_builds_static_artifact_with_sha256() -> None:
    """외부 변수가 없고 본문 해시가 정확한 Artifact를 만듭니다."""

    artifact = _compile()

    assert artifact.compiled_template.referenced_variables == (
        TITLE_PROMPT_VARIABLES
    )
    assert TITLE_PROMPT_VARIABLES == frozenset()
    assert artifact.content_hash == sha256(
        _VALID_SOURCE.encode("utf-8")
    ).hexdigest()


def test_compile_invokes_renderer_once_and_render_reuses_handle() -> None:
    """Artifact를 여러 번 렌더링해도 템플릿을 다시 컴파일하지 않습니다."""

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
    ["", "   \n", "\ud800", None, b"template"],
)
def test_compile_rejects_invalid_template_content(
    content: object,
) -> None:
    """비어 있거나 UTF-8 문자열이 아닌 제목 Prompt 본문을 거부합니다."""

    with pytest.raises(
        InvalidPromptTemplateContentError
    ) as error_info:
        _compile(content)

    assert error_info.value.template_path == _TEMPLATE_PATH


@pytest.mark.parametrize(
    "source",
    [
        "{{ text }}",
        "{{ existing_detections }}",
        "{{ conversation }}",
        "고정 지시문 {{ language }}",
    ],
)
def test_compile_rejects_every_external_variable(source: str) -> None:
    """사용자 입력을 정적 System Prompt에 삽입할 수 없도록 막습니다."""

    with pytest.raises(PromptVariableContractError) as error_info:
        _compile(source)

    error = error_info.value
    assert error.template_path == _TEMPLATE_PATH
    assert error.required_variables == TITLE_PROMPT_VARIABLES
    assert error.missing_variables == frozenset()
    assert error.unexpected_variables


def test_compile_allows_jinja_local_variables() -> None:
    """Jinja 내부 지역 변수는 외부 Context 계약으로 보지 않습니다."""

    artifact = _compile(
        "{% set fallback = '새 대화' %}{{ fallback }}"
    )

    assert artifact.compiled_template.referenced_variables == frozenset()
    assert artifact.render() == "새 대화"


def test_compile_preserves_template_syntax_line() -> None:
    """제목 Prompt 문법 오류가 발생한 줄 번호를 보존합니다."""

    with pytest.raises(PromptTemplateSyntaxError) as error_info:
        _compile(
            "제목 생성 규칙\n"
            "{% if true %}"
        )

    assert error_info.value.line_number == 2


def test_compile_enforces_utf8_template_byte_limit() -> None:
    """Artifact 경계에서도 본문 크기를 UTF-8 byte로 제한합니다."""

    renderer = PromptRenderer(
        limits=PromptLimits(max_template_bytes=6)
    )

    with pytest.raises(PromptTemplateTooLargeError) as error_info:
        _compile("가나다", renderer=renderer)

    assert error_info.value.template_path == _TEMPLATE_PATH
    assert error_info.value.actual_bytes == 9
    assert error_info.value.max_bytes == 6


def test_artifact_does_not_store_prompt_source() -> None:
    """활성 Snapshot용 Artifact에 Prompt 원문을 별도 필드로 남기지 않습니다."""

    marker = "SENSITIVE_TITLE_PROMPT_MARKER"
    artifact = _compile(f"{marker}: 제목만 출력하세요.")

    assert {field.name for field in fields(TitlePromptArtifact)} == {
        "compiled_template",
        "content_hash",
    }
    assert not hasattr(artifact, "source")
    assert marker not in repr(artifact)


def test_constructor_rechecks_external_variable_contract() -> None:
    """수동 생성으로 제목 Prompt의 무변수 계약을 우회하지 못합니다."""

    compiled = PromptRenderer().compile_template("{{ text }}")

    with pytest.raises(ValueError, match="외부 변수"):
        TitlePromptArtifact(
            compiled_template=compiled,
            content_hash="0" * 64,
        )


@pytest.mark.parametrize(
    "content_hash",
    ["", "ABC", "g" * 64, "0" * 63],
)
def test_artifact_rejects_invalid_content_hash(
    content_hash: str,
) -> None:
    """본문 해시는 정확한 SHA-256 소문자 16진수 형식이어야 합니다."""

    compiled = PromptRenderer().compile_template(_VALID_SOURCE)

    with pytest.raises(ValueError, match="SHA-256"):
        TitlePromptArtifact(
            compiled_template=compiled,
            content_hash=content_hash,
        )
