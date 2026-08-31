import pytest
from pydantic import ValidationError

from app.schemas.detection import (
    ALLOWED_DETECTION_TYPES,
    CONTEXTUAL_DETECTION_TYPES,
    DETECTION_TYPE_BY_POLICY_ID,
    MAX_REGEX_CANDIDATES,
    MULTI_OCCURRENCE_DETECTION_TYPES,
    NER_DETECTION_TYPES,
    POLICY_ID_BY_DETECTION_TYPE,
    DetectRequest,
    DetectResponse,
    Detection,
    NerCandidate,
    NerCandidateDecision,
    RegexCandidate,
    RegexCandidateDecision,
)


def _valid_detection_data() -> dict[str, object]:
    """유효한 Detection 모델 입력 데이터를 생성합니다."""
    return {
        "start": 0,
        "end": 3,
        "text": "홍길동",
        "type": "PERSONAL_IDENTITY",
        "policyId": "P01",
        "source": "ner",
        "score": 0.98,
    }


def _valid_regex_candidate_data() -> dict[str, object]:
    """Gateway가 요청에 포함할 수 있는 미확정 Regex 후보를 생성합니다."""

    return {
        "candidateId": "R001",
        "start": 0,
        "end": 3,
        "text": "홍길동",
        "policyId": "P01",
        "detailType": "PERSON_NAME",
        "score": 0.98,
    }


def _valid_ner_candidate_data() -> dict[str, object]:
    """LPL이 NER 결과에서 만들 수 있는 미확정 개체 후보를 생성합니다."""

    return {
        "candidateId": "N001",
        "start": 0,
        "end": 3,
        "text": "홍길동",
        "policyId": "P01",
        "entityType": "PERSON",
        "score": 0.98,
    }


def _valid_detection_context_fields() -> dict[str, object]:
    """DetectRequest에 필요한 조직 Context와 텍스트 출처를 생성합니다."""

    return {
        "organizationProfile": {
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
                "persons": [],
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
        },
        "sourceType": "CHAT_TEXT",
    }


def test_detection_is_valid() -> None:
    """유효한 입력으로 Detection 모델을 생성할 수 있는지 검증합니다."""
    detection = Detection.model_validate(_valid_detection_data())

    assert detection.start == 0
    assert detection.end == 3
    assert detection.text == "홍길동"
    assert detection.type == "PERSONAL_IDENTITY"
    assert detection.policy_id == "P01"
    assert detection.source == "ner"
    assert detection.score == 0.98


@pytest.mark.parametrize("detection_type", ALLOWED_DETECTION_TYPES)
def test_detection_accepts_every_policy_type(
    detection_type: str,
) -> None:
    """정책에서 정의한 14개 코드가 모두 Detection 계약에 포함됩니다."""

    data = _valid_detection_data()
    data["type"] = detection_type
    data["policyId"] = POLICY_ID_BY_DETECTION_TYPE[detection_type]

    assert Detection.model_validate(data).type == detection_type


@pytest.mark.parametrize(
    "detection_type",
    ["PERSON", "UNKNOWN", "contact", " CONTACT", "CONTACT "],
)
def test_detection_rejects_type_outside_policy(
    detection_type: str,
) -> None:
    """목록 밖 코드와 대소문자·공백 변형을 fail-closed로 거부합니다."""

    data = _valid_detection_data()
    data["type"] = detection_type

    with pytest.raises(ValidationError):
        Detection.model_validate(data)


def test_detection_schema_exposes_exact_policy_type_enum() -> None:
    """OpenAPI 기반 클라이언트도 정확한 14개 허용 목록을 알 수 있습니다."""

    schema = Detection.model_json_schema()

    assert tuple(schema["properties"]["type"]["enum"]) == (
        ALLOWED_DETECTION_TYPES
    )
    assert "policyId" in schema["required"]


