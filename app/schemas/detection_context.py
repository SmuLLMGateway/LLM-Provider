"""조직별 공개·비공개 기준과 요청 문서 문맥의 엄격한 계약입니다."""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)


def _require_utf8(value: str) -> str:
    """고립 surrogate가 없는 UTF-8 문자열만 허용합니다."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("문자열은 유효한 UTF-8이어야 합니다") from None
    return value


def _require_domain(value: str) -> str:
    """URL이 아닌 공개·내부 도메인 또는 wildcard 도메인을 검증합니다."""

    if (
        len(value) > 253
        or "://" in value
        or "/" in value
        or "@" in value
        or re.fullmatch(
            r"(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
            r"[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
            r"[A-Za-z0-9])?",
            value,
        )
        is None
    ):
        raise ValueError("도메인 형식이 올바르지 않습니다")
    return value.lower()


def _require_ip_or_network(value: str) -> str:
    """단일 IP 또는 CIDR 문자열을 검증합니다."""

    try:
        if "/" in value:
            ipaddress.ip_network(value, strict=False)
        else:
            ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("IP 또는 CIDR 형식이 올바르지 않습니다") from None
    return value


ShortText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256),
    AfterValidator(_require_utf8),
]
DescriptionText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048),
    AfterValidator(_require_utf8),
]
DomainName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=253),
    AfterValidator(_require_utf8),
    AfterValidator(_require_domain),
]
IpOrNetwork = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    AfterValidator(_require_utf8),
    AfterValidator(_require_ip_or_network),
]
SourceType = Literal["CHAT_TEXT", "OCR_TEXT"]

OrganizationType = Literal[
    "PRIVATE",
    "PUBLIC",
    "FINANCE",
    "MEDICAL",
    "EDUCATION",
    "DEFENSE",
    "OTHER",
]
PublicEntityType = Literal[
    "PRODUCT",
    "SERVICE",
    "PROJECT",
    "CUSTOMER",
    "PARTNER",
    "SYSTEM",
    "TECH",
    "OTHER",
]
EnvironmentType = Literal[
    "PRODUCT",
    "STAGING",
    "DEV",
    "TEST",
    "OTHER",
]
CloudProvider = Literal["AWS", "AZURE", "GCP", "OTHER"]
TechnologyAssetType = Literal[
    "PROJECT",
    "PRODUCT",
    "TECH",
    "REPO",
    "RESEARCH",
    "DATASET",
    "OTHER",
]
ThirdPartyRelationship = Literal[
    "CUSTOMER",
    "PARTNER",
    "SUPPLIER",
    "CONTRACTOR",
    "OTHER",
]


class DetectionContextModel(BaseModel):
    """Context 모델의 추가 필드와 느슨한 변환을 거부합니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
    )


def _normalize_tuple(value: object) -> object:
    """외부 JSON 배열을 원본과 분리된 tuple로 변환합니다."""

    if type(value) is list:
        return tuple(value)
    return value


def _require_unique(
    values: tuple[str, ...],
    *,
    field_name: str,
) -> tuple[str, ...]:
    """순서를 보존하면서 문자열 배열의 중복을 거부합니다."""

    if len(values) != len(set(values)):
        raise ValueError(f"{field_name}에는 중복 값이 없어야 합니다")
    return values


class OrganizationIdentity(DetectionContextModel):
    """조직의 공식 명칭, 별칭과 유형입니다."""

    name: ShortText
    aliases: tuple[ShortText, ...] = Field(max_length=100)
    type: OrganizationType

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="organization.aliases")


class PublicEntity(DetectionContextModel):
    """이름 자체만으로 기밀로 판단하지 않을 공개 조직 고유 명칭입니다."""

    name: ShortText
    aliases: tuple[ShortText, ...] = Field(max_length=100)
    type: PublicEntityType
    public_scope: DescriptionText = Field(alias="publicScope")

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="publicEntity.aliases")


class PublicContext(DetectionContextModel):
    """조직이 공식적으로 외부에 공개한 도메인과 고유 명칭입니다."""

    domains: tuple[DomainName, ...] = Field(min_length=1, max_length=1000)
    entities: tuple[PublicEntity, ...] = Field(max_length=1000)

    @field_validator("domains", "entities", mode="before")
    @classmethod
    def normalize_sequences(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("domains")
    @classmethod
    def validate_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="publicContext.domains")


class ProtectedPerson(DetectionContextModel):
    """조직이 보호 대상으로 등록한 관계자 이름과 별칭입니다."""

    name: ShortText
    aliases: tuple[ShortText, ...] = Field(default=(), max_length=100)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="protectedPerson.aliases")


