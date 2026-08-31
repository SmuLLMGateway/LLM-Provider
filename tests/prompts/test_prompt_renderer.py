"""고정 문자열 Context를 사용하는 Jinja Renderer를 검증합니다."""

from dataclasses import FrozenInstanceError

import pytest

from app.prompts.prompt_errors import (
    InvalidPromptContextError,
    MissingPromptVariableError,
    PromptTemplateRenderError,
    PromptTemplateSyntaxError,
)
from app.prompts.prompt_renderer import (
    CompiledPromptTemplate,
    PromptRenderer,
)


def _render(
    source: str,
    *,
    text: object = "홍길동",
    existing_detections: object = "[]",
) -> str:
    """본문을 컴파일하고 고정 두 Context 값으로 렌더링합니다."""

    return PromptRenderer().compile_template(source).render(
        text=text,  # type: ignore[arg-type]
        existing_detections=existing_detections,  # type: ignore[arg-type]
    )


def test_renderer_exposes_only_compile_entrypoint() -> None:
    """Renderer에서 문자열 직접 렌더링 우회 API를 제공하지 않습니다."""

    renderer = PromptRenderer()

    assert callable(renderer.compile_template)
    assert not hasattr(renderer, "render")
    assert not hasattr(renderer, "render_compiled")


def test_renders_korean_fixed_context() -> None:
    """한글 원문과 직렬화된 기존 탐지 문자열을 올바르게 렌더링합니다."""

    rendered = _render(
        "원문:\n{{ text }}\n기존:\n{{ existing_detections }}",
        text="홍길동은 프로젝트 알파를 담당합니다.",
        existing_detections='[{"type":"PERSONAL_IDENTITY","text":"홍길동"}]',
    )

    assert rendered == (
        "원문:\n홍길동은 프로젝트 알파를 담당합니다.\n"
        '기존:\n[{"type":"PERSONAL_IDENTITY","text":"홍길동"}]'
    )


def test_preserves_trailing_newline() -> None:
    """렌더링 결과의 마지막 줄바꿈을 보존합니다."""

    assert _render(
        "{{ text }} / {{ existing_detections }}\n"
    ) == "홍길동 / []\n"


def test_does_not_evaluate_jinja_inside_context_string() -> None:
    """사용자 문자열에 포함된 Jinja 문법을 다시 실행하지 않습니다."""

    rendered = _render(
        "{{ text }} / {{ existing_detections }}",
        text="{{ secret }}",
        existing_detections='["{{ 1 / 0 }}"]',
    )

    assert rendered == '{{ secret }} / ["{{ 1 / 0 }}"]'


def test_compiled_handle_hides_jinja_environment() -> None:
    """컴파일 Handle이 Jinja 환경과 global 변경 표면을 노출하지 않습니다."""

    compiled = PromptRenderer().compile_template(
        "{{ text }} / {{ existing_detections }}"
    )

    assert isinstance(compiled, CompiledPromptTemplate)
    assert not hasattr(compiled, "environment")
    assert not hasattr(compiled, "globals")
    with pytest.raises(FrozenInstanceError):
        compiled.referenced_variables = frozenset()  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        compiled.environment = object()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("text", ["원문"]),
        ("existing_detections", {"items": []}),
        ("text", object()),
    ],
)
def test_rejects_non_string_context(
    field_name: str,
    value: object,
) -> None:
    """두 고정 Context에 임의 객체나 가변 컨테이너를 허용하지 않습니다."""

    values = {
        "text": "원문",
        "existing_detections": "[]",
    }
    values[field_name] = value

    with pytest.raises(InvalidPromptContextError, match="문자열"):
        _render(
            "{{ text }} / {{ existing_detections }}",
            text=values["text"],
            existing_detections=values["existing_detections"],
        )


def test_rejects_callable_without_executing_it() -> None:
    """Context의 callable을 실행하지 않고 문자열 경계에서 거부합니다."""

    called = False

    def callback() -> str:
        nonlocal called
        called = True
        return "실행됨"

    with pytest.raises(InvalidPromptContextError):
        _render(
            "{{ text }} / {{ existing_detections }}",
            text=callback,
        )

    assert called is False


def test_rejects_non_utf8_context_string() -> None:
    """JSON UTF-8로 직렬화할 수 없는 문자열을 Context에서 거부합니다."""

    with pytest.raises(InvalidPromptContextError):
        _render(
            "{{ text }} / {{ existing_detections }}",
            text="\ud800",
        )


def test_rejects_missing_template_variable() -> None:
    """Renderer 단위에서도 정의되지 않은 외부 변수 접근을 거부합니다."""

    with pytest.raises(MissingPromptVariableError, match="language"):
        _render(
            "{{ text }} / {{ existing_detections }} / {{ language }}"
        )


def test_rejects_invalid_template_syntax() -> None:
    """잘못된 Jinja 문법과 줄 번호를 보고합니다."""

    with pytest.raises(PromptTemplateSyntaxError) as error_info:
        PromptRenderer().compile_template(
            "{{ existing_detections }}\n{% if text %}"
        )

    assert error_info.value.line_number == 2


def test_wraps_runtime_render_error() -> None:
    """렌더링 실행 오류를 공통 Prompt 오류로 변환합니다."""

    with pytest.raises(PromptTemplateRenderError):
        _render(
            "{{ text }} {{ existing_detections }} {{ 1 / 0 }}"
        )


def test_sandbox_blocks_unsafe_attribute_access() -> None:
    """Sandbox가 문자열의 안전하지 않은 속성 접근을 차단합니다."""

    with pytest.raises(PromptTemplateRenderError):
        _render(
            "{{ text }} {{ existing_detections }} {{ text.__class__ }}"
        )
