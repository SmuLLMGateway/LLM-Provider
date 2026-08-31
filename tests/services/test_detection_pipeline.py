"""직접 선택한 NER 후 LLM 추가 탐지 파이프라인을 통합 검증합니다."""

from __future__ import annotations

import json

import pytest

from app.backends.provider_registry import (
    BackendProviderLookupError,
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.policies.span_validator import DetectionSpanValidationError
from app.policies.organization_profile_validator import (
    OrganizationProfilePolicyError,
)
from app.registry.deployment_resolver import (
    DeploymentResolutionError,
    DeploymentResolver,
)
from app.registry.execution_plan import DetectionExecutionPlan
from app.registry.snapshot import ActiveRegistrySnapshot
from app.prompts.mask_prompt_artifact import MaskPromptArtifact
from app.prompts.policy_prompt_catalog import (
    COMMON_LLM_POLICY_IDS,
    PolicyPromptCatalog,
)
from app.prompts.prompt_artifact import PromptArtifact
from app.prompts.prompt_errors import PromptContextTooLargeError
from app.prompts.title_prompt_artifact import TitlePromptArtifact
from app.prompts.prompt_renderer import PromptRenderer
from app.schemas.detection import (
    ALLOWED_DETECTION_TYPES,
    ALLOWED_POLICY_IDS,
    DETECTION_TYPE_BY_POLICY_ID,
    DetectResponse,
    Detection,
    RegexCandidate,
    policy_id_for_detection_type,
)
from app.schemas.generation import LlmResult
from app.schemas.detection_context import (
    OrganizationProfile,
    SourceType,
)
from app.schemas.policy_settings import PolicySettings
from app.schemas.registry import DeploymentConfig, RegistryConfig
from app.services.detection_pipeline import (
    DETECTION_REASONING_EFFORT,
    DetectionBackendResultError,
    DetectionPipeline,
    DetectionPolicySelectionError,
)
from app.services.llm_detection_output_parser import (
    LlmDetectionOutput,
    LlmDetectionOutputError,
    LlmDetectionOutputParser,
)


_TEXT = "홍길동은 프로젝트 알파를 담당합니다."
_NER_ID = "ner-entity"
_LLM_ID = "llm-context"
_ORGANIZATION_PROFILE = OrganizationProfile.model_validate(
    {
        "organization": {
            "name": "ABC 주식회사",
            "aliases": ["ABC"],
            "type": "PRIVATE",
        },
        "publicContext": {
            "domains": ["abc.com"],
            "entities": [],
        },
        "privacy": {
            "personNameScope": True,
            "persons": [
                {"name": "홍길동", "aliases": ["길동"]},
            ],
        },
        "securityContext": {
            "internalIpRanges": [],
            "internalDomains": [],
            "internalSystems": [],
            "cloudAssets": [],
            "securityAssets": [],
            "protectTestLogs": True,
        },
        "confidentialTechnologyContext": {"assets": []},
        "thirdPartyContext": {"entities": []},
    }
)
_SOURCE_TYPE: SourceType = "CHAT_TEXT"
_MAIN_SELECTED_POLICY_IDS = ALLOWED_POLICY_IDS


def _mask_prompt(renderer: PromptRenderer) -> MaskPromptArtifact:
    """탐지 Snapshot에 필요한 정적 마스킹 Prompt를 만듭니다."""

    return MaskPromptArtifact.compile(
        "탐지 구간을 마스킹하십시오.",
        renderer=renderer,
        template_path="<mask-test>",
    )


class SwappingSnapshotManager:
    """Snapshot 반환 직후 활성 Snapshot을 교체하는 테스트 대역입니다."""

    def __init__(
        self,
        captured: ActiveRegistrySnapshot,
        replacement: ActiveRegistrySnapshot | None = None,
    ) -> None:
        self.active_snapshot = captured
        self.replacement = replacement or captured
        self.capture_calls = 0

    def capture(self) -> ActiveRegistrySnapshot:
        """현재 Snapshot을 반환한 뒤 교체 후보를 활성화합니다."""

        self.capture_calls += 1
        captured = self.active_snapshot
        self.active_snapshot = self.replacement
        return captured


class StaticPolicySettingsProvider:
    """요청마다 같은 불변 활성 정책 설정을 반환하는 테스트 대역입니다."""

    def __init__(self, enabled_policy_ids: tuple[str, ...]) -> None:
        self.settings = PolicySettings.model_validate(
            {"enabledPolicies": enabled_policy_ids}
        )
        self.capture_calls = 0

    def capture(self) -> PolicySettings:
        """설정 조회 횟수를 기록하고 같은 객체를 반환합니다."""

        self.capture_calls += 1
        return self.settings


class ContextDefaultDetectionPipeline(DetectionPipeline):
    """대부분의 기존 테스트에 안전한 조직 Context와 출처를 제공하는 대역입니다."""

    async def detect(
        self,
        *,
        text: str,
        ner_deployment_id: str,
        llm_deployment_id: str,
        organization_profile: OrganizationProfile = _ORGANIZATION_PROFILE,
        source_type: SourceType = _SOURCE_TYPE,
        regex_candidates: tuple[RegexCandidate, ...] = (),
    ) -> DetectResponse:
        """명시하지 않은 테스트 Context만 고정 기본값으로 보완합니다."""

        return await super().detect(
            text=text,
            ner_deployment_id=ner_deployment_id,
            llm_deployment_id=llm_deployment_id,
            organization_profile=organization_profile,
            source_type=source_type,
            regex_candidates=regex_candidates,
        )


class StaticExecutionPlanResolver(DeploymentResolver):
    """미리 조립된 실행 계획을 반환하고 호출 인자를 기록합니다."""

    def __init__(self, plan: DetectionExecutionPlan) -> None:
        self.plan = plan
        self.calls: list[
            tuple[str, str, ActiveRegistrySnapshot]
        ] = []

    def resolve_detection(
        self,
        *,
        ner_deployment_id: str,
        llm_deployment_id: str,
        snapshot: ActiveRegistrySnapshot,
    ) -> DetectionExecutionPlan:
        """Pipeline이 전달한 Deployment ID와 Snapshot을 기록합니다."""

        self.calls.append(
            (ner_deployment_id, llm_deployment_id, snapshot)
        )
        return self.plan


class RecordingNerBackend:
    """NER 호출과 순서를 기록하고 예약된 결과 또는 오류를 반환합니다."""

    def __init__(
        self,
        result: object,
        *,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.events = events
        self.calls: list[tuple[str, DeploymentConfig]] = []

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> object:
        """NER 호출 정보를 기록한 뒤 예약된 동작을 수행합니다."""

        self.calls.append((text, deployment))
        if self.events is not None:
            self.events.append("ner")
        if self.error is not None:
            raise self.error
        return self.result


class RecordingLlmBackend:
    """LLM 호출과 순서를 기록하고 예약된 결과 또는 오류를 반환합니다."""

    def __init__(
        self,
        result: object,
        *,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.events = events
        self.calls: list[
            tuple[
                list[dict[str, object]],
                DeploymentConfig,
                dict[str, object],
                dict[str, object] | None,
            ]
        ] = []

    async def generate(
        self,
        messages: list[dict[str, object]],
        deployment: DeploymentConfig,
        parameters: dict[str, object],
        output_schema: dict[str, object] | None = None,
    ) -> object:
        """LLM 호출 정보를 기록한 뒤 예약된 동작을 수행합니다."""

        self.calls.append(
            (
                [dict(message) for message in messages],
                deployment,
                dict(parameters),
                None if output_schema is None else dict(output_schema),
            )
        )
        if self.events is not None:
            self.events.append("llm")
        if self.error is not None:
            raise self.error
        return self.result


class SequencedLlmBackend(RecordingLlmBackend):
    """호출 순서대로 서로 다른 LLM 결과를 반환하는 테스트 대역입니다."""

    def __init__(self, results: tuple[object, ...]) -> None:
        if not results:
            raise ValueError("results는 비어 있을 수 없습니다")
        super().__init__(results[0])
        self.results = results

    async def generate(
        self,
        messages: list[dict[str, object]],
        deployment: DeploymentConfig,
        parameters: dict[str, object],
        output_schema: dict[str, object] | None = None,
    ) -> object:
        """현재 호출 순서의 예약 결과를 기록과 함께 반환합니다."""

        call_index = len(self.calls)
        if call_index >= len(self.results):
            raise AssertionError("예약된 LLM 결과보다 많이 호출됐습니다")
        self.result = self.results[call_index]
        return await super().generate(
            messages,
            deployment,
            parameters,
            output_schema,
        )


class StaticOutputParser:
    """주입된 Parser 결과를 반환하고 입력 문자열을 기록합니다."""

    max_detections = 1_000

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    def parse(self, output: str) -> object:
        """모델 출력 문자열을 기록하고 예약된 값을 반환합니다."""

        self.calls.append(output)
        return self.result


def _assert_pipeline_error_does_not_expose(
    error: BaseException,
    secret: str,
) -> None:
    """공개 오류와 Pipeline frame에 민감한 값이 남지 않는지 확인합니다."""

    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))

        exposed = (str(current), repr(current), repr(vars(current)))
        assert all(secret not in value for value in exposed)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)

    traceback = error.__traceback__
    while traceback is not None:
        filename = traceback.tb_frame.f_code.co_filename.replace("\\", "/")
        if filename.endswith("/app/services/detection_pipeline.py"):
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def _detection(
    text: str,
    needle: str,
    *,
    detection_type: str,
    source: str,
    score: float = 0.9,
) -> Detection:
    """원문에서 실제 문자열 위치를 사용해 유효한 Detection을 만듭니다."""

    start = text.index(needle)
    return Detection.model_validate(
        {
            "start": start,
            "end": start + len(needle),
            "text": needle,
            "type": detection_type,
            "policyId": policy_id_for_detection_type(detection_type),
            "source": source,
            "score": score,
        }
    )


