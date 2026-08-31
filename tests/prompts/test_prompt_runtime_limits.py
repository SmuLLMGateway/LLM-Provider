"""고정 Prompt 컴파일과 렌더링의 자원 제한을 검증합니다."""

import json

import pytest

from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_errors import (
    InvalidPromptContextError,
    PromptContextTooLargeError,
    PromptOutputTooLargeError,
    PromptRenderTimeoutError,
    PromptTemplateTooLargeError,
)
from app.prompts.prompt_limits import PromptLimits
from app.prompts.prompt_renderer import PromptRenderer


_LIMIT_FIELDS = (
    "max_template_bytes",
    "max_context_bytes",
    "max_output_bytes",
    "render_timeout_ms",
)
_SOURCE = "{{ text }}{{ existing_detections }}"


@pytest.mark.parametrize("field_name", _LIMIT_FIELDS)
@pytest.mark.parametrize("invalid_value", [0, -1, True, 1.5])
def test_prompt_limits_reject_invalid_values(
    field_name: str,
    invalid_value: object,
) -> None:
    """모든 제한값은 1 이상의 실제 정수여야 합니다."""

    with pytest.raises(ValueError, match=field_name):
        PromptLimits(**{field_name: invalid_value})


def test_prompt_limits_accept_minimum_values() -> None:
    """남은 네 제한값의 최소 허용값인 1을 받아들입니다."""

    limits = PromptLimits(
        max_template_bytes=1,
        max_context_bytes=1,
        max_output_bytes=1,
        render_timeout_ms=1,
    )

    assert all(
        getattr(limits, field_name) == 1
        for field_name in _LIMIT_FIELDS
    )


def test_renderer_enforces_template_byte_limit() -> None:
    """컴파일 전에 본문 UTF-8 byte 크기를 제한합니다."""

    renderer = PromptRenderer(
        limits=PromptLimits(max_template_bytes=5)
    )

    with pytest.raises(PromptTemplateTooLargeError) as error_info:
        renderer.compile_template("한글")

    assert error_info.value.actual_bytes == 6
    assert error_info.value.max_bytes == 5


def test_renderer_rejects_aggregate_context_byte_limit() -> None:
    """두 문자열을 담은 전체 JSON Context의 UTF-8 크기를 제한합니다."""

    text = "가"
    existing_detections = "[]"
    actual_bytes = len(
        json.dumps(
            {
                "text": text,
                "existing_detections": existing_detections,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    renderer = PromptRenderer(
        limits=PromptLimits(max_context_bytes=actual_bytes - 1)
    )
    compiled = renderer.compile_template(_SOURCE)

    with pytest.raises(PromptContextTooLargeError) as error_info:
        compiled.render(
            text=text,
            existing_detections=existing_detections,
        )

    assert error_info.value.actual_bytes == actual_bytes
    assert error_info.value.max_bytes == actual_bytes - 1


def test_renderer_rejects_context_string_that_is_not_utf8() -> None:
    """UTF-8 JSON으로 만들 수 없는 Context 문자열을 거부합니다."""

    compiled = PromptRenderer().compile_template(_SOURCE)

    with pytest.raises(InvalidPromptContextError):
        compiled.render(
            text="\ud800",
            existing_detections="[]",
        )


def test_renderer_rejects_utf8_output_byte_limit() -> None:
    """최종 출력 크기를 문자 수가 아닌 UTF-8 byte로 제한합니다."""

    renderer = PromptRenderer(
        limits=PromptLimits(max_output_bytes=5)
    )
    compiled = renderer.compile_template(_SOURCE)

    with pytest.raises(PromptOutputTooLargeError) as error_info:
        compiled.render(
            text="한글",
            existing_detections="",
        )

    assert error_info.value.actual_bytes == 6
    assert error_info.value.max_bytes == 5


def test_renderer_enforces_cooperative_deadline() -> None:
    """실제 대기 없이 chunk 사이의 협력적 deadline을 검증합니다."""

    timestamps = iter([0.0, 0.0, 0.0, 0.002])
    observed: list[float] = []

    def fake_clock() -> float:
        value = next(timestamps)
        observed.append(value)
        return value

    renderer = PromptRenderer(
        limits=PromptLimits(render_timeout_ms=1),
        clock=fake_clock,
    )
    compiled = renderer.compile_template(_SOURCE)

    with pytest.raises(PromptRenderTimeoutError) as error_info:
        compiled.render(text="", existing_detections="")

    assert error_info.value.timeout_ms == 1
    assert observed == [0.0, 0.0, 0.0, 0.002]


def test_artifact_preserves_compile_time_limits() -> None:
    """Artifact 렌더링이 컴파일 당시 Renderer 제한을 계속 사용합니다."""

    renderer = PromptRenderer(
        limits=PromptLimits(max_output_bytes=3)
    )
    artifact = PromptArtifact.compile(
        _SOURCE,
        renderer=renderer,
        template_path="config/prompts.j2",
    )

    assert artifact.render(
        text="한",
        existing_detections="",
    ) == "한"
    with pytest.raises(PromptOutputTooLargeError):
        artifact.render(
            text="한글",
            existing_detections="",
        )
