"""탐지 API와 Pipeline이 공유하는 Pydantic 모델입니다."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.registry import ResourceId
from app.schemas.detection_context import OrganizationProfile, SourceType


NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
CandidateId = Annotated[
    str,
    StringConstraints(pattern=r"^[RN][0-9]{3,}$"),
]
DetailType = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    ),
]
EntityType = Literal["PERSON", "LOCATION"]
CandidateDecisionValue = Literal["CONFIRMED", "REJECTED", "UNCERTAIN"]
DetectionSource = Literal["regex", "ner", "llm"]
DetectionType = Literal[
    "PERSONAL_IDENTITY",
    "UNIQUE_IDENTITY",
    "CONTACT",
    "LOCATION",
    "PAYMENT",
    "FINANCE_ACCOUNT",
    "PERSONAL_FINANCE",
    "SENSITIVE_PERSONAL",
    "AUTH",
    "SECURITY_INFRA",
    "SYSTEM_LOG",
    "PERSONAL",
    "CLIENT",
    "R&D",
]
PolicyId = Literal[
    "P01",
    "P02",
    "P03",
    "P04",
    "P05",
    "P06",
    "P07",
    "P08",
    "S01",
    "S02",
    "S03",
    "B01",
    "B02",
    "B03",
]
NerPolicyId = Literal["P01", "P04"]
DetectionScore = Annotated[float, Field(ge=0.0, le=1.0)]
MAX_REGEX_CANDIDATES = 10_000

ALLOWED_DETECTION_TYPES: tuple[DetectionType, ...] = (
    "PERSONAL_IDENTITY",
    "UNIQUE_IDENTITY",
    "CONTACT",
    "LOCATION",
    "PAYMENT",
    "FINANCE_ACCOUNT",
    "PERSONAL_FINANCE",
    "SENSITIVE_PERSONAL",
    "AUTH",
    "SECURITY_INFRA",
    "SYSTEM_LOG",
    "PERSONAL",
    "CLIENT",
    "R&D",
)

NER_DETECTION_TYPES: tuple[DetectionType, ...] = (
    "PERSONAL_IDENTITY",
    "LOCATION",
)

MULTI_OCCURRENCE_DETECTION_TYPES: tuple[DetectionType, ...] = (
    "PERSONAL_IDENTITY",
    "UNIQUE_IDENTITY",
    "CONTACT",
    "LOCATION",
    "PAYMENT",
    "FINANCE_ACCOUNT",
    "AUTH",
)

CONTEXTUAL_DETECTION_TYPES: tuple[DetectionType, ...] = (
    "PERSONAL_FINANCE",
    "SENSITIVE_PERSONAL",
    "SECURITY_INFRA",
    "SYSTEM_LOG",
    "PERSONAL",
    "CLIENT",
    "R&D",
)

POLICY_ID_BY_DETECTION_TYPE: Mapping[DetectionType, PolicyId] = (
    MappingProxyType(
        {
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
    )
)
DETECTION_TYPE_BY_POLICY_ID: Mapping[PolicyId, DetectionType] = (
    MappingProxyType(
        {
            policy_id: detection_type
            for detection_type, policy_id in (
                POLICY_ID_BY_DETECTION_TYPE.items()
            )
        }
    )
)
ALLOWED_POLICY_IDS: tuple[PolicyId, ...] = tuple(
    DETECTION_TYPE_BY_POLICY_ID
)
ENTITY_TYPE_BY_POLICY_ID: Mapping[NerPolicyId, EntityType] = (
    MappingProxyType(
        {
            "P01": "PERSON",
            "P04": "LOCATION",
        }
    )
)


def policy_id_for_detection_type(
    detection_type: DetectionType,
) -> PolicyId:
    """Detection Type에 고정된 Policy ID를 반환합니다."""

    return POLICY_ID_BY_DETECTION_TYPE[detection_type]


class DetectionModel(BaseModel):
    """탐지 모델의 필드 재할당을 막고 엄격하게 검증합니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        strict=True,
    )


