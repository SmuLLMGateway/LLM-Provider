"""Prompt 모듈에서 사용하는 공통 오류를 정의합니다."""


class PromptError(Exception):
    """Prompt 처리 중 발생하는 모든 오류의 공통 기반입니다."""


class PromptTemplateError(PromptError):
    """프롬프트 템플릿 파일을 처리할 수 없으면 발생합니다."""

    def __init__(self, template_path: str, message: str) -> None:
        self.template_path = template_path
        super().__init__(message)


class PromptTemplateNotFoundError(PromptTemplateError):
    """고정 Prompt 파일이 존재하지 않거나 일반 파일이 아니면 발생합니다."""


class PromptTemplateDecodeError(PromptTemplateError):
    """고정 Prompt 파일을 UTF-8로 해석할 수 없으면 발생합니다."""


class PromptTemplateReadError(PromptTemplateError):
    """파일 시스템 오류로 고정 Prompt를 읽을 수 없으면 발생합니다."""


class InvalidPromptTemplateContentError(PromptTemplateError):
    """고정 Prompt 본문이 비어 있거나 UTF-8로 표현될 수 없으면 발생합니다."""


class PromptVariableContractError(PromptTemplateError):
    """고정 Prompt가 정확한 외부 변수 계약을 따르지 않으면 발생합니다."""

    def __init__(
        self,
        template_path: str,
        *,
        required_variables: frozenset[str],
        actual_variables: frozenset[str],
    ) -> None:
        self.required_variables = frozenset(required_variables)
        self.actual_variables = frozenset(actual_variables)
        self.missing_variables = (
            self.required_variables - self.actual_variables
        )
        self.unexpected_variables = (
            self.actual_variables - self.required_variables
        )
        super().__init__(
            template_path,
            "고정 Prompt의 외부 변수 계약이 올바르지 않습니다: "
            f"required={sorted(self.required_variables)}, "
            f"actual={sorted(self.actual_variables)}",
        )


class PromptRenderError(PromptError):
    """프롬프트를 렌더링할 수 없을 때 발생하는 기본 오류입니다."""


class InvalidPromptContextError(PromptRenderError):
    """렌더링 Context가 안전한 JSON 호환 형식이 아니면 발생합니다."""


class PromptTemplateTooLargeError(PromptTemplateError, PromptRenderError):
    """템플릿의 UTF-8 크기가 허용된 제한을 넘으면 발생합니다."""

    def __init__(
        self,
        template_path: str,
        actual_bytes: int,
        max_bytes: int,
    ) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            template_path,
            "프롬프트 템플릿이 허용 크기를 초과했습니다: "
            f"{template_path} ({actual_bytes} > {max_bytes} bytes)",
        )


class PromptContextTooLargeError(InvalidPromptContextError):
    """렌더링 Context의 UTF-8 크기가 제한을 넘으면 발생합니다."""

    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "프롬프트 렌더링 Context가 허용 크기를 초과했습니다: "
            f"{actual_bytes} > {max_bytes} bytes"
        )


class PromptOutputTooLargeError(PromptRenderError):
    """렌더링 출력의 UTF-8 크기가 제한을 넘으면 발생합니다."""

    def __init__(self, actual_bytes: int, max_bytes: int) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "프롬프트 렌더링 결과가 허용 크기를 초과했습니다: "
            f"{actual_bytes} > {max_bytes} bytes"
        )


class PromptRenderTimeoutError(PromptRenderError):
    """협력적 렌더링 deadline을 초과하면 발생합니다."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(
            "프롬프트 렌더링 제한 시간을 초과했습니다: "
            f"{timeout_ms}ms"
        )


class PromptTemplateSyntaxError(PromptRenderError):
    """Jinja2 템플릿 문법이 올바르지 않으면 발생합니다."""

    def __init__(self, message: str, line_number: int) -> None:
        self.line_number = line_number
        super().__init__(message)


class MissingPromptVariableError(PromptRenderError):
    """템플릿에서 참조한 변수가 제공되지 않으면 발생합니다."""


class PromptTemplateRenderError(PromptRenderError):
    """문법과 변수 외의 이유로 렌더링에 실패하면 발생합니다."""


__all__ = [
    "InvalidPromptTemplateContentError",
    "InvalidPromptContextError",
    "MissingPromptVariableError",
    "PromptContextTooLargeError",
    "PromptError",
    "PromptOutputTooLargeError",
    "PromptRenderError",
    "PromptRenderTimeoutError",
    "PromptTemplateError",
    "PromptTemplateDecodeError",
    "PromptTemplateNotFoundError",
    "PromptTemplateReadError",
    "PromptTemplateRenderError",
    "PromptTemplateSyntaxError",
    "PromptTemplateTooLargeError",
    "PromptVariableContractError",
]