def test_policy_ids_have_exact_fixed_type_mapping() -> None:
    """14개 Policy ID와 Detection Type의 1:1 매핑을 고정합니다."""

    expected = {
        "PERSONAL_IDENTITY": "P01",
        "UNIQUE_IDENTITY": "P02",
        "CONTACT": "P03",
        "LOCATION": "P04",
        "PAYMENT": "P05",
        "FINANCE_ACCOUNT": "P06",
        "PERSONAL_FINANCE": "P07",
        "SENSITIVE_PERSONAL": "P08",
        "AUTH": "S01",
        "SECURITY_INFRA": "S02",
        "SYSTEM_LOG": "S03",
        "PERSONAL": "B01",
        "CLIENT": "B02",
        "R&D": "B03",
    }

    assert dict(POLICY_ID_BY_DETECTION_TYPE) == expected
    assert dict(DETECTION_TYPE_BY_POLICY_ID) == {
        policy_id: detection_type
        for detection_type, policy_id in expected.items()
    }
    with pytest.raises(TypeError):
        POLICY_ID_BY_DETECTION_TYPE["CONTACT"] = "P01"  # type: ignore[index]


def test_detection_type_responsibility_groups_partition_policy() -> None:
    """반복 가능 7개와 문맥형 7개가 전체 14개 정책을 분할합니다."""

    assert len(ALLOWED_DETECTION_TYPES) == 14
    assert len(NER_DETECTION_TYPES) == 2
    assert len(MULTI_OCCURRENCE_DETECTION_TYPES) == 7
    assert len(CONTEXTUAL_DETECTION_TYPES) == 7
    assert not (
        set(MULTI_OCCURRENCE_DETECTION_TYPES)
        & set(CONTEXTUAL_DETECTION_TYPES)
    )
    assert set(ALLOWED_DETECTION_TYPES) == (
        set(MULTI_OCCURRENCE_DETECTION_TYPES)
        | set(CONTEXTUAL_DETECTION_TYPES)
    )
    assert set(POLICY_ID_BY_DETECTION_TYPE) == set(ALLOWED_DETECTION_TYPES)


def test_detection_requires_policy_id() -> None:
    """Gateway와 공통 Detection은 policyId를 반드시 포함해야 합니다."""

    data = _valid_detection_data()
    del data["policyId"]

    with pytest.raises(ValidationError):
        Detection.model_validate(data)


def test_detection_rejects_policy_id_mismatched_with_type() -> None:
    """type과 고정 매핑이 다른 policyId를 fail-closed로 거부합니다."""

    data = _valid_detection_data()
    data["policyId"] = "P03"

    with pytest.raises(ValidationError, match="고정 정책 ID"):
        Detection.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", -1),
        ("end", 0),
        ("score", -0.01),
        ("score", 1.01),
        ("source", "unknown"),
        ("text", ""),
        ("type", ""),
    ],
)
def test_detection_rejects_invalid_field(
    field: str,
    value: object,
) -> None:
    """Detection 모델이 잘못된 필드 값을 거부하는지 검증합니다."""
    data = _valid_detection_data()
    data[field] = value

    with pytest.raises(ValidationError):
        Detection.model_validate(data)


def test_detection_rejects_reversed_or_empty_span() -> None:
    """Detection 모델이 역전되거나 빈 범위를 거부하는지 검증합니다."""
    data = _valid_detection_data()
    data["start"] = 3
    data["end"] = 3

    with pytest.raises(ValidationError, match="end는 start보다 커야 합니다"):
        Detection.model_validate(data)


def test_detection_rejects_unknown_field() -> None:
    """Detection 모델이 알 수 없는 필드를 거부하는지 검증합니다."""
    data = _valid_detection_data()
    data["unknownField"] = True

    with pytest.raises(ValidationError):
        Detection.model_validate(data)


