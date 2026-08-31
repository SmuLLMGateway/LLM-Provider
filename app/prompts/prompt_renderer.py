"""Jinja2 프롬프트 템플릿에 실행 데이터를 안전하게 렌더링합니다."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Callable

from jinja2 import (
    StrictUndefined,
    Template,
    TemplateError as JinjaTemplateError,
    TemplateSyntaxError as JinjaTemplateSyntaxError,
    UndefinedError,
    meta,
    nodes,
)
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.core.json_codec import StrictJsonEncodeError, dump_json_utf8
from app.prompts.prompt_errors import (
    InvalidPromptContextError,
    MissingPromptVariableError,
    PromptContextTooLargeError,
    PromptOutputTooLargeError,
    PromptRenderError,
    PromptRenderTimeoutError,
    PromptTemplateRenderError,
    PromptTemplateSyntaxError,
    PromptTemplateTooLargeError,
)
from app.prompts.prompt_limits import (
    DEFAULT_PROMPT_LIMITS,
    PromptLimits,
)


@dataclass(frozen=True, slots=True)
class CompiledPromptTemplate:
    """컴파일 환경과 렌더링 정책을 함께 캡처한 불변 실행 Handle입니다."""

    __template: Template = field(repr=False, compare=False)
    referenced_variables: frozenset[str]
    __limits: PromptLimits = field(repr=False, compare=False)
    __clock: Callable[[], float] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """참조 변수 집합을 방어적으로 복사합니다."""

        object.__setattr__(
            self,
            "referenced_variables",
            frozenset(self.referenced_variables),
        )

    def render(
        self,
        *,
        text: str,
        existing_detections: str,
    ) -> str:
        """고정 문자열 Context로 Prompt를 스트리밍 렌더링합니다."""

        return self._render_context(
            {
                "text": text,
                "existing_detections": existing_detections,
            }
        )

    def render_static(self) -> str:
        """외부 변수가 없는 고정 Prompt를 스트리밍 렌더링합니다."""

        return self._render_context({})

    def _render_context(
        self,
        context_values: dict[str, object],
    ) -> str:
        """역할별 공개 메서드가 만든 문자열 Context만 렌더링합니다."""

        started_at = self.__clock()
        try:
            context = self._build_context(context_values)
            self._require_deadline(started_at)

            chunks: list[str] = []
            output_bytes = 0
            generated_chunks = iter(self.__template.generate(context))

            while True:
                self._require_deadline(started_at)
                try:
                    chunk = next(generated_chunks)
                except StopIteration:
                    self._require_deadline(started_at)
                    break

                encoded_chunk = chunk.encode("utf-8")
                output_bytes += len(encoded_chunk)
                if output_bytes > self.__limits.max_output_bytes:
                    raise PromptOutputTooLargeError(
                        output_bytes,
                        self.__limits.max_output_bytes,
                    )
                chunks.append(chunk)
                self._require_deadline(started_at)

            return "".join(chunks)
        except PromptRenderError:
            raise
        except UndefinedError as error:
            raise MissingPromptVariableError(
                f"프롬프트 렌더링 변수가 누락되었습니다: {error}"
            ) from error
        except JinjaTemplateError as error:
            raise PromptTemplateRenderError(
                f"프롬프트 템플릿을 렌더링할 수 없습니다: {error}"
            ) from error
        except Exception as error:
            raise PromptTemplateRenderError(
                f"프롬프트 렌더링 중 오류가 발생했습니다: {error}"
            ) from error

    def _build_context(
        self,
        context_values: dict[str, object],
    ) -> dict[str, str]:
        """문자열 값만 허용하고 전체 Context의 UTF-8 크기를 제한합니다."""

        if any(
            type(key) is not str or type(value) is not str
            for key, value in context_values.items()
        ):
            raise InvalidPromptContextError(
                "고정 Prompt Context의 키와 값은 문자열이어야 합니다"
            )
        context = {
            key: value
            for key, value in context_values.items()
            if isinstance(value, str)
        }
        try:
            encoded_context = dump_json_utf8(context)
        except StrictJsonEncodeError as error:
            raise InvalidPromptContextError(
                "고정 Prompt Context는 JSON으로 직렬화할 수 "
                "있어야 합니다"
            ) from error

        context_bytes = len(encoded_context)
        if context_bytes > self.__limits.max_context_bytes:
            raise PromptContextTooLargeError(
                context_bytes,
                self.__limits.max_context_bytes,
            )
        return context

    def _require_deadline(self, started_at: float) -> None:
        """렌더링이 협력적 deadline을 넘지 않았는지 확인합니다."""

        elapsed_ms = (self.__clock() - started_at) * 1_000
        if elapsed_ms > self.__limits.render_timeout_ms:
            raise PromptRenderTimeoutError(
                self.__limits.render_timeout_ms
            )


class PromptRenderer:
    """고정 Jinja2 본문을 안전한 실행 Handle로 컴파일합니다."""

    def __init__(
        self,
        limits: PromptLimits = DEFAULT_PROMPT_LIMITS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock은 callable이어야 합니다")
        self.limits = limits
        self.clock = clock
        self._environment = ImmutableSandboxedEnvironment(
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        self._environment.globals.clear()

    def _parse_template(self, template_source: str) -> nodes.Template:
        """변수 계약 검사에 사용할 Jinja2 문법 트리를 반환합니다."""

        self._require_template_size(template_source)
        try:
            return self._environment.parse(template_source)
        except JinjaTemplateSyntaxError as error:
            raise PromptTemplateSyntaxError(
                f"프롬프트 템플릿 문법 오류: {error.message}",
                error.lineno,
            ) from error
        except JinjaTemplateError as error:
            raise PromptTemplateRenderError(
                f"프롬프트 템플릿을 해석할 수 없습니다: {error}"
            ) from error
        except Exception as error:
            raise PromptTemplateRenderError(
                f"프롬프트 템플릿 해석 중 오류가 발생했습니다: {error}"
            ) from error

    def compile_template(
        self,
        template_source: str,
    ) -> CompiledPromptTemplate:
        """템플릿을 컴파일하고 원시 Jinja 객체를 숨긴 Handle을 반환합니다."""

        try:
            parsed_template = self._parse_template(template_source)
            referenced_variables = frozenset(
                meta.find_undeclared_variables(parsed_template)
            )
            template_code = self._environment.compile(parsed_template)
            template = self._environment.template_class.from_code(
                self._environment,
                template_code,
                self._environment.make_globals(None),
                None,
            )
            return CompiledPromptTemplate(
                template,
                referenced_variables,
                self.limits,
                self.clock,
            )
        except PromptRenderError:
            raise
        except JinjaTemplateSyntaxError as error:
            raise PromptTemplateSyntaxError(
                f"프롬프트 템플릿 문법 오류: {error.message}",
                error.lineno,
            ) from error
        except JinjaTemplateError as error:
            raise PromptTemplateRenderError(
                f"프롬프트 템플릿을 렌더링할 수 없습니다: {error}"
            ) from error
        except Exception as error:
            raise PromptTemplateRenderError(
                f"프롬프트 렌더링 중 오류가 발생했습니다: {error}"
            ) from error

    def _require_template_size(self, template_source: str) -> None:
        """템플릿 본문의 UTF-8 크기를 컴파일 전에 제한합니다."""

        if not isinstance(template_source, str):
            raise PromptTemplateRenderError(
                "프롬프트 템플릿 본문은 문자열이어야 합니다"
            )
        try:
            template_bytes = len(template_source.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise PromptTemplateRenderError(
                "프롬프트 템플릿을 UTF-8로 인코딩할 수 없습니다"
            ) from error

        if template_bytes > self.limits.max_template_bytes:
            raise PromptTemplateTooLargeError(
                "<inline>",
                template_bytes,
                self.limits.max_template_bytes,
            )


__all__ = [
    "CompiledPromptTemplate",
    "InvalidPromptContextError",
    "MissingPromptVariableError",
    "PromptContextTooLargeError",
    "PromptOutputTooLargeError",
    "PromptRenderError",
    "PromptRenderTimeoutError",
    "PromptRenderer",
    "PromptTemplateRenderError",
    "PromptTemplateSyntaxError",
    "PromptTemplateTooLargeError",
]