def _regex_candidate(
    text: str,
    needle: str,
    *,
    candidate_id: str = "R001",
    policy_id: str = "P01",
    detail_type: str = "PERSON_NAME",
    score: float = 1.0,
) -> RegexCandidate:
    """원문의 실제 span을 사용하는 미확정 Regex 후보를 만듭니다."""

    start = text.index(needle)
    return RegexCandidate.model_validate(
        {
            "candidateId": candidate_id,
            "start": start,
            "end": start + len(needle),
            "text": needle,
            "policyId": policy_id,
            "detailType": detail_type,
            "score": score,
        }
    )


def _llm_result(
    *detections: Detection,
    decisions: tuple[tuple[str, str], ...] = (),
) -> LlmResult:
    """후보 판정과 좌표 없는 신규 탐지를 LLM 출력 JSON으로 만듭니다."""

    payload = {
        "candidateDecisions": [
            {"candidateId": candidate_id, "decision": decision}
            for candidate_id, decision in decisions
        ],
        "newDetections": [
            {
                "text": detection.text,
                "type": detection.type,
                "score": detection.score,
            }
            for detection in detections
        ],
    }
    return LlmResult(
        text=json.dumps(payload, ensure_ascii=False),
        modelName="test-model",
        finishReason="stop",
    )


def _artifact(source: str) -> PromptArtifact:
    """테스트 Snapshot에 넣을 고정 문맥 탐지 Prompt를 컴파일합니다."""

    renderer = PromptRenderer()
    return PromptArtifact.compile(
        source,
        renderer=renderer,
        template_path="<test>",
        policy_prompts=PolicyPromptCatalog.compile(
            {
                policy_id: f"TEST_POLICY_RULE_{policy_id}"
                for policy_id in ALLOWED_POLICY_IDS
            },
            source_path="<test-policy-prompts>",
            max_output_bytes=renderer.limits.max_output_bytes,
        ),
    )


def _title_artifact() -> TitlePromptArtifact:
    """Snapshot 완전성에 필요한 정적 제목 Prompt를 컴파일합니다."""

    return TitlePromptArtifact.compile(
        "제목만 생성하십시오.",
        renderer=PromptRenderer(),
        template_path="<title-test>",
    )