def test_detect_request_uses_deployment_id_aliases() -> None:
    """DetectRequest가 NER·LLM Deployment ID 별칭을 사용하는지 검증합니다."""
    request = DetectRequest.model_validate(
        {
            **_valid_detection_context_fields(),
            "nerDeploymentId": "ner-a",
            "llmDeploymentId": "llm-a",
            "text": "홍길동은 프로젝트 알파를 담당합니다.",
            "regexCandidates": [_valid_regex_candidate_data()],
        }
    )

    assert request.ner_deployment_id == "ner-a"
    assert request.llm_deployment_id == "llm-a"
    assert request.regex_candidates[0].text == "홍길동"
    assert request.model_dump(by_alias=True, mode="json") == {
        **_valid_detection_context_fields(),
        "nerDeploymentId": "ner-a",
        "llmDeploymentId": "llm-a",
        "text": "홍길동은 프로젝트 알파를 담당합니다.",
        "regexCandidates": [_valid_regex_candidate_data()],
    }


@pytest.mark.parametrize(
    "invalid_data",
    [
        {"text": "Deployment ID 누락 원문"},
        {
            "text": "NER Deployment ID 누락 원문",
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "LLM Deployment ID 누락 원문",
            "nerDeploymentId": "ner-a",
        },
        {
            "text": "null NER Deployment ID 원문",
            "nerDeploymentId": None,
            "llmDeploymentId": "llm-a",
        },
        {
            "text": "null LLM Deployment ID 원문",
            "nerDeploymentId": "ner-a",
            "llmDeploymentId": None,
        },
    ],
)
def test_detect_request_requires_non_null_deployment_ids(
    invalid_data: dict[str, object],
) -> None:
    """탐지 요청은 null이 아닌 NER·LLM Deployment ID를 포함해야 합니다."""

    with pytest.raises(ValidationError):
        DetectRequest.model_validate(invalid_data)


def test_detect_request_rejects_legacy_profile_id() -> None:
    """이전 profileId 선택 방식은 알 수 없는 추가 필드로 거부합니다."""

    with pytest.raises(ValidationError):
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "text": "홍길동",
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "profileId": "profile-a",
            }
        )


def test_detect_request_accepts_regex_candidate() -> None:
    """Gateway의 미확정 Regex 후보를 별도 계약으로 받을 수 있습니다."""

    data = _valid_regex_candidate_data()

    request = DetectRequest.model_validate(
        {
            **_valid_detection_context_fields(),
            "nerDeploymentId": "ner-a",
            "llmDeploymentId": "llm-a",
            "text": "홍길동은 프로젝트 알파를 담당합니다.",
            "regexCandidates": [data],
        }
    )

    candidate = request.regex_candidates[0]
    assert type(candidate) is RegexCandidate
    assert candidate.candidate_id == "R001"
    assert candidate.detail_type == "PERSON_NAME"


@pytest.mark.parametrize(
    ("field", "value"),
    [("source", "regex"), ("type", "CONTACT"), ("candidateId", "N001")],
)
def test_detect_request_rejects_detection_fields_and_ner_candidate_id(
    field: str,
    value: str,
) -> None:
    """Regex 후보는 type/source를 받지 않고 R 접두사 ID만 허용합니다."""

    data = _valid_regex_candidate_data()
    data[field] = value

    with pytest.raises(ValidationError):
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "text": "홍길동은 프로젝트 알파를 담당합니다",
                "regexCandidates": [data],
            }
        )


def test_detect_request_rejects_non_utf8_text() -> None:
    """Prompt로 전달할 수 없는 고립 surrogate 원문을 요청 단계에서 거부합니다."""

    with pytest.raises(ValidationError):
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "text": "\ud800",
            }
        )


def test_detect_request_schema_limits_regex_candidate_count() -> None:
    """OpenAPI Schema에도 Regex 후보의 개수 상한을 공개합니다."""

    schema = DetectRequest.model_json_schema(by_alias=True)

    assert (
        schema["properties"]["regexCandidates"]["maxItems"]
        == MAX_REGEX_CANDIDATES
    )
    assert "nerDeploymentId" in schema["required"]
    assert "llmDeploymentId" in schema["required"]
    assert "organizationProfile" not in schema["required"]
    assert "sourceType" in schema["required"]
    assert schema["properties"]["nerDeploymentId"]["type"] == "string"
    assert schema["properties"]["llmDeploymentId"]["type"] == "string"


