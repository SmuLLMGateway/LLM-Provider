"""비신뢰 LLM 제목 출력의 정규화와 거부 규칙을 검증합니다."""

from __future__ import annotations

import pytest

from app.backends.errors import BackendResponseError
from app.services.title_output_validator import (
    DEFAULT_MAX_TITLE_BYTES,
    DEFAULT_MAX_TITLE_CHARACTERS,
    TitleOutputValidationError,
    TitleOutputValidator,
)


class CustomString(str):
    """정확한 내장 문자열 타입 검증에 사용할 하위 타입입니다."""


def _assert_invalid(
    output: object,
    *,
    validator: TitleOutputValidator | None = None,
) -> TitleOutputValidationError:
    """제목 출력을 거부하고 안정적인 공통 오류 코드를 내는지 확인합니다."""

    with pytest.raises(TitleOutputValidationError) as error_info:
        (validator or TitleOutputValidator()).validate(output)

    error = error_info.value
    assert isinstance(error, BackendResponseError)
    assert error.code == "TITLE_OUTPUT_INVALID"
    assert type(error.detail) is str
    assert error.detail
    return error


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("FastAPI 제목 생성", "FastAPI 제목 생성"),
        ("  LLM 서버 라우팅  ", "LLM 서버 라우팅"),
        ("\t새 대화\r\n", "새 대화"),
        ("Privacy-Preserving Gateway", "Privacy-Preserving Gateway"),
    ],
)
def test_validate_returns_trimmed_plain_text_title(
    output: str,
    expected: str,
) -> None:
    """유효한 한 줄 제목의 바깥 공백만 제거해 반환합니다."""

    assert TitleOutputValidator().validate(output) == expected


@pytest.mark.parametrize(
    "output",
    [None, b"title", 1, True, [], {}, CustomString("제목")],
)
def test_validate_rejects_non_exact_string(output: object) -> None:
    """문자열 하위 타입을 포함해 정확한 내장 str 이외의 값을 거부합니다."""

    _assert_invalid(output)


@pytest.mark.parametrize("output", ["", " ", "\n\r\t"])
def test_validate_rejects_empty_title_after_trimming(output: str) -> None:
    """바깥 공백을 제거한 결과가 비어 있으면 제목으로 허용하지 않습니다."""

    _assert_invalid(output)


def test_default_limits_are_applied() -> None:
    """기본 제목 제한값이 공개 상수와 동일하게 적용됩니다."""

    validator = TitleOutputValidator()

    assert validator.max_characters == DEFAULT_MAX_TITLE_CHARACTERS
    assert validator.max_bytes == DEFAULT_MAX_TITLE_BYTES


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("max_characters", 0),
        ("max_characters", -1),
        ("max_characters", True),
        ("max_characters", 1.0),
        ("max_characters", "30"),
        ("max_bytes", 0),
        ("max_bytes", -1),
        ("max_bytes", True),
        ("max_bytes", 1.0),
        ("max_bytes", "120"),
    ],
)
def test_constructor_rejects_invalid_limits(
    field_name: str,
    value: object,
) -> None:
    """제한값에는 1 이상의 정확한 정수만 허용합니다."""

    arguments: dict[str, object] = {field_name: value}

    with pytest.raises(ValueError, match=field_name):
        TitleOutputValidator(**arguments)  # type: ignore[arg-type]


def test_validate_enforces_character_boundary() -> None:
    """문자 수 상한과 정확히 같은 제목은 허용하고 초과하면 거부합니다."""

    validator = TitleOutputValidator(
        max_characters=4,
        max_bytes=100,
    )

    assert validator.validate("가나다라") == "가나다라"
    _assert_invalid("가나다라마", validator=validator)


def test_validate_enforces_utf8_byte_boundary() -> None:
    """문자 수와 별개로 UTF-8 byte 수 상한을 적용합니다."""

    validator = TitleOutputValidator(
        max_characters=10,
        max_bytes=6,
    )

    assert validator.validate("가나") == "가나"
    _assert_invalid("가나다", validator=validator)


def test_validate_rejects_non_utf8_title() -> None:
    """UTF-8로 표현할 수 없는 서로게이트 문자를 거부합니다."""

    _assert_invalid("\ud800")


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r", "\u2028", "\u2029"],
)
def test_validate_rejects_every_line_separator(separator: str) -> None:
    """일반 개행과 Unicode 줄 구분자가 포함된 다중 행 출력을 거부합니다."""

    _assert_invalid(f"첫 제목{separator}둘째 제목")


@pytest.mark.parametrize(
    "control_character",
    ["\x00", "\t", "\u200b"],
)
def test_validate_rejects_control_and_format_characters(
    control_character: str,
) -> None:
    """제목 내부의 제어 문자와 보이지 않는 형식 문자를 거부합니다."""

    _assert_invalid(f"제목{control_character}생성")


@pytest.mark.parametrize(
    "output",
    [
        '"제목"',
        "'제목'",
        "`제목`",
        "# 제목",
        '{"title":"제목"}',
        '["제목"]',
        "제목}",
        "제목]",
    ],
)
def test_validate_rejects_quotes_markdown_and_structured_wrappers(
    output: str,
) -> None:
    """따옴표, Markdown과 JSON 형태로 감싼 모델 출력을 거부합니다."""

    _assert_invalid(output)


@pytest.mark.parametrize(
    "output",
    [
        "title: generated title",
        "Title: Generated Title",
        "TITLE: GENERATED TITLE",
        "제목: 생성된 제목",
    ],
)
def test_validate_rejects_explanatory_prefix(output: str) -> None:
    """모델이 붙인 제목 설명 접두사를 대소문자와 무관하게 거부합니다."""

    _assert_invalid(output)


def test_validate_allows_colon_inside_normal_title() -> None:
    """금지 접두사가 아니라면 일반 제목 안의 콜론은 보존합니다."""

    title = "FastAPI: 제목 생성"

    assert TitleOutputValidator().validate(title) == title


def test_validation_error_does_not_retain_model_output() -> None:
    """오류 객체의 공개 내용과 문자열에 비신뢰 모델 출력을 남기지 않습니다."""

    marker = "SENSITIVE_TITLE_OUTPUT_7f4d2c"
    output = f"{marker}\n둘째 줄"

    error = _assert_invalid(output)

    assert marker not in str(error)
    assert marker not in repr(error)
    assert marker not in error.detail
    assert not hasattr(error, "output")