def _snapshot(*, marker: str = "old") -> ActiveRegistrySnapshot:
    """직접 선택할 수 있는 NER와 후속 LLM Deployment를 만듭니다."""

    registry = RegistryConfig.model_validate(
        {
            "deployments": {
                "ner-entity": {
                    "kind": "ner",
                    "adapterType": "test_ner",
                    "modelName": f"ner-{marker}",
                    "enabled": True,
                },
                "ner-entity-b": {
                    "kind": "ner",
                    "adapterType": "test_ner",
                    "modelName": f"ner-b-{marker}",
                    "enabled": True,
                },
                "llm-context": {
                    "kind": "llm",
                    "adapterType": "test_context_llm",
                    "baseUrl": "http://localhost:9200/v1",
                    "modelName": f"context-{marker}",
                    "timeoutMs": 5000,
                    "enabled": True,
                },
                "llm-context-b": {
                    "kind": "llm",
                    "adapterType": "test_context_llm",
                    "baseUrl": "http://localhost:9300/v1",
                    "modelName": f"context-b-{marker}",
                    "timeoutMs": 5000,
                    "enabled": True,
                },
            },
        }
    )
    context_prompt = _artifact(
        (
            f"CONTEXT-{marker}: {{{{ text }}}}\n"
            "EVIDENCE: {{ existing_detections }}"
        )
    )
    return ActiveRegistrySnapshot(
        snapshot_id=f"snapshot-{marker}",
        deployments=registry.deployments,
        detection_prompt=context_prompt,
        mask_prompt=_mask_prompt(PromptRenderer()),
        title_prompt=_title_artifact(),
    )


def _providers(
    *,
    ner: object | None,
    context_llm: object | None,
) -> BackendProviderRegistry:
    """전달된 NER와 문맥 LLM만 Provider Registry에 등록합니다."""

    registrations: list[BackendProviderRegistration] = []
    if ner is not None:
        registrations.append(
            BackendProviderRegistration(
                kind="ner",
                adapter_type="test_ner",
                provider=ner,
            )
        )
    if context_llm is not None:
        registrations.append(
            BackendProviderRegistration(
                kind="llm",
                adapter_type="test_context_llm",
                provider=context_llm,
            )
        )
    return BackendProviderRegistry(registrations)


def _pipeline(
    manager: SwappingSnapshotManager,
    providers: BackendProviderRegistry,
    *,
    deployment_resolver: DeploymentResolver | None = None,
    output_parser: object | None = None,
    enabled_policy_ids: tuple[str, ...] | None = None,
) -> DetectionPipeline:
    """실제 Resolver와 선택한 협력 객체로 DetectionPipeline을 조립합니다."""

    return ContextDefaultDetectionPipeline(
        registry_manager=manager,
        deployment_resolver=(
            deployment_resolver or DeploymentResolver()
        ),
        backend_providers=providers,
        output_parser=output_parser,  # type: ignore[arg-type]
        policy_settings_provider=(
            None
            if enabled_policy_ids is None
            else StaticPolicySettingsProvider(enabled_policy_ids)
        ),
    )


@pytest.mark.asyncio
async def test_detect_runs_ner_then_llm_and_passes_all_evidence() -> None:
    """Regex와 NER 결과를 근거로 전달한 뒤 LLM 추가 결과를 합칩니다."""

    snapshot = _snapshot()
    events: list[str] = []
    regex_candidate = _regex_candidate(
        _TEXT,
        "프로젝트 알파",
        policy_id="B01",
        detail_type="PROJECT_CODE",
    )
    entity = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )
    contextual = _detection(
        _TEXT,
        "담당",
        detection_type="PERSONAL_IDENTITY",
        source="llm",
    )
    ner = RecordingNerBackend([entity], events=events)
    context_llm = RecordingLlmBackend(
        _llm_result(
            contextual,
            decisions=(
                ("N001", "CONFIRMED"),
                ("R001", "CONFIRMED"),
            ),
        ),
        events=events,
    )
    response = await _pipeline(
        SwappingSnapshotManager(snapshot),
        _providers(ner=ner, context_llm=context_llm),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        regex_candidates=(regex_candidate,),
    )

    assert isinstance(response, DetectResponse)
    assert response.detections == (entity, contextual)
    assert [
        decision.model_dump(by_alias=True, mode="json")
        for decision in response.candidate_decisions
    ] == [
        {
            **regex_candidate.model_dump(by_alias=True, mode="json"),
            "source": "regex",
            "decision": "CONFIRMED",
        },
        {
            "candidateId": "N001",
            "source": "ner",
            "start": entity.start,
            "end": entity.end,
            "text": entity.text,
            "policyId": "P01",
            "entityType": "PERSON",
            "score": entity.score,
            "decision": "CONFIRMED",
        },
    ]
    assert events == ["ner", "llm"]
    assert ner.calls == [(_TEXT, snapshot.deployments["ner-entity"])]

    expected_context = {
        "enabledPolicies": [
            {
                "policyId": policy_id,
                "type": DETECTION_TYPE_BY_POLICY_ID[policy_id],
            }
            for policy_id in _MAIN_SELECTED_POLICY_IDS
        ],
        "organizationProfile": _ORGANIZATION_PROFILE.model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
        ),
        "sourceType": _SOURCE_TYPE,
        "regexCandidates": [
            regex_candidate.model_dump(by_alias=True, mode="json")
        ],
        "nerCandidates": [
            {
                "candidateId": "N001",
                "start": entity.start,
                "end": entity.end,
                "text": entity.text,
                "policyId": entity.policy_id,
                "entityType": "PERSON",
                "score": entity.score,
            }
        ],
    }
    expected_evidence = json.dumps(
        expected_context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    messages, deployment, parameters, output_schema = context_llm.calls[0]
    assert deployment is snapshot.deployments["llm-context"]
    assert parameters == {
        "temperature": 0,
        "reasoning_effort": DETECTION_REASONING_EFFORT,
    }
    assert output_schema is not None
    assert messages[0]["role"] == "user"
    assert expected_evidence in str(messages[0]["content"])
    assert json.loads(expected_evidence) == expected_context
    assert '"candidateId":"R001"' in expected_evidence
    assert '"detailType":"PROJECT_CODE"' in expected_evidence
    assert '"candidateId":"N001"' in expected_evidence
    assert '"entityType":"PERSON"' in expected_evidence
    assert '"persons":[{"aliases":["길동"],"name":"홍길동"}]' in (
        expected_evidence
    )
    rendered = str(messages[0]["content"])
    for policy_id in _MAIN_SELECTED_POLICY_IDS:
        assert rendered.count(f"[Policy {policy_id}]") == 1
        assert f"TEST_POLICY_RULE_{policy_id}" in rendered
    assert (
        output_schema["properties"]["candidateDecisions"]["items"]
        ["properties"]["candidateId"]["enum"]
        == ["R001", "N001"]
    )


@pytest.mark.asyncio
async def test_rejected_and_uncertain_candidates_are_not_auto_detected() -> None:
    """REJECTED와 UNCERTAIN은 응답에 보존하되 자동 탐지에 포함하지 않습니다."""

    regex = _regex_candidate(
        _TEXT,
        "프로젝트 알파",
        policy_id="B01",
        detail_type="PROJECT_CODE",
    )
    entity = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([entity]),
            context_llm=RecordingLlmBackend(
                _llm_result(
                    decisions=(
                        ("R001", "REJECTED"),
                        ("N001", "UNCERTAIN"),
                    )
                )
            ),
        ),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        regex_candidates=(regex,),
    )

    assert response.detections == ()
    assert [
        decision.decision
        for decision in response.candidate_decisions
    ] == ["REJECTED", "UNCERTAIN"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decisions",
    [
        (),
        (("R999", "CONFIRMED"),),
    ],
)
async def test_candidate_decisions_must_cover_exact_input_ids(
    decisions: tuple[tuple[str, str], ...],
) -> None:
    """후보 판정 누락과 입력에 없는 ID를 전체 출력 오류로 거부합니다."""

    regex = _regex_candidate(_TEXT, "홍길동")

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend(
                    _llm_result(decisions=decisions)
                ),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
            regex_candidates=(regex,),
        )

    assert error_info.value.code == (
        "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH"
    )