@pytest.mark.parametrize("include_null", [False, True])
def test_detect_request_allows_missing_organization_profile(
    include_null: bool,
) -> None:
    """조직 정보를 제공하지 않는 요청도 구조적으로 유효합니다."""

    data = {
        "text": "조직 기준정보가 없는 요청",
        "nerDeploymentId": "ner-a",
        "llmDeploymentId": "llm-a",
        "sourceType": "CHAT_TEXT",
    }
    if include_null:
        data["organizationProfile"] = None

    request = DetectRequest.model_validate(data)

    assert request.organization_profile is None


def test_detect_request_rejects_too_many_regex_candidates() -> None:
    """Regex 후보가 공개 개수 상한을 넘으면 요청을 거부합니다."""

    data = _valid_regex_candidate_data()

    with pytest.raises(ValidationError):
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "text": "홍길동",
                "regexCandidates": [
                    data
                ]
                * (MAX_REGEX_CANDIDATES + 1),
            }
        )


def test_detect_request_isolates_and_freezes_regex_candidates() -> None:
    """원본 리스트 변경이 전파되지 않고 검증된 컬렉션을 변경할 수 없는지 확인합니다."""

    detections = [_valid_regex_candidate_data()]
    request = DetectRequest.model_validate(
        {
            **_valid_detection_context_fields(),
            "nerDeploymentId": "ner-a",
            "llmDeploymentId": "llm-a",
            "text": "홍길동은 프로젝트 알파를 담당합니다.",
            "regexCandidates": detections,
        }
    )

    detections.clear()

    assert isinstance(request.regex_candidates, tuple)
    assert len(request.regex_candidates) == 1
    assert not hasattr(request.regex_candidates, "append")


def test_detect_request_serializes_regex_candidates_as_json_array() -> None:
    """내부 tuple이 별칭을 사용한 JSON 직렬화에서는 배열이 되는지 확인합니다."""

    request = DetectRequest.model_validate(
        {
            **_valid_detection_context_fields(),
            "nerDeploymentId": "ner-a",
            "llmDeploymentId": "llm-a",
            "text": "홍길동은 프로젝트 알파를 담당합니다.",
            "regexCandidates": [_valid_regex_candidate_data()],
        }
    )

    serialized = request.model_dump(by_alias=True, mode="json")
    schema = DetectRequest.model_json_schema(by_alias=True)

    assert isinstance(serialized["regexCandidates"], list)
    assert serialized["regexCandidates"][0]["text"] == "홍길동"
    assert serialized["regexCandidates"][0]["policyId"] == "P01"
    assert schema["properties"]["regexCandidates"]["type"] == "array"


def test_detect_request_rejects_regex_candidate_outside_text() -> None:
    """Regex 후보의 끝 위치가 요청 원문을 벗어나면 거부합니다."""

    data = _valid_regex_candidate_data()
    data["end"] = 4

    with pytest.raises(ValidationError) as exc_info:
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "text": "홍길동",
                "regexCandidates": [data],
            }
        )

    assert (
        "regexCandidates[0].end는 text 길이를 초과할 수 없습니다"
        in str(exc_info.value)
    )


def test_detect_request_rejects_regex_candidate_text_mismatch() -> None:
    """Regex 후보 문자열이 요청 원문의 해당 구간과 다르면 거부합니다."""

    data = _valid_regex_candidate_data()
    data["text"] = "김철수"

    with pytest.raises(ValidationError) as exc_info:
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "text": "홍길동",
                "regexCandidates": [data],
            }
        )

    assert (
        "regexCandidates[0].text는 요청 원문의 해당 구간과 일치해야 합니다"
        in str(exc_info.value)
    )


