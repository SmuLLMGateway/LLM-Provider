"""고정 Prompt와 요청별 Local LLM으로 검증 가능한 마스킹을 수행합니다."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable

from app.backends.provider_registry import BackendProviderRegistry
from app.core.json_codec import StrictJsonEncodeError, dump_canonical_json_utf8
from app.policies.mask_target_resolver import MaskTarget, MaskTargetResolver
from app.policies.span_validator import DetectionSpanValidationError, SpanValidator
from app.prompts.prompt_errors import PromptContextTooLargeError
from app.registry.deployment_resolver import DeploymentResolver
from app.schemas.detection import Detection
from app.schemas.masking import MaskResponse
from app.services.llm_masking_output_parser import (
    LlmMaskingOutputError,
    LlmMaskingOutputParser,
)
from app.services.llm_masking_output_validator import (
    LlmMaskingOutputValidator,
    LlmMaskingValidationError,
)
from app.services.llm_result_validator import (
    LlmResultValidationError,
    validate_llm_result,
)
from app.services.registry_snapshot_provider import RegistrySnapshotProvider


MASKING_MAX_TOKENS = 16_384
DEFAULT_MAX_MASKING_INPUT_BYTES = 12_288
DEFAULT_MAX_MASKING_PREFLIGHT_OUTPUT_BYTES = 16_384
_NAMESPACE_PATTERN = re.compile(r"^[0-9a-f]{16}$")


class MaskingBackendResultError(TypeError):
    """LLM Backend가 공통 LlmResult 계약을 지키지 않으면 발생합니다."""

    def __init__(self, *, deployment_id: str, actual_type: type[object]) -> None:
        self.deployment_id = deployment_id
        self.actual_type = actual_type
        super().__init__(
            "마스킹 Backend는 LlmResult를 반환해야 합니다: "
            f"deployment={deployment_id}, actual={actual_type.__name__}"
        )


class MaskingInputSerializationError(ValueError):
    """검증된 마스킹 입력을 안전한 JSON으로 직렬화할 수 없으면 발생합니다."""


class MaskingNamespaceError(RuntimeError):
    """원문과 충돌하지 않는 요청 범위 placeholder namespace를 만들 수 없습니다."""


class MaskingPipeline:
    """실행 계획, LLM 호출과 fail-closed 출력 검증을 조립합니다."""

    def __init__(
        self,
        *,
        registry_manager: RegistrySnapshotProvider,
        deployment_resolver: DeploymentResolver,
        backend_providers: BackendProviderRegistry,
        span_validator: SpanValidator | None = None,
        target_resolver: MaskTargetResolver | None = None,
        output_parser: LlmMaskingOutputParser | None = None,
        output_validator: LlmMaskingOutputValidator | None = None,
        namespace_factory: Callable[[], str] | None = None,
        max_input_bytes: int = DEFAULT_MAX_MASKING_INPUT_BYTES,
        max_preflight_output_bytes: int = (
            DEFAULT_MAX_MASKING_PREFLIGHT_OUTPUT_BYTES
        ),
    ) -> None:
        if type(max_input_bytes) is not int or max_input_bytes < 1:
            raise ValueError("max_input_bytes는 1 이상의 정수여야 합니다")
        if (
            type(max_preflight_output_bytes) is not int
            or max_preflight_output_bytes < 1
        ):
            raise ValueError(
                "max_preflight_output_bytes는 1 이상의 정수여야 합니다"
            )
        self._registry_manager = registry_manager
        self._deployment_resolver = deployment_resolver
        self._backend_providers = backend_providers
        self._span_validator = span_validator or SpanValidator()
        self._target_resolver = target_resolver or MaskTargetResolver()
        self._output_parser = output_parser or LlmMaskingOutputParser()
        self._output_validator = output_validator or LlmMaskingOutputValidator()
        self._namespace_factory = namespace_factory or (
            lambda: secrets.token_hex(8)
        )
        self._max_input_bytes = max_input_bytes
        self._max_preflight_output_bytes = max_preflight_output_bytes

    @property
    def max_input_bytes(self) -> int:
        """마스킹 user JSON에 적용하는 최대 UTF-8 byte 크기입니다."""

        return self._max_input_bytes

    @property
    def max_preflight_output_bytes(self) -> int:
        """LLM 호출 전 허용하는 최악 출력 JSON의 최대 UTF-8 크기입니다."""

        return self._max_preflight_output_bytes

    async def mask(
        self,
        *,
        text: str,
        llm_deployment_id: str,
        detections: tuple[Detection, ...],
    ) -> MaskResponse:
        """모델 출력이나 원문을 오류 객체에 남기지 않고 마스킹을 수행합니다."""

        try:
            return await self._mask(
                text=text,
                llm_deployment_id=llm_deployment_id,
                detections=detections,
            )
        except DetectionSpanValidationError as error:
            safe_error: Exception = DetectionSpanValidationError(
                error.code,
                item_index=error.item_index,
                actual_items=error.actual_items,
                max_detections=error.max_detections,
            )
        except LlmMaskingOutputError as error:
            safe_error = LlmMaskingOutputError(
                error.code,
                item_index=error.item_index,
                actual_bytes=error.actual_bytes,
                max_output_bytes=error.max_output_bytes,
                actual_items=error.actual_items,
                max_assignments=error.max_assignments,
            )
        except LlmMaskingValidationError as error:
            safe_error = LlmMaskingValidationError(error.code)
        except MaskingBackendResultError as error:
            safe_error = MaskingBackendResultError(
                deployment_id=error.deployment_id,
                actual_type=error.actual_type,
            )
        except PromptContextTooLargeError as error:
            safe_error = PromptContextTooLargeError(
                error.actual_bytes,
                error.max_bytes,
            )
        except MaskingInputSerializationError:
            safe_error = MaskingInputSerializationError()
        except MaskingNamespaceError:
            safe_error = MaskingNamespaceError()
        else:
            raise RuntimeError("마스킹 Pipeline 실행 결과가 없습니다")

        del self, text, llm_deployment_id, detections
        raise safe_error from None

    async def _mask(
        self,
        *,
        text: str,
        llm_deployment_id: str,
        detections: tuple[Detection, ...],
    ) -> MaskResponse:
        """한 Snapshot의 LLM과 Prompt만 사용해 마스킹합니다."""

        snapshot = self._registry_manager.capture()
        plan = self._deployment_resolver.resolve_masking(
            llm_deployment_id=llm_deployment_id,
            snapshot=snapshot,
        )
        self._require_text_size(text)
        validated_detections = self._span_validator.validate(
            text,
            detections,
        )
        targets = self._target_resolver.resolve(text, validated_detections)
        if not targets:
            return MaskResponse.model_validate(
                {"maskedText": text, "replacements": []}
            )

        namespace = self._create_namespace(text)
        user_message = self._serialize_input(
            text=text,
            targets=targets,
            placeholder_namespace=namespace,
        )
        self._require_output_budget(
            text=text,
            targets=targets,
            placeholder_namespace=namespace,
        )
        deployment = plan.llm_deployment
        backend = self._backend_providers.require_llm(
            deployment_id=deployment.id,
            deployment=deployment.config,
        )
        result = await backend.generate(
            messages=[
                {"role": "system", "content": plan.mask_prompt.render()},
                {"role": "user", "content": user_message},
            ],
            deployment=deployment.config,
            parameters={"max_tokens": MASKING_MAX_TOKENS},
            output_schema=_build_output_schema(targets),
        )
        try:
            validated_result = validate_llm_result(result)
        except LlmResultValidationError as error:
            raise MaskingBackendResultError(
                deployment_id=deployment.id,
                actual_type=error.actual_type,
            ) from None

        parsed = self._output_parser.parse(validated_result.text)
        return self._output_validator.validate(
            original_text=text,
            targets=targets,
            placeholder_namespace=namespace,
            output=parsed,
        )

    def _create_namespace(self, original_text: str) -> str:
        """원문에 없는 placeholder prefix를 제한된 횟수 안에서 만듭니다."""

        for _ in range(16):
            namespace = self._namespace_factory()
            if (
                type(namespace) is str
                and _NAMESPACE_PATTERN.fullmatch(namespace) is not None
                and f"[[LPL_{namespace}_" not in original_text
            ):
                return namespace
        raise MaskingNamespaceError()

    def _serialize_input(
        self,
        *,
        text: str,
        targets: tuple[MaskTarget, ...],
        placeholder_namespace: str,
    ) -> str:
        """원문과 대상 정보를 Prompt와 분리된 canonical JSON으로 만듭니다."""

        payload = {
            "placeholderNamespace": placeholder_namespace,
            "targets": [
                {
                    "end": target.end,
                    "sources": list(target.sources),
                    "start": target.start,
                    "targetId": target.target_id,
                    "text": target.text,
                    "types": list(target.types),
                }
                for target in targets
            ],
            "text": text,
        }
        try:
            encoded = dump_canonical_json_utf8(payload)
        except StrictJsonEncodeError:
            del payload
            raise MaskingInputSerializationError() from None
        if len(encoded) > self._max_input_bytes:
            actual_bytes = len(encoded)
            del payload, encoded
            raise PromptContextTooLargeError(
                actual_bytes,
                self._max_input_bytes,
            )
        return encoded.decode("utf-8")

    def _require_text_size(self, text: str) -> None:
        """빈 Detection 단축 경로에도 원문 UTF-8 byte 상한을 적용합니다."""

        if type(text) is not str:
            raise DetectionSpanValidationError("INVALID_TEXT")
        try:
            actual_bytes = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise DetectionSpanValidationError("INVALID_TEXT") from None
        if actual_bytes > self._max_input_bytes:
            raise PromptContextTooLargeError(
                actual_bytes,
                self._max_input_bytes,
            )

    def _require_output_budget(
        self,
        *,
        text: str,
        targets: tuple[MaskTarget, ...],
        placeholder_namespace: str,
    ) -> None:
        """모든 target이 별도 entity인 최악 출력 크기를 호출 전에 계산합니다."""

        fragments: list[str] = []
        assignments: list[dict[str, str]] = []
        cursor = 0
        for ordinal, target in enumerate(targets, start=1):
            entity_id = f"entity-{ordinal}"
            placeholder = (
                f"[[LPL_{placeholder_namespace}_{ordinal:04d}]]"
            )
            fragments.append(text[cursor : target.start])
            fragments.append(placeholder)
            cursor = target.end
            assignments.append(
                {
                    "entityId": entity_id,
                    "targetId": target.target_id,
                }
            )
        fragments.append(text[cursor:])
        try:
            encoded = dump_canonical_json_utf8(
                {
                    "assignments": assignments,
                    "maskedText": "".join(fragments),
                }
            )
        except StrictJsonEncodeError:
            del fragments, assignments
            raise MaskingInputSerializationError() from None
        if len(encoded) > self._max_preflight_output_bytes:
            actual_bytes = len(encoded)
            del fragments, assignments, encoded
            raise PromptContextTooLargeError(
                actual_bytes,
                self._max_preflight_output_bytes,
            )


def _build_output_schema(
    targets: tuple[MaskTarget, ...],
) -> dict[str, object]:
    """현재 요청 target만 참조할 수 있는 strict JSON Schema를 만듭니다."""

    count = len(targets)
    return {
        "type": "object",
        "properties": {
            "maskedText": {"type": "string"},
            "assignments": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "targetId": {
                            "type": "string",
                            "enum": [target.target_id for target in targets],
                        },
                        "entityId": {
                            "type": "string",
                            "pattern": r"^entity-[1-9][0-9]{0,4}$",
                        },
                    },
                    "required": ["targetId", "entityId"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["maskedText", "assignments"],
        "additionalProperties": False,
    }


__all__ = [
    "DEFAULT_MAX_MASKING_INPUT_BYTES",
    "DEFAULT_MAX_MASKING_PREFLIGHT_OUTPUT_BYTES",
    "MASKING_MAX_TOKENS",
    "MaskingBackendResultError",
    "MaskingInputSerializationError",
    "MaskingNamespaceError",
    "MaskingPipeline",
]