class PrivacyContext(DetectionContextModel):
    """인명 보호 활성 여부와 등록된 조직 관계자 목록입니다."""

    person_name_scope: bool = Field(alias="personNameScope")
    persons: tuple[ProtectedPerson, ...] = Field(
        default=(),
        max_length=1000,
    )

    @field_validator("persons", mode="before")
    @classmethod
    def normalize_persons(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("persons")
    @classmethod
    def validate_persons(
        cls,
        value: tuple[ProtectedPerson, ...],
    ) -> tuple[ProtectedPerson, ...]:
        registered_values: list[str] = []
        for person in value:
            registered_values.extend((person.name, *person.aliases))
        if len(registered_values) != len(set(registered_values)):
            raise ValueError(
                "privacy.persons의 이름과 별칭은 서로 중복될 수 없습니다"
            )
        return value


class InternalSystem(DetectionContextModel):
    """조직 내부 시스템·서버·장비 기준정보입니다."""

    name: ShortText
    aliases: tuple[ShortText, ...] = Field(max_length=100)
    type: ShortText
    environment: EnvironmentType

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="internalSystem.aliases")


class CloudAsset(DetectionContextModel):
    """조직 내부 클라우드 계정·프로젝트·리소스 기준정보입니다."""

    provider: CloudProvider
    account: ShortText
    resource: ShortText


class SecurityContext(DetectionContextModel):
    """내부 인프라와 시스템 로그 판단에 사용할 조직 기준정보입니다."""

    internal_ip_ranges: tuple[IpOrNetwork, ...] = Field(
        alias="internalIpRanges",
        max_length=1000,
    )
    internal_domains: tuple[DomainName, ...] = Field(
        alias="internalDomains",
        max_length=1000,
    )
    internal_systems: tuple[InternalSystem, ...] = Field(
        alias="internalSystems",
        max_length=1000,
    )
    cloud_assets: tuple[CloudAsset, ...] = Field(
        alias="cloudAssets",
        max_length=1000,
    )
    security_assets: tuple[ShortText, ...] = Field(
        alias="securityAssets",
        max_length=1000,
    )
    protect_test_logs: bool | None = Field(
        default=None,
        alias="protectTestLogs",
    )

    @field_validator(
        "internal_ip_ranges",
        "internal_domains",
        "internal_systems",
        "cloud_assets",
        "security_assets",
        mode="before",
    )
    @classmethod
    def normalize_sequences(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator(
        "internal_ip_ranges",
        "internal_domains",
        "security_assets",
    )
    @classmethod
    def validate_unique_strings(
        cls,
        value: tuple[str, ...],
        info,
    ) -> tuple[str, ...]:
        return _require_unique(value, field_name=info.field_name)


class TechnologyAsset(DetectionContextModel):
    """조직의 비공개 프로젝트·제품·기술·연구 기준정보입니다."""

    name: ShortText
    aliases: tuple[ShortText, ...] = Field(max_length=100)
    type: TechnologyAssetType

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="technologyAsset.aliases")


class ConfidentialTechnologyContext(DetectionContextModel):
    """비공개 R&D·영업비밀 자산 목록입니다."""

    assets: tuple[TechnologyAsset, ...] = Field(max_length=1000)

    @field_validator("assets", mode="before")
    @classmethod
    def normalize_assets(cls, value: object) -> object:
        return _normalize_tuple(value)


class ThirdPartyEntity(DetectionContextModel):
    """고객·협력사 관계와 공개·기밀 여부 기준정보입니다."""

    name: ShortText
    aliases: tuple[ShortText, ...] = Field(max_length=100)
    relationship: ThirdPartyRelationship
    relationship_public: bool = Field(alias="relationshipPublic")
    has_confidential_information: bool = Field(
        alias="hasConfidentialInformation"
    )

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> object:
        return _normalize_tuple(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique(value, field_name="thirdPartyEntity.aliases")


class ThirdPartyContext(DetectionContextModel):
    """고객·파트너·공급업체 등 제3자 기준정보입니다."""

    entities: tuple[ThirdPartyEntity, ...] = Field(max_length=1000)

    @field_validator("entities", mode="before")
    @classmethod
    def normalize_entities(cls, value: object) -> object:
        return _normalize_tuple(value)


class OrganizationProfile(DetectionContextModel):
    """Gateway가 조직별로 관리하고 탐지 요청에 전달하는 기준정보입니다."""

    organization: OrganizationIdentity
    public_context: PublicContext = Field(alias="publicContext")
    privacy: PrivacyContext | None = None
    security_context: SecurityContext | None = Field(
        default=None,
        alias="securityContext",
    )
    confidential_technology_context: ConfidentialTechnologyContext | None = (
        Field(default=None, alias="confidentialTechnologyContext")
    )
    third_party_context: ThirdPartyContext | None = Field(
        default=None,
        alias="thirdPartyContext",
    )


__all__ = [
    "CloudAsset",
    "ConfidentialTechnologyContext",
    "InternalSystem",
    "EnvironmentType",
    "OrganizationIdentity",
    "OrganizationProfile",
    "OrganizationType",
    "PrivacyContext",
    "ProtectedPerson",
    "PublicContext",
    "PublicEntity",
    "PublicEntityType",
    "SecurityContext",
    "SourceType",
    "TechnologyAsset",
    "TechnologyAssetType",
    "ThirdPartyContext",
    "ThirdPartyEntity",
    "ThirdPartyRelationship",
]