def test_detect_request_rejects_duplicate_candidate_ids() -> None:
    """한 요청에서 같은 candidateId를 여러 후보가 공유하지 못합니다."""

    data = _valid_regex_candidate_data()

    with pytest.raises(ValidationError, match="candidateId는 중복"):
        DetectRequest.model_validate(
            {
                **_valid_detection_context_fields(),
                "nerDeploymentId": "ner-a",
                "llmDeploymentId": "llm-a",
                "text": "홍길동",
                "regexCandidates": [data, dict(data)],
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidateId", "R01"),
        ("candidateId", "regex-001"),
        ("detailType", "phone-number"),
        ("detailType", ""),
        ("score", -0.1),
        ("score", 1.1),
    ],
)
def test_regex_candidate_rejects_invalid_fields(
    field: str,
    value: object,
) -> None:
    """후보 ID·DetailType·점수의 엄격한 계약 위반을 거부합니다."""

    data = _valid_regex_candidate_data()
    data[field] = value

    with pytest.raises(ValidationError):
        RegexCandidate.model_validate(data)


@pytest.mark.parametrize(
    ("policy_id", "entity_type"),
    [("P01", "PERSON"), ("P04", "LOCATION")],
)
def test_ner_candidate_accepts_fixed_entity_mapping(
    policy_id: str,
    entity_type: str,
) -> None:
    """NER 책임인 두 정책과 개체 형식의 고정 매핑을 허용합니다."""

    data = _valid_ner_candidate_data()
    data["policyId"] = policy_id
    data["entityType"] = entity_type

    candidate = NerCandidate.model_validate(data)

    assert candidate.policy_id == policy_id
    assert candidate.entity_type == entity_type


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidateId", "R001"),
        ("candidateId", "N01"),
        ("policyId", "P03"),
        ("entityType", "ORGANIZATION"),
        ("score", -0.1),
        ("score", 1.1),
    ],
)
def test_ner_candidate_rejects_invalid_fields(
    field: str,
    value: object,
) -> None:
    """NER 후보 ID·정책·개체 형식·점수 계약 위반을 거부합니다."""

    data = _valid_ner_candidate_data()
    data[field] = value

    with pytest.raises(ValidationError):
        NerCandidate.model_validate(data)


def test_ner_candidate_rejects_policy_entity_mismatch() -> None:
    """정책과 다른 개체 형식 조합을 fail-closed로 거부합니다."""

    data = _valid_ner_candidate_data()
    data["entityType"] = "LOCATION"

    with pytest.raises(ValidationError, match="고정 개체 형식"):
        NerCandidate.model_validate(data)


def test_ner_candidate_is_frozen_and_uses_camel_case_aliases() -> None:
    """NER 후보를 불변으로 보관하고 Prompt용 camelCase로 직렬화합니다."""

    candidate = NerCandidate.model_validate(_valid_ner_candidate_data())

    with pytest.raises(ValidationError):
        candidate.score = 0.5
    assert candidate.model_dump(by_alias=True, mode="json") == (
        _valid_ner_candidate_data()
    )


def test_candidate_decisions_preserve_server_verified_candidate_data() -> None:
    """Regex·NER 후보 판정이 출처별 상세 필드를 정확히 보존합니다."""

    regex_decision = RegexCandidateDecision.model_validate(
        {
            **_valid_regex_candidate_data(),
            "source": "regex",
            "decision": "CONFIRMED",
        }
    )
    ner_decision = NerCandidateDecision.model_validate(
        {
            **_valid_ner_candidate_data(),
            "source": "ner",
            "decision": "UNCERTAIN",
        }
    )
    response = DetectResponse(
        candidateDecisions=(regex_decision, ner_decision),
    )

    serialized = response.model_dump(by_alias=True, mode="json")
    assert serialized["candidateDecisions"] == [
        {
            **_valid_regex_candidate_data(),
            "source": "regex",
            "decision": "CONFIRMED",
        },
        {
            **_valid_ner_candidate_data(),
            "source": "ner",
            "decision": "UNCERTAIN",
        },
    ]