@pytest.mark.asyncio
async def test_candidate_decisions_reject_duplicate_id() -> None:
    """같은 후보 ID에 여러 판정을 반환하면 전체 출력을 거부합니다."""

    regex = _regex_candidate(
        _TEXT,
        "프로젝트 알파",
        policy_id="B01",
        detail_type="PROJECT_CODE",
    )
    entity = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([entity]),
                context_llm=RecordingLlmBackend(
                    _llm_result(
                        decisions=(
                            ("R001", "CONFIRMED"),
                            ("R001", "REJECTED"),
                        )
                    )
                ),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
            regex_candidates=(regex,),
        )

    assert error_info.value.code == (
        "LLM_DETECTION_OUTPUT_CANDIDATE_DECISIONS_MISMATCH"
    )
    assert error_info.value.item_index == 1


@pytest.mark.asyncio
async def test_ner_candidates_get_deterministic_ids_after_sorting() -> None:
    """NER 결과 순서와 무관하게 원문 순서대로 N 접두사 ID를 부여합니다."""

    text = "서울에서 김철수가 일합니다."
    person = _detection(
        text,
        "김철수",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )
    location = _detection(
        text,
        "서울",
        detection_type="LOCATION",
        source="ner",
    )
    context_llm = RecordingLlmBackend(
        _llm_result(
            decisions=(
                ("N001", "CONFIRMED"),
                ("N002", "CONFIRMED"),
            )
        )
    )

    await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([person, location]),
            context_llm=context_llm,
        ),
    ).detect(
        text=text,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    content = str(context_llm.calls[0][0][0]["content"])
    serialized_context = content.split("EVIDENCE: ", maxsplit=1)[1].split(
        "\n\n<policy_instructions>",
        maxsplit=1,
    )[0]
    context = json.loads(serialized_context)
    assert context["nerCandidates"] == [
        {
            "candidateId": "N001",
            "start": location.start,
            "end": location.end,
            "text": "서울",
            "policyId": "P04",
            "entityType": "LOCATION",
            "score": location.score,
        },
        {
            "candidateId": "N002",
            "start": person.start,
            "end": person.end,
            "text": "김철수",
            "policyId": "P01",
            "entityType": "PERSON",
            "score": person.score,
        },
    ]


@pytest.mark.asyncio
async def test_ner_candidate_rejects_policy_outside_ner_responsibility() -> None:
    """NER가 P01·P04 외 정책을 반환하면 LLM에 넘기지 않고 거부합니다."""

    invalid = _detection(
        _TEXT,
        "프로젝트 알파",
        detection_type="PERSONAL",
        source="ner",
    )
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(DetectionSpanValidationError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([invalid]),
                context_llm=context_llm,
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "INVALID_ITEM"
    assert error_info.value.item_index == 0
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_llm_detection_uses_strict_output_schema() -> None:
    """LLM 추가 탐지가 고정 타입과 필드만 허용하는 Schema를 전달합니다."""

    snapshot = _snapshot()
    context_llm = RecordingLlmBackend(_llm_result())
    output_parser = LlmDetectionOutputParser(max_detections=7)

    await _pipeline(
        SwappingSnapshotManager(snapshot),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=context_llm,
        ),
        output_parser=output_parser,
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    _, _, parameters, output_schema = context_llm.calls[0]
    assert parameters == {
        "temperature": 0,
        "reasoning_effort": DETECTION_REASONING_EFFORT,
    }
    assert output_schema == {
        "type": "object",
        "properties": {
            "candidateDecisions": {
                "type": "array",
                "minItems": 0,
                "maxItems": 0,
                "items": {
                    "type": "object",
                    "properties": {
                        "candidateId": {
                            "type": "string",
                            "pattern": r"^[RN][0-9]{3,}$",
                        },
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
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "type": {
                            "type": "string",
                            "enum": [
                                DETECTION_TYPE_BY_POLICY_ID[policy_id]
                                for policy_id in COMMON_LLM_POLICY_IDS
                            ],
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


@pytest.mark.asyncio
async def test_enabled_policies_filter_ner_and_limit_llm_context() -> None:
    """비활성 NER 결과를 제외하고 Prompt와 Schema를 활성 정책으로 제한합니다."""

    entity = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )
    context_llm = RecordingLlmBackend(_llm_result())

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([entity]),
            context_llm=context_llm,
        ),
        enabled_policy_ids=("B01",),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == ()
    messages, _, _, output_schema = context_llm.calls[0]
    assert output_schema is not None
    assert (
        output_schema["properties"]["newDetections"]["items"]
        ["properties"]["type"]["enum"]
        == ["PERSONAL"]
    )
    assert (
        '"enabledPolicies":[{"policyId":"B01","type":"PERSONAL"}]'
        in str(messages[0]["content"])
    )
    assert '"regexCandidates":[]' in str(messages[0]["content"])
    assert '"nerCandidates":[]' in str(messages[0]["content"])


@pytest.mark.asyncio
async def test_organization_profile_requires_active_policy_sections() -> None:
    """활성 정책별 필수 조직 섹션이 없으면 Backend 실행 전에 거부합니다."""

    incomplete = OrganizationProfile.model_validate(
        {
            "organization": {
                "name": "ABC 주식회사",
                "aliases": [],
                "type": "PRIVATE",
            },
            "publicContext": {
                "domains": ["abc.com"],
                "entities": [],
            },
        }
    )
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(OrganizationProfilePolicyError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(ner=ner, context_llm=context_llm),
            enabled_policy_ids=(
                "P01",
                "S02",
                "S03",
                "B01",
                "B02",
                "B03",
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
            organization_profile=incomplete,
        )

    assert error_info.value.missing_sections == (
        "privacy",
        "securityContext",
        "thirdPartyContext",
        "confidentialTechnologyContext",
    )
    assert ner.calls == []
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_omitted_organization_profile_is_allowed_and_explicitly_null() -> None:
    """조직 기준정보가 없으면 전 정책 활성 상태에서도 null Context로 실행합니다."""

    context_llm = RecordingLlmBackend(_llm_result())

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=context_llm,
        ),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        organization_profile=None,
    )

    assert response == DetectResponse()
    rendered = str(context_llm.calls[0][0][0]["content"])
    assert '"organizationProfile":null' in rendered


@pytest.mark.asyncio
async def test_organization_context_byte_limit_stops_before_backend() -> None:
    """과도한 조직 기준정보는 NER·LLM 호출 전에 413 계열 오류로 거부합니다."""

    profile_data = _ORGANIZATION_PROFILE.model_dump(
        by_alias=True,
        mode="json",
    )
    profile_data["publicContext"]["entities"] = [
        {
            "name": f"PUBLIC_ENTITY_{index}",
            "aliases": [],
            "type": "SYSTEM",
            "publicScope": "가" * 2000,
        }
        for index in range(100)
    ]
    oversized = OrganizationProfile.model_validate(profile_data)
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(PromptContextTooLargeError):
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(ner=ner, context_llm=context_llm),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
            organization_profile=oversized,
        )

    assert ner.calls == []
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_ocr_source_type_is_serialized_into_prompt() -> None:
    """Gateway가 알려준 OCR 출처를 Prompt Context에 그대로 보존합니다."""

    context_llm = RecordingLlmBackend(_llm_result())

    await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=context_llm,
        ),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        source_type="OCR_TEXT",
    )

    rendered = str(context_llm.calls[0][0][0]["content"])
    assert '"sourceType":"OCR_TEXT"' in rendered


@pytest.mark.asyncio
async def test_only_enabled_policy_is_added_to_prompt_and_schema() -> None:
    """활성 정책이 하나면 후보 유무와 관계없이 해당 정책만 사용합니다."""

    regex = _regex_candidate(
        _TEXT,
        "홍길동",
        policy_id="P03",
        detail_type="CONTACT_NAME_TEST",
    )
    context_llm = RecordingLlmBackend(
        _llm_result(decisions=(("R001", "REJECTED"),))
    )

    await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=context_llm,
        ),
        enabled_policy_ids=("P03",),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        regex_candidates=(regex,),
    )

    messages, _, _, output_schema = context_llm.calls[0]
    rendered = str(messages[0]["content"])
    assert rendered.count("[Policy P03]") == 1
    assert "TEST_POLICY_RULE_P03" in rendered
    assert "[Policy P05]" not in rendered
    assert output_schema is not None
    assert (
        output_schema["properties"]["newDetections"]["items"]
        ["properties"]["type"]["enum"]
        == ["CONTACT"]
    )


