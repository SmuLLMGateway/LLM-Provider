"""요청이 선택한 NER와 LLM으로 탐지 단계를 순서대로 수행합니다."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import ValidationError

from app.backends.provider_registry import BackendProviderRegistry
from app.core.json_codec import dump_canonical_json_utf8
from app.policies.detection_result_validator import DetectionResultValidator
from app.policies.organization_profile_validator import (
    OrganizationProfilePolicyError,
    OrganizationProfileValidator,
)
from app.policies.span_validator import DetectionSpanValidationError
from app.registry.deployment_resolver import DeploymentResolver
from app.registry.execution_plan import (
    DetectionExecutionPlan,
    ResolvedDeployment,
)
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_errors import PromptContextTooLargeError
from app.schemas.detection import (
    CandidateDecision,
    CandidateDecisionValue,
    DETECTION_TYPE_BY_POLICY_ID,
    ENTITY_TYPE_BY_POLICY_ID,
    DetectResponse,
    Detection,
    DetectionType,
    NerCandidate,
    NerCandidateDecision,
    PolicyId,
    RegexCandidate,
    RegexCandidateDecision,
)
from app.schemas.policy_settings import (
    PolicySettings,
    default_policy_settings,
)
from app.schemas.detection_context import (
    OrganizationProfile,
    SourceType,
)
from app.services.llm_detection_output_parser import (
    LlmCandidateDecision,
    LlmDetectionCandidate,
    LlmDetectionOutput,
    LlmDetectionOutputError,
    LlmDetectionOutputErrorCode,
    LlmDetectionOutputParser,
)
from app.services.llm_detection_span_resolver import (
    LlmDetectionSpanResolver,
)
from app.services.llm_result_validator import (
    LlmResultValidationError,
    validate_llm_result,
)
from app.services.registry_snapshot_provider import RegistrySnapshotProvider


DetectionStage = Literal[
    "confidential_detection",
]
DEFAULT_MAX_DETECTION_CONTEXT_BYTES = 512 * 1024
DETECTION_REASONING_EFFORT = "none"
RETRYABLE_LLM_DETECTION_OUTPUT_ERRORS: frozenset[
    LlmDetectionOutputErrorCode
] = frozenset(
    {
        "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND",
        "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS",
    }
)


class PolicySettingsProvider(Protocol):
    """Detection 요청이 사용할 활성 정책 Snapshot을 제공합니다."""

    def capture(self) -> PolicySettings:
        """현재 불변 정책 설정을 반환합니다."""

        ...


class _DefaultPolicySettingsProvider:
    """직접 조립한 Pipeline에 전체 활성 기본 설정을 제공합니다."""

    def __init__(self) -> None:
        self._settings = default_policy_settings()

    def capture(self) -> PolicySettings:
        """생성 시 고정한 기본 설정을 반환합니다."""

        return self._settings


class DetectionBackendResultError(TypeError):
    """탐지 LLM Backend가 공통 결과 계약을 지키지 않으면 발생합니다."""

    def __init__(
        self,
        *,
        stage: DetectionStage,
        deployment_id: str,
        actual_type: type[object],
    ) -> None:
        self.stage = stage
        self.deployment_id = deployment_id
        self.actual_type = actual_type
        super().__init__(
            "탐지 LLM Backend는 LlmResult를 반환해야 합니다: "
            f"stage={stage}, deployment={deployment_id}, "
            f"actual={actual_type.__name__}"
        )


class DetectionPolicySelectionError(ValueError):
    """Gateway Regex 후보가 현재 비활성 정책을 참조하면 발생합니다."""

    def __init__(self, *, item_index: int) -> None:
        self.item_index = item_index
        super().__init__(
            "RegexCandidate가 비활성 정책을 참조합니다: "
            f"index={item_index}"
        )


class DetectionPipeline:
    """하나의 실행 계획으로 탐지 단계 전체를 수행하고 정규화합니다."""

    def __init__(
        self,
        *,
        registry_manager: RegistrySnapshotProvider,
        deployment_resolver: DeploymentResolver,
        backend_providers: BackendProviderRegistry,
        output_parser: LlmDetectionOutputParser | None = None,
        span_resolver: LlmDetectionSpanResolver | None = None,
        result_validator: DetectionResultValidator | None = None,
        policy_settings_provider: PolicySettingsProvider | None = None,
        organization_profile_validator: (
            OrganizationProfileValidator | None
        ) = None,
    ) -> None:
        self._registry_manager = registry_manager
        self._deployment_resolver = deployment_resolver
        self._backend_providers = backend_providers
        self._output_parser = (
            output_parser
            if output_parser is not None
            else LlmDetectionOutputParser()
        )
        self._span_resolver = (
            span_resolver
            if span_resolver is not None
            else LlmDetectionSpanResolver(
                max_detections=self._output_parser.max_detections,
            )
        )
        self._result_validator = (
            result_validator
            if result_validator is not None
            else DetectionResultValidator()
        )
        self._policy_settings_provider = (
            policy_settings_provider
            if policy_settings_provider is not None
            else _DefaultPolicySettingsProvider()
        )
        self._organization_profile_validator = (
            organization_profile_validator
            if organization_profile_validator is not None
            else OrganizationProfileValidator()
        )

    async def detect(
        self,
        *,
        text: str,
        ner_deployment_id: str,
        llm_deployment_id: str,
        organization_profile: OrganizationProfile | None,
        source_type: SourceType,
        regex_candidates: tuple[RegexCandidate, ...] = (),
    ) -> DetectResponse:
        """후보 판정과 문맥 탐지를 실행해 검증된 결과를 반환합니다."""

        response: DetectResponse | None = None
        safe_error: (
            DetectionBackendResultError
            | DetectionPolicySelectionError
            | DetectionSpanValidationError
            | LlmDetectionOutputError
            | OrganizationProfilePolicyError
            | None
        ) = None
        try:
            response = await self._detect(
                text=text,
                ner_deployment_id=ner_deployment_id,
                llm_deployment_id=llm_deployment_id,
                organization_profile=organization_profile,
                source_type=source_type,
                regex_candidates=regex_candidates,
            )
        except DetectionBackendResultError as error:
            safe_error = DetectionBackendResultError(
                stage=error.stage,
                deployment_id=error.deployment_id,
                actual_type=error.actual_type,
            )
        except DetectionPolicySelectionError as error:
            safe_error = DetectionPolicySelectionError(
                item_index=error.item_index,
            )
        except OrganizationProfilePolicyError as error:
            safe_error = OrganizationProfilePolicyError(
                missing_sections=error.missing_sections,
            )
        except DetectionSpanValidationError as error:
            safe_error = DetectionSpanValidationError(
                error.code,
                item_index=error.item_index,
                actual_items=error.actual_items,
                max_detections=error.max_detections,
            )
        except LlmDetectionOutputError as error:
            safe_error = LlmDetectionOutputError(
                error.code,
                item_index=error.item_index,
                actual_bytes=error.actual_bytes,
                max_output_bytes=error.max_output_bytes,
                actual_items=error.actual_items,
                max_detections=error.max_detections,
            )

        if safe_error is not None:
            del (
                self,
                text,
                ner_deployment_id,
                llm_deployment_id,
                organization_profile,
                source_type,
                regex_candidates,
                response,
            )
            raise safe_error from None
        if response is None:
            raise RuntimeError("Detection Pipeline 실행 결과가 없습니다")
        return response

    async def _detect(
        self,
        *,
        text: str,
        ner_deployment_id: str,
        llm_deployment_id: str,
        organization_profile: OrganizationProfile | None,
        source_type: SourceType,
        regex_candidates: tuple[RegexCandidate, ...],
    ) -> DetectResponse:
        """한 Snapshot에서 조립한 실행 계획으로 탐지 단계를 수행합니다."""

        snapshot = self._registry_manager.capture()
        policy_settings = self._policy_settings_provider.capture()
        enabled_policy_ids = policy_settings.enabled_policy_ids
        self._organization_profile_validator.validate(
            organization_profile,
            enabled_policy_ids=enabled_policy_ids,
        )
        if type(source_type) is not str or source_type not in {
            "CHAT_TEXT",
            "OCR_TEXT",
        }:
            raise TypeError("source_type은 CHAT_TEXT 또는 OCR_TEXT여야 합니다")
        _require_detection_context_size(
            organization_profile=organization_profile,
            source_type=source_type,
        )
        plan = self._deployment_resolver.resolve_detection(
            ner_deployment_id=ner_deployment_id,
            llm_deployment_id=llm_deployment_id,
            snapshot=snapshot,
        )

        regex_detections = tuple(
            _regex_candidate_to_detection(candidate)
            for candidate in regex_candidates
        )
        validated_existing = self._result_validator.validate(
            text,
            regex_detections,
            expected_source="regex",
        )
        enabled_policy_set = frozenset(enabled_policy_ids)
        for index, detection in enumerate(validated_existing):
            if detection.policy_id not in enabled_policy_set:
                raise DetectionPolicySelectionError(item_index=index)
        entity_detections = await self._detect_entities(
            text=text,
            plan=plan,
        )
        entity_detections = tuple(
            detection
            for detection in entity_detections
            if detection.policy_id in enabled_policy_set
        )

        contextual_evidence = self._result_validator.validate(
            text,
            (*validated_existing, *entity_detections),
        )
        ner_candidates = _build_ner_candidates(
            tuple(
                detection
                for detection in contextual_evidence
                if detection.source == "ner"
            )
        )
        selected_policy_ids = (
            plan.detection_prompt.policy_prompts.select_policy_ids(
                enabled_policy_ids=enabled_policy_ids,
                candidate_policy_ids=tuple(
                    candidate.policy_id
                    for candidate in (*regex_candidates, *ner_candidates)
                ),
            )
        )
        if not selected_policy_ids:
            return DetectResponse()
        candidate_decisions, contextual_detections = (
            await self._detect_confidential_context(
                text=text,
                plan=plan,
                regex_candidates=regex_candidates,
                ner_candidates=ner_candidates,
                organization_profile=organization_profile,
                source_type=source_type,
                selected_policy_ids=selected_policy_ids,
            )
        )

        all_detections = self._result_validator.validate(
            text,
            (
                *validated_existing,
                *entity_detections,
                *contextual_detections,
            ),
        )
        new_llm_detections = tuple(
            detection
            for detection in all_detections
            if detection.source == "llm"
        )
        confirmed_ner_detections = tuple(
            _ner_candidate_decision_to_detection(decision)
            for decision in candidate_decisions
            if (
                type(decision) is NerCandidateDecision
                and decision.decision == "CONFIRMED"
            )
        )
        response_detections = self._result_validator.validate(
            text,
            (*confirmed_ner_detections, *new_llm_detections),
        )
        return DetectResponse(
            candidateDecisions=candidate_decisions,
            detections=response_detections,
        )

    async def _detect_entities(
        self,
        *,
        text: str,
        plan: DetectionExecutionPlan,
    ) -> tuple[Detection, ...]:
        """요청이 선택한 NER Backend로 개체를 탐지합니다."""

        deployment = plan.ner_deployment
        backend = self._backend_providers.require_ner(
            deployment_id=deployment.id,
            deployment=deployment.config,
        )
        result = await backend.detect(text, deployment.config)
        return self._result_validator.validate(
            text,
            result,
            expected_source="ner",
        )

    async def _detect_confidential_context(
        self,
        *,
        text: str,
        plan: DetectionExecutionPlan,
        regex_candidates: tuple[RegexCandidate, ...],
        ner_candidates: tuple[NerCandidate, ...],
        organization_profile: OrganizationProfile | None,
        source_type: SourceType,
        selected_policy_ids: tuple[PolicyId, ...],
    ) -> tuple[tuple[CandidateDecision, ...], tuple[Detection, ...]]:
        """고정 Prompt로 후보를 판정하고 누락·문맥형 기밀을 탐지합니다."""

        raw_decisions, detections = await self._run_llm_detection(
            stage="confidential_detection",
            text=text,
            deployment=plan.llm_deployment,
            prompt=plan.detection_prompt,
            detection_context=_serialize_detection_context(
                regex_candidates=regex_candidates,
                ner_candidates=ner_candidates,
                organization_profile=organization_profile,
                source_type=source_type,
                enabled_policy_ids=selected_policy_ids,
            ),
            candidate_ids=tuple(
                candidate.candidate_id
                for candidate in (*regex_candidates, *ner_candidates)
            ),
            selected_policy_ids=selected_policy_ids,
        )
        return (
            _build_candidate_decision_responses(
                regex_candidates=regex_candidates,
                ner_candidates=ner_candidates,
                decisions=raw_decisions,
            ),
            detections,
        )

    async def _run_llm_detection(
        self,
        *,
        stage: DetectionStage,
        text: str,
        deployment: ResolvedDeployment,
        prompt: PromptArtifact,
        detection_context: str,
        candidate_ids: tuple[str, ...],
        selected_policy_ids: tuple[PolicyId, ...],
    ) -> tuple[tuple[LlmCandidateDecision, ...], tuple[Detection, ...]]:
        """Prompt 렌더링부터 LLM 출력 파싱과 Span 검증까지 수행합니다."""

        backend = self._backend_providers.require_llm(
            deployment_id=deployment.id,
            deployment=deployment.config,
        )
        rendered_prompt = prompt.render(
            text=text,
            existing_detections=detection_context,
            policy_ids=selected_policy_ids,
        )
        enabled_detection_types = tuple(
            DETECTION_TYPE_BY_POLICY_ID[policy_id]
            for policy_id in selected_policy_ids
        )
        initial_messages = [
            {
                "role": "user",
                "content": rendered_prompt,
            }
        ]
        parameters = {
            "temperature": 0,
            "reasoning_effort": DETECTION_REASONING_EFFORT,
        }
        output_schema = _build_output_schema(
            candidate_ids=candidate_ids,
            max_detections=self._output_parser.max_detections,
            enabled_detection_types=enabled_detection_types,
        )
        result = await backend.generate(
            messages=initial_messages,
            deployment=deployment.config,
            parameters=parameters,
            output_schema=output_schema,
        )
        output = _require_llm_output(
            result,
            stage=stage,
            deployment_id=deployment.id,
        )
        retry_reason: tuple[
            LlmDetectionOutputErrorCode,
            int | None,
        ] | None = None
        try:
            return _parse_and_resolve_llm_detection_output(
                output=output,
                text=text,
                candidate_ids=candidate_ids,
                selected_policy_ids=selected_policy_ids,
                output_parser=self._output_parser,
                span_resolver=self._span_resolver,
                result_validator=self._result_validator,
            )
        except LlmDetectionOutputError as error:
            if error.code not in RETRYABLE_LLM_DETECTION_OUTPUT_ERRORS:
                del output, result, rendered_prompt, initial_messages
                raise
            retry_reason = (error.code, error.item_index)

        if retry_reason is None:
            raise RuntimeError("LLM 탐지 출력 재시도 사유가 없습니다")
        correction_messages = [
            *initial_messages,
            {
                "role": "assistant",
                "content": output,
            },
            {
                "role": "user",
                "content": _build_detection_output_correction_instruction(
                    code=retry_reason[0],
                    item_index=retry_reason[1],
                ),
            },
        ]
        del result, output, retry_reason, initial_messages
        corrected_result = await backend.generate(
            messages=correction_messages,
            deployment=deployment.config,
            parameters=parameters,
            output_schema=output_schema,
        )
        corrected_output = _require_llm_output(
            corrected_result,
            stage=stage,
            deployment_id=deployment.id,
        )
        del corrected_result, correction_messages, rendered_prompt
        return _parse_and_resolve_llm_detection_output(
            output=corrected_output,
            text=text,
            candidate_ids=candidate_ids,
            selected_policy_ids=selected_policy_ids,
            output_parser=self._output_parser,
            span_resolver=self._span_resolver,
            result_validator=self._result_validator,
        )


def _parse_and_resolve_llm_detection_output(
    *,
    output: str,
    text: str,
    candidate_ids: tuple[str, ...],
    selected_policy_ids: tuple[PolicyId, ...],
    output_parser: LlmDetectionOutputParser,
    span_resolver: LlmDetectionSpanResolver,
    result_validator: DetectionResultValidator,
) -> tuple[tuple[LlmCandidateDecision, ...], tuple[Detection, ...]]:
    """한 LLM 출력을 파싱하고 원문 좌표까지 fail-closed로 검증합니다."""

    parsed_output = output_parser.parse(output)
    decisions, new_detection_candidates = (
        _require_candidate_decision_coverage(
            parsed_output,
            candidate_ids=candidate_ids,
        )
    )
    parsed = span_resolver.resolve(
        text,
        new_detection_candidates,
    )
    _require_enabled_llm_detections(
        parsed,
        enabled_policy_ids=selected_policy_ids,
    )
    return (
        decisions,
        result_validator.validate(
            text,
            parsed,
            expected_source="llm",
        ),
    )


def _build_detection_output_correction_instruction(
    *,
    code: LlmDetectionOutputErrorCode,
    item_index: int | None,
) -> str:
    """원문을 반복하지 않고 Span 계약 위반만 모델에 알려줍니다."""

    index_text = str(item_index) if item_index is not None else "unknown"
    failure = (
        "원문에 정확히 존재하지 않습니다"
        if code == "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND"
        else "원문에서 두 번 이상 나타나 위치를 고유하게 결정할 수 없습니다"
    )
    return (
        "이전 JSON 응답을 수정하여 전체 JSON 객체를 다시 출력하세요.\n"
        f"newDetections[{index_text}].text가 {failure}.\n"
        "모든 newDetections[].text는 original_text에서 연속된 문자열을 "
        "문자 단위로 그대로 복사해야 합니다. 조사, 띄어쓰기, 문장부호, "
        "따옴표와 숫자 형식을 삭제·추가·정규화하거나 요약하지 마세요.\n"
        "문맥형 type의 문자열이 반복되면 민감한 값을 포함하면서 원문에서 "
        "한 번만 나타나는 최소한의 연속 구절로 확장하세요.\n"
        "regexCandidates 또는 nerCandidates와 동일한 대상을 "
        "newDetections에 다시 넣지 마세요.\n"
        "candidateDecisions를 포함한 전체 응답을 기존 JSON Schema에 맞춰 "
        "설명이나 Markdown 없이 순수 JSON으로 다시 출력하세요."
    )


def _build_output_schema(
    *,
    candidate_ids: tuple[str, ...],
    max_detections: int,
    enabled_detection_types: tuple[DetectionType, ...],
) -> dict[str, object]:
    """후보 판정과 좌표 없는 신규 탐지만 생성하도록 제한합니다."""

    return {
        "type": "object",
        "properties": {
            "candidateDecisions": {
                "type": "array",
                "minItems": len(candidate_ids),
                "maxItems": len(candidate_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "candidateId": (
                            {
                                "type": "string",
                                "enum": list(candidate_ids),
                            }
                            if candidate_ids
                            else {
                                "type": "string",
                                "pattern": r"^[RN][0-9]{3,}$",
                            }
                        ),
                        "decision": {
                            "type": "string",
                            "enum": [
                                "CONFIRMED",
                                "REJECTED",
                                "UNCERTAIN",
                            ],
                        },
                    },
                    "required": ["candidateId", "decision"],
                    "additionalProperties": False,
                },
            },
            "newDetections": {
                "type": "array",
                "maxItems": max_detections,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "type": {
                            "type": "string",
                            "enum": list(enabled_detection_types),
                        },
                        "score": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["text", "type", "score"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["candidateDecisions", "newDetections"],
        "additionalProperties": False,
    }


def _require_llm_output(
    result: object,
    *,
    stage: DetectionStage,
    deployment_id: str,
) -> str:
    """Backend 결과와 text 필드를 검증 우회 가능성까지 고려해 확인합니다."""

    try:
        validated = validate_llm_result(result)
    except LlmResultValidationError as error:
        raise DetectionBackendResultError(
            stage=stage,
            deployment_id=deployment_id,
            actual_type=error.actual_type,
        ) from None
    return validated.text


def _serialize_detection_context(
    *,
    regex_candidates: tuple[RegexCandidate, ...],
    ner_candidates: tuple[NerCandidate, ...],
    organization_profile: OrganizationProfile | None,
    source_type: SourceType,
    enabled_policy_ids: tuple[PolicyId, ...],
) -> str:
    """활성 정책과 검증된 후보를 Prompt용 JSON으로 변환합니다."""

    payload = {
        "enabledPolicies": [
            {
                "policyId": policy_id,
                "type": DETECTION_TYPE_BY_POLICY_ID[policy_id],
            }
            for policy_id in enabled_policy_ids
        ],
        "organizationProfile": (
            organization_profile.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
            if organization_profile is not None
            else None
        ),
        "sourceType": source_type,
        "regexCandidates": [
            candidate.model_dump(by_alias=True, mode="json")
            for candidate in regex_candidates
        ],
        "nerCandidates": [
            candidate.model_dump(by_alias=True, mode="json")
            for candidate in ner_candidates
        ],
    }
    return dump_canonical_json_utf8(payload).decode("utf-8")


def _require_detection_context_size(
    *,
    organization_profile: OrganizationProfile | None,
    source_type: SourceType,
) -> None:
    """조직 Context와 출처를 Backend 실행 전에 UTF-8 byte로 제한합니다."""

    encoded = dump_canonical_json_utf8(
        {
            "organizationProfile": (
                organization_profile.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_none=True,
                )
                if organization_profile is not None
                else None
            ),
            "sourceType": source_type,
        }
    )
    actual_bytes = len(encoded)
    if actual_bytes > DEFAULT_MAX_DETECTION_CONTEXT_BYTES:
        raise PromptContextTooLargeError(
            actual_bytes,
            DEFAULT_MAX_DETECTION_CONTEXT_BYTES,
        )


def _regex_candidate_to_detection(
    candidate: RegexCandidate,
) -> Detection:
    """검증된 Regex 후보에서 type과 source가 고정된 내부 Detection을 만듭니다."""

    if type(candidate) is not RegexCandidate:
        raise TypeError("regexCandidates 항목은 RegexCandidate여야 합니다")
    return Detection(
        start=candidate.start,
        end=candidate.end,
        text=candidate.text,
        type=DETECTION_TYPE_BY_POLICY_ID[candidate.policy_id],
        policyId=candidate.policy_id,
        source="regex",
        score=candidate.score,
    )


def _build_ner_candidates(
    detections: tuple[Detection, ...],
) -> tuple[NerCandidate, ...]:
    """검증·중복 제거된 NER 결과에 결정적인 N 접두사 ID를 부여합니다."""

    candidates: list[NerCandidate] = []
    for index, detection in enumerate(detections):
        if (
            type(detection) is not Detection
            or detection.source != "ner"
            or detection.policy_id not in ENTITY_TYPE_BY_POLICY_ID
        ):
            raise DetectionSpanValidationError(
                "INVALID_ITEM",
                item_index=index,
            )
        candidates.append(
            NerCandidate(
                candidateId=f"N{index + 1:03d}",
                start=detection.start,
                end=detection.end,
                text=detection.text,
                policyId=detection.policy_id,
                entityType=ENTITY_TYPE_BY_POLICY_ID[detection.policy_id],
                score=detection.score,
            )
        )
    return tuple(candidates)


def _require_candidate_decision_coverage(
    parsed_output: object,
    *,
    candidate_ids: tuple[str, ...],
) -> tuple[
    tuple[LlmCandidateDecision, ...],
    tuple[LlmDetectionCandidate, ...],
]:
    """모든 입력 후보에 정확히 한 판정이 있는지 검증하고 순서를 복원합니다."""

    if type(parsed_output) is not LlmDetectionOutput:
        del parsed_output
        raise LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL"
        )
    if (
        type(parsed_output.candidate_decisions) is not tuple
        or type(parsed_output.new_detections) is not tuple
    ):
        del parsed_output
        raise LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_INVALID_TOP_LEVEL"
        )

    raw_decisions = parsed_output.candidate_decisions
    if len(raw_decisions) != len(candidate_ids):
        del parsed_output, raw_decisions
        raise LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH"
        )

    expected = frozenset(candidate_ids)
    by_id: dict[str, LlmCandidateDecision] = {}
    for index, raw_decision in enumerate(raw_decisions):
        if type(raw_decision) is not LlmCandidateDecision:
            del parsed_output, raw_decisions, by_id
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_DECISION",
                item_index=index,
            )
        try:
            decision = LlmCandidateDecision.model_validate(
                {
                    "candidateId": raw_decision.candidate_id,
                    "decision": raw_decision.decision,
                }
            )
        except (AttributeError, ValidationError):
            del parsed_output, raw_decisions, by_id
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_INVALID_DECISION",
                item_index=index,
            ) from None
        if (
            decision.candidate_id not in expected
            or decision.candidate_id in by_id
        ):
            del parsed_output, raw_decisions, by_id, decision
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH",
                item_index=index,
            )
        by_id[decision.candidate_id] = decision

    if frozenset(by_id) != expected:
        del parsed_output, raw_decisions, by_id
        raise LlmDetectionOutputError(
            "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH"
        )

    ordered = tuple(by_id[candidate_id] for candidate_id in candidate_ids)
    new_detections = parsed_output.new_detections
    del parsed_output, raw_decisions, by_id
    return ordered, new_detections


def _build_candidate_decision_responses(
    *,
    regex_candidates: tuple[RegexCandidate, ...],
    ner_candidates: tuple[NerCandidate, ...],
    decisions: tuple[LlmCandidateDecision, ...],
) -> tuple[CandidateDecision, ...]:
    """검증된 후보 원본에 LLM의 최소 판정만 결합합니다."""

    decision_by_id: dict[str, CandidateDecisionValue] = {
        decision.candidate_id: decision.decision
        for decision in decisions
    }
    responses: list[CandidateDecision] = []
    for candidate in regex_candidates:
        responses.append(
            RegexCandidateDecision(
                candidateId=candidate.candidate_id,
                source="regex",
                start=candidate.start,
                end=candidate.end,
                text=candidate.text,
                policyId=candidate.policy_id,
                detailType=candidate.detail_type,
                score=candidate.score,
                decision=decision_by_id[candidate.candidate_id],
            )
        )
    for candidate in ner_candidates:
        responses.append(
            NerCandidateDecision(
                candidateId=candidate.candidate_id,
                source="ner",
                start=candidate.start,
                end=candidate.end,
                text=candidate.text,
                policyId=candidate.policy_id,
                entityType=candidate.entity_type,
                score=candidate.score,
                decision=decision_by_id[candidate.candidate_id],
            )
        )
    return tuple(responses)


def _ner_candidate_decision_to_detection(
    decision: NerCandidateDecision,
) -> Detection:
    """확정된 NER 후보 판정을 공통 Detection으로 변환합니다."""

    if (
        type(decision) is not NerCandidateDecision
        or decision.decision != "CONFIRMED"
    ):
        raise TypeError("CONFIRMED NER 후보 판정만 변환할 수 있습니다")
    return Detection(
        start=decision.start,
        end=decision.end,
        text=decision.text,
        type=DETECTION_TYPE_BY_POLICY_ID[decision.policy_id],
        policyId=decision.policy_id,
        source="ner",
        score=decision.score,
    )


def _require_enabled_llm_detections(
    detections: tuple[Detection, ...],
    *,
    enabled_policy_ids: tuple[PolicyId, ...],
) -> None:
    """Schema를 무시한 LLM의 비활성 정책 출력을 최종 거부합니다."""

    enabled = frozenset(enabled_policy_ids)
    for index, detection in enumerate(detections):
        if detection.policy_id not in enabled:
            raise LlmDetectionOutputError(
                "LLM_DETECTION_OUTPUT_POLICY_DISABLED",
                item_index=index,
            )


__all__ = [
    "DEFAULT_MAX_DETECTION_CONTEXT_BYTES",
    "DETECTION_REASONING_EFFORT",
    "DetectionBackendResultError",
    "DetectionPipeline",
    "DetectionPolicySelectionError",
    "DetectionStage",
    "PolicySettingsProvider",
]