@pytest.mark.parametrize("decision", ["ACCEPTED", "confirmed", ""])
def test_candidate_decision_rejects_unknown_decision(decision: str) -> None:
    """후보 판정은 세 개의 대문자 상태만 허용합니다."""

    with pytest.raises(ValidationError):
        RegexCandidateDecision.model_validate(
            {
                **_valid_regex_candidate_data(),
                "source": "regex",
                "decision": decision,
            }
        )


def test_detect_response_rejects_source_specific_candidate_field_mix() -> None:
    """Regex 판정에는 entityType, NER 판정에는 detailType을 허용하지 않습니다."""

    with pytest.raises(ValidationError):
        DetectResponse.model_validate(
            {
                "candidateDecisions": [
                    {
                        **_valid_regex_candidate_data(),
                        "source": "regex",
                        "entityType": "PERSON",
                        "decision": "CONFIRMED",
                    }
                ],
                "detections": [],
            }
        )


def test_detect_response_uses_empty_tuple_by_default() -> None:
    """DetectResponse가 두 결과 목록을 불변 빈 tuple로 보관합니다."""

    response = DetectResponse()

    assert response.candidate_decisions == ()
    assert response.detections == ()


def test_detect_response_normalizes_list_and_validates_detections() -> None:
    """리스트 입력을 tuple로 바꾸면서 중첩 Detection을 검증하는지 확인합니다."""

    response = DetectResponse.model_validate(
        {"detections": [_valid_detection_data()]}
    )

    assert isinstance(response.detections, tuple)
    assert len(response.detections) == 1
    assert response.detections[0].type == "PERSONAL_IDENTITY"


def test_detect_response_detections_cannot_be_appended() -> None:
    """검증이 끝난 탐지 결과 컬렉션을 생성 후 변경할 수 없는지 확인합니다."""

    response = DetectResponse.model_validate(
        {"detections": [_valid_detection_data()]}
    )

    assert not hasattr(response.detections, "append")


def test_detect_response_isolated_from_source_list() -> None:
    """생성에 사용한 원본 리스트의 변경이 응답에 전파되지 않는지 확인합니다."""

    detections = [_valid_detection_data()]
    response = DetectResponse.model_validate({"detections": detections})

    detections.clear()

    assert len(response.detections) == 1


def test_detect_response_serializes_tuple_as_json_array() -> None:
    """내부 tuple이 API 직렬화에서는 기존 JSON 배열로 변환되는지 확인합니다."""

    response = DetectResponse.model_validate(
        {"detections": [_valid_detection_data()]}
    )

    serialized = response.model_dump(mode="json", by_alias=True)
    schema = DetectResponse.model_json_schema(by_alias=True)

    assert isinstance(serialized["detections"], list)
    assert serialized["detections"][0]["type"] == "PERSONAL_IDENTITY"
    assert serialized["detections"][0]["policyId"] == "P01"
    assert schema["properties"]["detections"]["type"] == "array"


def test_detect_response_accepts_json_array() -> None:
    """HTTP JSON 배열 입력도 내부 tuple로 정규화되는지 확인합니다."""

    response = DetectResponse.model_validate_json(
        '{"detections":[]}'
    )

    assert response.detections == ()


def test_detect_response_rejects_existing_regex_result() -> None:
    """LPL 응답에는 Gateway가 이미 가진 Regex 결과를 포함할 수 없습니다."""

    data = _valid_regex_candidate_data()

    with pytest.raises(ValidationError):
        DetectResponse.model_validate({"detections": [data]})


def test_detection_is_frozen() -> None:
    """Detection 모델의 필드를 생성 후 변경할 수 없는지 검증합니다."""
    detection = Detection.model_validate(_valid_detection_data())

    with pytest.raises(ValidationError):
        detection.score = 0.5