class RegexCandidate(DetectionModel):
    """Gateway Regex가 제안하고 LLM이 나중에 판정할 미확정 후보입니다."""

    candidate_id: CandidateId = Field(alias="candidateId")
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: NonEmptyString
    policy_id: PolicyId = Field(alias="policyId")
    detail_type: DetailType = Field(alias="detailType")
    score: DetectionScore

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        """Regex ID 접두사와 반개방 span 순서를 검증합니다."""

        if not self.candidate_id.startswith("R"):
            raise ValueError("RegexCandidate.candidateId는 R로 시작해야 합니다")
        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        return self


class NerCandidate(DetectionModel):
    """NER가 새로 찾고 LLM이 나중에 판정할 미확정 개체 후보입니다."""

    candidate_id: CandidateId = Field(alias="candidateId")
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: NonEmptyString
    policy_id: NerPolicyId = Field(alias="policyId")
    entity_type: EntityType = Field(alias="entityType")
    score: DetectionScore

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        """NER ID 접두사, span 순서와 정책별 개체 형식을 검증합니다."""

        if not self.candidate_id.startswith("N"):
            raise ValueError("NerCandidate.candidateId는 N으로 시작해야 합니다")
        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        if self.entity_type != ENTITY_TYPE_BY_POLICY_ID[self.policy_id]:
            raise ValueError(
                "entityType은 policyId에 대응하는 고정 개체 형식이어야 합니다"
            )
        return self


class RegexCandidateDecision(DetectionModel):
    """검증된 Regex 후보와 LLM 판정을 결합한 응답 항목입니다."""

    candidate_id: CandidateId = Field(alias="candidateId")
    source: Literal["regex"] = "regex"
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: NonEmptyString
    policy_id: PolicyId = Field(alias="policyId")
    detail_type: DetailType = Field(alias="detailType")
    score: DetectionScore
    decision: CandidateDecisionValue

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        """Regex ID 접두사와 반개방 span 순서를 다시 검증합니다."""

        if not self.candidate_id.startswith("R"):
            raise ValueError("Regex 후보 판정 candidateId는 R로 시작해야 합니다")
        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        return self


class NerCandidateDecision(DetectionModel):
    """검증된 NER 후보와 LLM 판정을 결합한 응답 항목입니다."""

    candidate_id: CandidateId = Field(alias="candidateId")
    source: Literal["ner"] = "ner"
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: NonEmptyString
    policy_id: NerPolicyId = Field(alias="policyId")
    entity_type: EntityType = Field(alias="entityType")
    score: DetectionScore
    decision: CandidateDecisionValue

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        """NER ID·span과 정책별 개체 형식을 다시 검증합니다."""

        if not self.candidate_id.startswith("N"):
            raise ValueError("NER 후보 판정 candidateId는 N으로 시작해야 합니다")
        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        if self.entity_type != ENTITY_TYPE_BY_POLICY_ID[self.policy_id]:
            raise ValueError(
                "entityType은 policyId에 대응하는 고정 개체 형식이어야 합니다"
            )
        return self


CandidateDecision = Annotated[
    RegexCandidateDecision | NerCandidateDecision,
    Field(discriminator="source"),
]


class Detection(DetectionModel):
    """원문에서 탐지된 민감정보 구간 하나입니다."""

    start: int = Field(ge=0)
    end: int = Field(ge=1)
    text: NonEmptyString
    type: DetectionType
    policy_id: PolicyId = Field(alias="policyId")
    source: DetectionSource
    score: DetectionScore

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        """탐지 구간의 끝 위치가 시작 위치보다 뒤인지 검증합니다."""

        if self.end <= self.start:
            raise ValueError("end는 start보다 커야 합니다")
        expected_policy_id = policy_id_for_detection_type(self.type)
        if self.policy_id != expected_policy_id:
            raise ValueError(
                "policyId는 type에 대응하는 고정 정책 ID여야 합니다"
            )
        return self


def _normalize_detection_sequence(value: object) -> object:
    """JSON list 입력을 원본과 분리된 불변 tuple로 변환합니다."""

    if type(value) is list:
        return tuple(value)
    return value