@pytest.mark.asyncio
async def test_active_policy_without_candidate_still_runs_llm() -> None:
    """후보가 없어도 활성 정책은 LLM 누락 탐지 대상으로 사용합니다."""

    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(ner=ner, context_llm=context_llm),
        enabled_policy_ids=("P01",),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response == DetectResponse()
    assert len(ner.calls) == 1
    assert len(context_llm.calls) == 1
    messages, _, _, output_schema = context_llm.calls[0]
    assert "[Policy P01]" in str(messages[0]["content"])
    assert output_schema is not None
    assert (
        output_schema["properties"]["newDetections"]["items"]
        ["properties"]["type"]["enum"]
        == ["PERSONAL_IDENTITY"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "needle", "policy_id", "detection_type"),
    [
        (
            "여권번호는 M12345678입니다.",
            "M12345678",
            "P02",
            "UNIQUE_IDENTITY",
        ),
        (
            "연락처는 010-9876-5432입니다.",
            "010-9876-5432",
            "P03",
            "CONTACT",
        ),
    ],
)
async def test_p02_and_p03_can_detect_without_regex_candidates(
    text: str,
    needle: str,
    policy_id: str,
    detection_type: str,
) -> None:
    """Regex 후보가 없어도 P02·P03 활성 타입으로 신규 탐지할 수 있습니다."""

    expected = _detection(
        text,
        needle,
        detection_type=detection_type,
        source="llm",
    )
    context_llm = RecordingLlmBackend(_llm_result(expected))

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=context_llm,
        ),
        enabled_policy_ids=(policy_id,),
    ).detect(
        text=text,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == (expected,)
    _, _, _, output_schema = context_llm.calls[0]
    assert output_schema is not None
    assert (
        output_schema["properties"]["newDetections"]["items"]
        ["properties"]["type"]["enum"]
        == [detection_type]
    )


@pytest.mark.asyncio
async def test_regex_detection_for_disabled_global_policy_is_rejected() -> None:
    """별도 전역 설정에서 비활성인 Regex 후보는 Backend 실행 전에 거부합니다."""

    regex = _regex_candidate(
        _TEXT,
        "홍길동",
    )
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(DetectionPolicySelectionError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(ner=ner, context_llm=context_llm),
            enabled_policy_ids=("B01",),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
            regex_candidates=(regex,),
        )

    assert error_info.value.item_index == 0
    assert ner.calls == []
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_llm_output_for_disabled_policy_is_rejected() -> None:
    """Backend가 JSON Schema를 무시해도 비활성 정책 출력은 fail-closed입니다."""

    disabled = _detection(
        _TEXT,
        "홍길동",
        detection_type="CONTACT",
        source="llm",
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend(_llm_result(disabled)),
            ),
            enabled_policy_ids=("B01",),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_POLICY_DISABLED"


@pytest.mark.asyncio
async def test_detect_routes_each_requested_deployment_pair() -> None:
    """요청한 두 ID에 해당하는 NER·LLM Deployment를 선택합니다."""

    snapshot = _snapshot()
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    response = await _pipeline(
        SwappingSnapshotManager(snapshot),
        _providers(ner=ner, context_llm=context_llm),
    ).detect(
        text=_TEXT,
        ner_deployment_id="ner-entity-b",
        llm_deployment_id="llm-context-b",
    )

    assert response.detections == ()
    assert ner.calls == [(_TEXT, snapshot.deployments["ner-entity-b"])]
    assert context_llm.calls[0][1] is snapshot.deployments["llm-context-b"]


@pytest.mark.asyncio
async def test_detect_runs_llm_when_ner_returns_empty() -> None:
    """NER가 놓친 경우에도 후속 LLM을 반드시 실행해 추가 탐지합니다."""

    events: list[str] = []
    contextual = _detection(
        _TEXT,
        "프로젝트 알파",
        detection_type="PERSONAL",
        source="llm",
    )
    ner = RecordingNerBackend([], events=events)
    context_llm = RecordingLlmBackend(
        _llm_result(contextual),
        events=events,
    )

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(ner=ner, context_llm=context_llm),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert events == ["ner", "llm"]
    assert response.detections == (contextual,)


@pytest.mark.asyncio
async def test_llm_text_is_expanded_to_every_original_occurrence() -> None:
    """LLM은 문자열만 판단하고 서버가 모든 정확한 원문 좌표를 계산합니다."""

    text = "카드 1234와 카드 1234를 확인합니다."
    first = _detection(
        text,
        "1234",
        detection_type="PAYMENT",
        source="llm",
    )
    second_start = text.index("1234", first.end)
    second = Detection(
        start=second_start,
        end=second_start + len("1234"),
        text="1234",
        type="PAYMENT",
        policyId="P05",
        source="llm",
        score=0.9,
    )

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=RecordingLlmBackend(_llm_result(first)),
        ),
    ).detect(
        text=text,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == (first, second)


@pytest.mark.asyncio
async def test_llm_text_not_found_is_corrected_by_one_retry() -> None:
    """원문 불일치 출력은 한 번 교정 요청하고 정확한 span만 채택합니다."""

    text = "현재 대출 잔액은 1억 2천만 원입니다."
    invalid_result = LlmResult(
        text=json.dumps(
            {
                "candidateDecisions": [],
                "newDetections": [
                    {
                        "text": "대출 잔액 1억 2천만 원",
                        "type": "PERSONAL_FINANCE",
                        "score": 0.95,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    corrected = _detection(
        text,
        "대출 잔액은 1억 2천만 원",
        detection_type="PERSONAL_FINANCE",
        source="llm",
        score=0.95,
    )
    context_llm = SequencedLlmBackend(
        (invalid_result, _llm_result(corrected))
    )

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=context_llm,
        ),
    ).detect(
        text=text,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == (corrected,)
    assert len(context_llm.calls) == 2
    first_messages = context_llm.calls[0][0]
    retry_messages = context_llm.calls[1][0]
    assert len(first_messages) == 1
    assert [message["role"] for message in retry_messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert retry_messages[1]["content"] == invalid_result.text
    correction = str(retry_messages[2]["content"])
    assert "newDetections[0].text" in correction
    assert "조사, 띄어쓰기, 문장부호" in correction
    assert text not in correction
    assert context_llm.calls[0][2:] == context_llm.calls[1][2:]


@pytest.mark.asyncio
async def test_repeated_contextual_llm_text_is_rejected() -> None:
    """문맥형 text가 반복되면 Pipeline이 모든 위치로 임의 확장하지 않습니다."""

    text = "프로젝트 알파와 프로젝트 알파"
    result = LlmResult(
        text=json.dumps(
            {
                "candidateDecisions": [],
                "newDetections": [
                    {
                        "text": "프로젝트 알파",
                        "type": "PERSONAL",
                        "score": 0.9,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    context_llm = RecordingLlmBackend(result)

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=context_llm,
            ),
        ).detect(
            text=text,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS"
    assert error_info.value.item_index == 0
    assert len(context_llm.calls) == 2


@pytest.mark.asyncio
async def test_llm_text_absent_from_original_is_rejected() -> None:
    """LLM이 원문에 없는 문자열을 만들면 서버가 좌표를 추측하지 않습니다."""

    result = LlmResult(
        text=json.dumps(
            {
                "candidateDecisions": [],
                "newDetections": [
                    {
                        "text": "김철수",
                        "type": "PERSONAL_IDENTITY",
                        "score": 0.9,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    context_llm = RecordingLlmBackend(result)

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=context_llm,
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_TEXT_NOT_FOUND"
    assert error_info.value.item_index == 0
    assert len(context_llm.calls) == 2


@pytest.mark.asyncio
async def test_ner_failure_stops_before_llm_execution() -> None:
    """필수 NER 단계가 실패하면 후속 LLM을 호출하지 않습니다."""

    expected = RuntimeError("NER 서버 호출 실패")
    ner = RecordingNerBackend([], error=expected)
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(RuntimeError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(ner=ner, context_llm=context_llm),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value is expected
    assert len(ner.calls) == 1
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_detect_uses_one_captured_snapshot_during_activation_swap() -> None:
    """요청 중 Snapshot이 교체돼도 처음 캡처한 구성만 사용합니다."""

    captured = _snapshot(marker="old")
    replacement = _snapshot(marker="new")
    manager = SwappingSnapshotManager(captured, replacement)
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())
    response = await _pipeline(
        manager,
        _providers(ner=ner, context_llm=context_llm),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == ()
    assert manager.capture_calls == 1
    assert manager.active_snapshot is replacement
    assert ner.calls[0][1] is captured.deployments["ner-entity"]
    assert ner.calls[0][1].model_name == "ner-old"
    assert context_llm.calls[0][1] is captured.deployments["llm-context"]
    assert context_llm.calls[0][1].model_name == "context-old"
    assert "CONTEXT-old:" in str(context_llm.calls[0][0][0]["content"])


@pytest.mark.asyncio
async def test_detect_uses_resources_from_execution_plan() -> None:
    """Pipeline은 Snapshot을 재조회하지 않고 Resolver의 Plan만 소비합니다."""

    captured = _snapshot(marker="snapshot")
    plan_snapshot = _snapshot(marker="plan")
    plan = DeploymentResolver().resolve_detection(
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        snapshot=plan_snapshot,
    )
    resolver = StaticExecutionPlanResolver(plan)
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    response = await _pipeline(
        SwappingSnapshotManager(captured),
        _providers(ner=ner, context_llm=context_llm),
        deployment_resolver=resolver,
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == ()
    assert resolver.calls == [(_NER_ID, _LLM_ID, captured)]
    assert ner.calls[0][1] is plan.ner_deployment.config
    assert context_llm.calls[0][1] is plan.llm_deployment.config
    assert "CONTEXT-plan:" in str(context_llm.calls[0][0][0]["content"])


@pytest.mark.parametrize(
    "missing_argument",
    ["ner_deployment_id", "llm_deployment_id"],
)
def test_detect_requires_both_deployment_ids(
    missing_argument: str,
) -> None:
    """Detection Pipeline은 NER와 LLM ID를 모두 필수로 요구합니다."""

    pipeline = _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([]),
            context_llm=RecordingLlmBackend(_llm_result()),
        ),
    )

    arguments = {
        "text": _TEXT,
        "ner_deployment_id": _NER_ID,
        "llm_deployment_id": _LLM_ID,
    }
    del arguments[missing_argument]

    with pytest.raises(TypeError):
        pipeline.detect(**arguments)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ner_deployment_id", "llm_deployment_id", "expected_id"),
    [
        ("unknown-ner", _LLM_ID, "unknown-ner"),
        (_NER_ID, "unknown-llm", "unknown-llm"),
    ],
)
async def test_unknown_deployment_stops_before_backend_execution(
    ner_deployment_id: str,
    llm_deployment_id: str,
    expected_id: str,
) -> None:
    """알 수 없는 Deployment는 어느 Backend도 실행하지 않습니다."""

    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(DeploymentResolutionError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(ner=ner, context_llm=context_llm),
        ).detect(
            text=_TEXT,
            ner_deployment_id=ner_deployment_id,
            llm_deployment_id=llm_deployment_id,
        )

    assert error_info.value.code == "DEPLOYMENT_NOT_FOUND"
    assert error_info.value.deployment_id == expected_id
    assert ner.calls == []
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_pipeline_rejects_non_regex_candidate_object() -> None:
    """직접 Pipeline 호출도 Detection을 RegexCandidate로 오인하지 않습니다."""

    invalid_existing = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )
    ner = RecordingNerBackend([])
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(TypeError, match="RegexCandidate"):
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(ner=ner, context_llm=context_llm),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
            regex_candidates=(invalid_existing,),  # type: ignore[arg-type]
        )

    assert ner.calls == []
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_ner_result_source_is_enforced() -> None:
    """NER Backend가 다른 source를 반환하면 전체 batch를 거부합니다."""

    wrong_source = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="llm",
    )
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(DetectionSpanValidationError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([wrong_source]),
                context_llm=context_llm,
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "SOURCE_MISMATCH"
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_ner_invalid_duplicate_is_not_hidden_by_resolution() -> None:
    """유효한 중복이 있어도 원문 불일치 항목을 먼저 거부합니다."""

    valid = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )
    invalid = Detection.model_construct(
        start=valid.start,
        end=valid.end,
        text="김철수",
        type=valid.type,
        policy_id=valid.policy_id,
        source="ner",
        score=0.1,
    )
    context_llm = RecordingLlmBackend(_llm_result())

    with pytest.raises(DetectionSpanValidationError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([valid, invalid]),
                context_llm=context_llm,
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "TEXT_MISMATCH"
    assert error_info.value.item_index == 1
    assert context_llm.calls == []


@pytest.mark.asyncio
async def test_ner_rejects_invalid_result_container() -> None:
    """NER Provider가 list 또는 tuple이 아닌 값을 반환하면 거부합니다."""

    with pytest.raises(DetectionSpanValidationError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend({"detections": []}),
                context_llm=RecordingLlmBackend(_llm_result()),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "INVALID_CONTAINER"


@pytest.mark.asyncio
async def test_llm_stage_rejects_non_llm_result() -> None:
    """후속 탐지 LLM이 LlmResult가 아닌 값을 반환하면 거부합니다."""

    with pytest.raises(DetectionBackendResultError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend({"text": "[]"}),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    error = error_info.value
    assert error.stage == "confidential_detection"
    assert error.deployment_id == "llm-context"
    assert error.actual_type is dict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_result", "actual_type"),
    [
        (LlmResult.model_construct(), LlmResult),
        (LlmResult.model_construct(text=123), int),
    ],
)
async def test_llm_stage_revalidates_untrusted_llm_result_text(
    invalid_result: LlmResult,
    actual_type: type[object],
) -> None:
    """검증을 우회한 LlmResult의 text 필드도 다시 검사합니다."""

    with pytest.raises(DetectionBackendResultError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend(invalid_result),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    error = error_info.value
    assert error.stage == "confidential_detection"
    assert error.deployment_id == "llm-context"
    assert error.actual_type is actual_type


@pytest.mark.asyncio
async def test_llm_stage_propagates_parser_error() -> None:
    """후속 LLM의 잘못된 JSON 출력은 Parser 오류로 중단합니다."""

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend(
                    LlmResult(text="```json\n[]\n```")
                ),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_JSON"


@pytest.mark.asyncio
async def test_parser_error_does_not_retain_model_output_in_pipeline() -> None:
    """Parser 오류 전달 시 Pipeline frame에서 모델 원문을 제거합니다."""

    secret = "TOP_SECRET_LLM_OUTPUT_46192"

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend(
                    LlmResult(text=f"not-json-{secret}")
                ),
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    _assert_pipeline_error_does_not_expose(error_info.value, secret)


@pytest.mark.asyncio
async def test_span_error_does_not_retain_original_text_in_pipeline() -> None:
    """Span 오류 전달 시 Pipeline frame에서 사용자 원문을 제거합니다."""

    secret = "TOP_SECRET_PIPELINE_TEXT_72518"
    invalid = Detection.model_construct(
        start=0,
        end=len(secret),
        text="X" * len(secret),
        type="PERSONAL",
        source="ner",
        score=0.5,
    )

    with pytest.raises(DetectionSpanValidationError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([invalid]),
                context_llm=RecordingLlmBackend(_llm_result()),
            ),
        ).detect(
            text=secret,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    _assert_pipeline_error_does_not_expose(error_info.value, secret)


@pytest.mark.asyncio
async def test_parser_return_value_is_validated_as_untrusted_data() -> None:
    """주입 Parser가 후보가 아닌 객체를 반환해도 Resolver에서 거부합니다."""

    wrong_source = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
    )
    parser = StaticOutputParser(
        LlmDetectionOutput(
            candidate_decisions=(),
            new_detections=(wrong_source,),  # type: ignore[arg-type]
        )
    )

    with pytest.raises(LlmDetectionOutputError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=RecordingLlmBackend(_llm_result()),
            ),
            output_parser=parser,
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "LLM_DETECTION_OUTPUT_INVALID_ITEM"
    assert len(parser.calls) == 1


@pytest.mark.asyncio
async def test_llm_duplicate_keeps_ner_and_additional_result() -> None:
    """LLM 중복은 NER이 이기고 LLM의 새로운 탐지는 함께 반환합니다."""

    entity = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
        score=0.2,
    )
    duplicate = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="llm",
        score=1.0,
    )
    additional = _detection(
        _TEXT,
        "프로젝트 알파",
        detection_type="PERSONAL",
        source="llm",
    )

    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([entity]),
            context_llm=RecordingLlmBackend(
                _llm_result(
                    duplicate,
                    additional,
                    decisions=(("N001", "CONFIRMED"),),
                )
            ),
        ),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
    )

    assert response.detections == (entity, additional)


@pytest.mark.asyncio
async def test_existing_regex_suppresses_same_new_detection() -> None:
    """Gateway가 가진 동일 탐지는 LPL 응답에 다시 포함하지 않습니다."""

    regex = _regex_candidate(
        _TEXT,
        "홍길동",
    )
    entity_duplicate = _detection(
        _TEXT,
        "홍길동",
        detection_type="PERSONAL_IDENTITY",
        source="ner",
        score=0.9,
    )

    context_llm = RecordingLlmBackend(
        _llm_result(decisions=(("R001", "CONFIRMED"),))
    )
    response = await _pipeline(
        SwappingSnapshotManager(_snapshot()),
        _providers(
            ner=RecordingNerBackend([entity_duplicate]),
            context_llm=context_llm,
        ),
    ).detect(
        text=_TEXT,
        ner_deployment_id=_NER_ID,
        llm_deployment_id=_LLM_ID,
        regex_candidates=(regex,),
    )

    assert response.detections == ()
    assert '"nerCandidates":[]' in str(
        context_llm.calls[0][0][0]["content"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_provider", "deployment_id"),
    [
        ("ner", "ner-entity"),
        ("context", "llm-context"),
    ],
)
async def test_detect_propagates_stage_provider_lookup_error(
    missing_provider: str,
    deployment_id: str,
) -> None:
    """필수 NER 또는 후속 LLM Provider가 없으면 조회 오류를 전달합니다."""

    providers = _providers(
        ner=None if missing_provider == "ner" else RecordingNerBackend([]),
        context_llm=(
            None
            if missing_provider == "context"
            else RecordingLlmBackend(_llm_result())
        ),
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            providers,
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value.code == "BACKEND_PROVIDER_NOT_REGISTERED"
    assert error_info.value.deployment_id == deployment_id


@pytest.mark.asyncio
async def test_detect_propagates_backend_execution_error() -> None:
    """후속 LLM Backend 실행 예외를 다른 오류로 감추지 않습니다."""

    expected = RuntimeError("문맥 모델 서버 연결 실패")
    context_llm = RecordingLlmBackend(
        _llm_result(),
        error=expected,
    )

    with pytest.raises(RuntimeError) as error_info:
        await _pipeline(
            SwappingSnapshotManager(_snapshot()),
            _providers(
                ner=RecordingNerBackend([]),
                context_llm=context_llm,
            ),
        ).detect(
            text=_TEXT,
            ner_deployment_id=_NER_ID,
            llm_deployment_id=_LLM_ID,
        )

    assert error_info.value is expected
    assert len(context_llm.calls) == 1
