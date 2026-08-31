"""비신뢰 LLM 제목 출력을 짧은 단일 제목으로 검증합니다."""

from __future__ import annotations

import unicodedata

from app.backends.errors import BackendResponseError


DEFAULT_MAX_TITLE_CHARACTERS = 30
DEFAULT_MAX_TITLE_BYTES = 120
_FORBIDDEN_PREFIXES = (
    "\"",
    "'",
    "`",
    "#",
    "{",
    "[",
)
_FORBIDDEN_SUFFIXES = (
    "\"",
    "'",
    "`",
    "}",
    "]",
)


class TitleOutputValidationError(BackendResponseError):
    """모델 출력이 제목 계약과 다르면 안전한 공통 응답 오류를 냅니다."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "TITLE_OUTPUT_INVALID",
            reason,
        )


class TitleOutputValidator:
    """제목 길이, UTF-8, 한 줄과 일반 텍스트 계약을 검증합니다."""

    __slots__ = ("_max_bytes", "_max_characters")

    def __init__(
        self,
        *,
        max_characters: int = DEFAULT_MAX_TITLE_CHARACTERS,
        max_bytes: int = DEFAULT_MAX_TITLE_BYTES,
    ) -> None:
        for field_name, value in (
            ("max_characters", max_characters),
            ("max_bytes", max_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(
                    f"{field_name}는 1 이상의 정수여야 합니다"
                )
        self._max_characters = max_characters
        self._max_bytes = max_bytes

    @property
    def max_characters(self) -> int:
        """허용하는 제목의 최대 Unicode 문자 수를 반환합니다."""

        return self._max_characters

    @property
    def max_bytes(self) -> int:
        """허용하는 제목의 최대 UTF-8 byte 수를 반환합니다."""

        return self._max_bytes

    def validate(self, output: object) -> str:
        """원문을 오류에 남기지 않고 유효한 제목만 반환합니다."""

        if type(output) is not str:
            raise TitleOutputValidationError(
                "제목 출력은 문자열이어야 합니다"
            )

        title = output.strip()
        if not title:
            raise TitleOutputValidationError(
                "제목 출력은 비어 있을 수 없습니다"
            )
        if len(title) > self._max_characters:
            raise TitleOutputValidationError(
                "제목 출력이 문자 수 제한을 초과했습니다"
            )
        try:
            encoded_title = title.encode("utf-8")
        except UnicodeEncodeError:
            raise TitleOutputValidationError(
                "제목 출력을 UTF-8로 표현할 수 없습니다"
            ) from None
        if len(encoded_title) > self._max_bytes:
            raise TitleOutputValidationError(
                "제목 출력이 byte 수 제한을 초과했습니다"
            )
        if any(
            separator in title
            for separator in ("\n", "\r", "\u2028", "\u2029")
        ):
            raise TitleOutputValidationError(
                "제목 출력은 한 줄이어야 합니다"
            )
        if any(
            unicodedata.category(character).startswith("C")
            for character in title
        ):
            raise TitleOutputValidationError(
                "제목 출력에 제어 문자를 사용할 수 없습니다"
            )

        lowered = title.casefold()
        if lowered.startswith(("title:", "제목:")):
            raise TitleOutputValidationError(
                "제목 출력에 설명 접두사를 사용할 수 없습니다"
            )
        if title.startswith(_FORBIDDEN_PREFIXES) or title.endswith(
            _FORBIDDEN_SUFFIXES
        ):
            raise TitleOutputValidationError(
                "제목 출력은 일반 텍스트 한 줄이어야 합니다"
            )
        return title


__all__ = [
    "DEFAULT_MAX_TITLE_BYTES",
    "DEFAULT_MAX_TITLE_CHARACTERS",
    "TitleOutputValidationError",
    "TitleOutputValidator",
]