class DetectRequest(DetectionModel):
    """탐지 Pipeline 실행에 필요한 요청입니다."""

    text: NonEmptyString
    ner_deployment_id: ResourceId = Field(alias="nerDeploymentId")
    llm_deployment_id: ResourceId = Field(alias="llmDeploymentId")
    organization_profile: OrganizationProfile | None = Field(
        default=None,
        alias="organizationProfile",
    )
    source_type: SourceType = Field(alias="sourceType")
    regex_candidates: tuple[RegexCandidate, ...] = Field(
        default=(),
        alias="regexCandidates",
        max_length=MAX_REGEX_CANDIDATES,
    )

    @field_validator("regex_candidates", mode="before")
    @classmethod
    def normalize_regex_candidates(
        cls,
        value: object,
    ) -> object:
        """Regex 후보 JSON 배열을 요청 내부의 불변 tuple로 정규화합니다."""

        return _normalize_detection_sequence(value)

    @model_validator(mode="after")
    def validate_regex_candidate_spans(self) -> Self:
        """Regex 후보 ID·구간·문자열이 요청 원문과 일치하는지 검증합니다."""

        text_encoding_failed = False
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            text_encoding_failed = True
        if text_encoding_failed:
            raise ValueError(
                "text는 유효한 UTF-8 문자열이어야 합니다"
            ) from None

        seen_candidate_ids: set[str] = set()
        for index, candidate in enumerate(self.regex_candidates):
            if candidate.candidate_id in seen_candidate_ids:
                raise ValueError(
                    f"regexCandidates[{index}].candidateId는 중복될 수 없습니다"
                )
            seen_candidate_ids.add(candidate.candidate_id)
            detection_encoding_failed = False
            try:
                candidate.text.encode("utf-8")
                candidate.detail_type.encode("utf-8")
            except UnicodeEncodeError:
                detection_encoding_failed = True
            if detection_encoding_failed:
                raise ValueError(
                    f"regexCandidates[{index}]의 문자열은 "
                    "유효한 UTF-8이어야 합니다"
                ) from None
            if candidate.end > len(self.text):
                raise ValueError(
                    f"regexCandidates[{index}].end는 "
                    "text 길이를 초과할 수 없습니다"
                )
            if self.text[candidate.start : candidate.end] != candidate.text:
                raise ValueError(
                    f"regexCandidates[{index}].text는 요청 원문의 "
                    "해당 구간과 일치해야 합니다"
                )
        return self


class DetectResponse(DetectionModel):
    """LPL이 정규화하여 반환하는 후보 판정과 탐지 결과입니다."""

    candidate_decisions: tuple[CandidateDecision, ...] = Field(
        default=(),
        alias="candidateDecisions",
    )
    detections: tuple[Detection, ...] = ()

    @field_validator("candidate_decisions", "detections", mode="before")
    @classmethod
    def normalize_sequences(
        cls,
        value: object,
    ) -> object:
        """Python list 입력을 내부 불변 tuple로 정규화합니다."""

        return _normalize_detection_sequence(value)

    @model_validator(mode="after")
    def reject_existing_regex_results(self) -> Self:
        """LPL 응답에는 새로 탐지한 NER·LLM 결과만 허용합니다."""

        for index, detection in enumerate(self.detections):
            if detection.source == "regex":
                raise ValueError(
                    f"detections[{index}].source는 ner 또는 llm이어야 합니다"
                )
        return self


__all__ = [
    "ALLOWED_DETECTION_TYPES",
    "ALLOWED_POLICY_IDS",
    "CandidateId",
    "CandidateDecision",
    "CandidateDecisionValue",
    "CONTEXTUAL_DETECTION_TYPES",
    "DETECTION_TYPE_BY_POLICY_ID",
    "ENTITY_TYPE_BY_POLICY_ID",
    "DetectRequest",
    "DetectResponse",
    "Detection",
    "DetectionScore",
    "DetectionSource",
    "DetectionType",
    "DetailType",
    "EntityType",
    "MAX_REGEX_CANDIDATES",
    "MULTI_OCCURRENCE_DETECTION_TYPES",
    "NER_DETECTION_TYPES",
    "NerCandidate",
    "NerCandidateDecision",
    "NerPolicyId",
    "POLICY_ID_BY_DETECTION_TYPE",
    "PolicyId",
    "RegexCandidate",
    "RegexCandidateDecision",
    "policy_id_for_detection_type",
]
