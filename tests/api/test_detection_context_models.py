"""조직 프로필과 텍스트 출처의 엄격한 API 계약을 검증합니다."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.policies.organization_profile_validator import (
    OrganizationProfilePolicyError,
    OrganizationProfileValidator,
)
from app.schemas.detection_context import (
    EnvironmentType,
    OrganizationProfile,
    OrganizationType,
    PublicEntityType,
    SourceType,
    TechnologyAssetType,
    ThirdPartyRelationship,
)


def _profile_data() -> dict[str, object]:
    """모든 조건부 섹션이 포함된 유효한 조직 프로필을 만듭니다."""

    return {
        "organization": {
            "name": "ABC 주식회사",
            "aliases": ["ABC", "ABC Corp"],
            "type": "PRIVATE",
        },
        "publicContext": {
            "domains": ["abc.com", "service.abc.com"],
            "entities": [
                {
                    "name": "LLMGateway",
                    "aliases": ["엘엘엠게이트웨이"],
                    "type": "SYSTEM",
                    "publicScope": "시스템명 및 일반 기능",
                }
            ],
        },
        "privacy": {
            "personNameScope": True,
            "persons": [
                {"name": "고명준", "aliases": ["MJ Ko"]},
                {"name": "지재현", "aliases": []},
            ],
        },
        "securityContext": {
            "internalIpRanges": ["10.20.0.0/16", "172.16.10.15"],
            "internalDomains": ["corp.abc.internal", "*.abc.local"],
            "internalSystems": [
                {
                    "name": "PAY-PROD",
                    "aliases": ["결제 운영계"],
                    "type": "APPLICATION_SERVER",
                    "environment": "PRODUCT",
                }
            ],
            "cloudAssets": [
                {
                    "provider": "AWS",
                    "account": "production-main",
                    "resource": "payment-prod",
                }
            ],
            "securityAssets": ["prod-vpn-01", "WAF-PROD"],
            "protectTestLogs": True,
        },
        "confidentialTechnologyContext": {
            "assets": [
                {
                    "name": "Aurora",
                    "aliases": ["Project A", "오로라"],
                    "type": "PROJECT",
                }
            ]
        },
        "thirdPartyContext": {
            "entities": [
                {
                    "name": "DEF은행",
                    "aliases": ["DEF Bank"],
                    "relationship": "CUSTOMER",
                    "relationshipPublic": True,
                    "hasConfidentialInformation": True,
                }
            ]
        },
    }


@pytest.mark.parametrize(
    ("enum_type", "expected"),
    [
        (
            OrganizationType,
            [
                "PRIVATE",
                "PUBLIC",
                "FINANCE",
                "MEDICAL",
                "EDUCATION",
                "DEFENSE",
                "OTHER",
            ],
        ),
        (
            PublicEntityType,
            [
                "PRODUCT",
                "SERVICE",
                "PROJECT",
                "CUSTOMER",
                "PARTNER",
                "SYSTEM",
                "TECH",
                "OTHER",
            ],
        ),
        (
            EnvironmentType,
            ["PRODUCT", "STAGING", "DEV", "TEST", "OTHER"],
        ),
        (
            TechnologyAssetType,
            [
                "PROJECT",
                "PRODUCT",
                "TECH",
                "REPO",
                "RESEARCH",
                "DATASET",
                "OTHER",
            ],
        ),
        (
            ThirdPartyRelationship,
            ["CUSTOMER", "PARTNER", "SUPPLIER", "CONTRACTOR", "OTHER"],
        ),
    ],
)
def test_organization_context_enums_are_exact(
    enum_type: object,
    expected: list[str],
) -> None:
    """Gateway와 공유한 조직 Context Enum 목록을 OpenAPI 기준으로 고정합니다."""

    assert TypeAdapter(enum_type).json_schema()["enum"] == expected


@pytest.mark.parametrize(
    "legacy_value",
    [
        "PRIVATE_COMPANY",
        "PUBLIC_AGENCY",
        "FINANCIAL",
        "HEALTHCARE",
        "DEFENSE_SECURITY",
        "TECHNOLOGY",
        "PRODUCTION",
        "DEVELOPMENT",
        "REPOSITORY",
        "RESEARCH_TOPIC",
    ],
)
def test_organization_context_rejects_legacy_enum_values(
    legacy_value: str,
) -> None:
    """이전 조직 Context Enum은 새 외부 계약에서 허용하지 않습니다."""

    adapters = (
        TypeAdapter(OrganizationType),
        TypeAdapter(PublicEntityType),
        TypeAdapter(EnvironmentType),
        TypeAdapter(TechnologyAssetType),
    )
    assert all(
        legacy_value not in adapter.json_schema()["enum"]
        for adapter in adapters
    )


def test_organization_profile_accepts_and_freezes_full_context() -> None:
    """전체 관리자 기준정보를 중첩 불변 모델과 camelCase로 보존합니다."""

    profile = OrganizationProfile.model_validate(_profile_data())
    serialized = profile.model_dump(by_alias=True, mode="json")

    assert profile.organization.aliases == ("ABC", "ABC Corp")
    assert profile.security_context is not None
    assert profile.security_context.internal_ip_ranges == (
        "10.20.0.0/16",
        "172.16.10.15",
    )
    assert serialized["publicContext"]["entities"][0]["publicScope"] == (
        "시스템명 및 일반 기능"
    )
    assert serialized["securityContext"]["protectTestLogs"] is True
    assert serialized["privacy"]["persons"][0] == {
        "name": "고명준",
        "aliases": ["MJ Ko"],
    }
    assert not hasattr(profile.organization.aliases, "append")


def test_privacy_persons_defaults_to_empty_tuple() -> None:
    """기존 프로필은 persons를 생략해도 빈 명단으로 호환됩니다."""

    data = _profile_data()
    privacy = data["privacy"]
    assert isinstance(privacy, dict)
    privacy.pop("persons")

    profile = OrganizationProfile.model_validate(data)

    assert profile.privacy is not None
    assert profile.privacy.persons == ()
    assert profile.model_dump(by_alias=True, mode="json")["privacy"][
        "persons"
    ] == []


def test_privacy_persons_rejects_duplicate_names_and_aliases() -> None:
    """서로 다른 관계자가 같은 이름·별칭을 공유하면 모호하므로 거부합니다."""

    data = _profile_data()
    privacy = data["privacy"]
    assert isinstance(privacy, dict)
    privacy["persons"] = [
        {"name": "고명준", "aliases": []},
        {"name": "지재현", "aliases": ["고명준"]},
    ]

    with pytest.raises(ValidationError, match="서로 중복"):
        OrganizationProfile.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("publicContext", "domains"), []),
        (("publicContext", "domains"), ["https://abc.com/path"]),
        (("securityContext", "internalIpRanges"), ["999.1.1.1"]),
        (("securityContext", "internalDomains"), ["abc.com/path"]),
        (("organization", "aliases"), ["ABC", "ABC"]),
        (("organization", "name"), "\ud800"),
    ],
)
def test_organization_profile_rejects_invalid_values(
    path: tuple[str, str],
    value: object,
) -> None:
    """빈 공개 도메인, URL·IP 오류, 중복과 비 UTF-8을 거부합니다."""

    data = _profile_data()
    section = data[path[0]]
    assert isinstance(section, dict)
    section[path[1]] = value

    with pytest.raises(ValidationError):
        OrganizationProfile.model_validate(data)


def test_organization_profile_rejects_unknown_field() -> None:
    """조직 Context의 알 수 없는 필드는 중첩 수준에서도 거부합니다."""

    data = _profile_data()
    organization = data["organization"]
    assert isinstance(organization, dict)
    organization["unknownField"] = True

    with pytest.raises(ValidationError):
        OrganizationProfile.model_validate(data)


def test_active_policy_sections_are_conditionally_required() -> None:
    """P01·S02·S03·B02·B03 필수 섹션을 모두 확인합니다."""

    data = _profile_data()
    for field in (
        "privacy",
        "securityContext",
        "confidentialTechnologyContext",
        "thirdPartyContext",
    ):
        del data[field]
    profile = OrganizationProfile.model_validate(data)

    with pytest.raises(OrganizationProfilePolicyError) as error_info:
        OrganizationProfileValidator().validate(
            profile,
            enabled_policy_ids=(
                "P01",
                "S02",
                "S03",
                "B01",
                "B02",
                "B03",
            ),
        )

    assert error_info.value.missing_sections == (
        "privacy",
        "securityContext",
        "thirdPartyContext",
        "confidentialTechnologyContext",
    )


def test_b01_does_not_require_technology_context() -> None:
    """인사·인력 정책 B01은 기밀 기술 Context를 요구하지 않습니다."""

    data = _profile_data()
    del data["confidentialTechnologyContext"]
    profile = OrganizationProfile.model_validate(data)

    OrganizationProfileValidator().validate(
        profile,
        enabled_policy_ids=("B01",),
    )


def test_b03_requires_technology_context() -> None:
    """R&D 정책 B03은 등록 기술자산 Context를 요구합니다."""

    data = _profile_data()
    del data["confidentialTechnologyContext"]
    profile = OrganizationProfile.model_validate(data)

    with pytest.raises(OrganizationProfilePolicyError) as error_info:
        OrganizationProfileValidator().validate(
            profile,
            enabled_policy_ids=("B03",),
        )

    assert error_info.value.missing_sections == (
        "confidentialTechnologyContext",
    )


def test_missing_entire_profile_skips_conditional_section_requirements() -> None:
    """조직 프로필 자체를 선택하지 않은 기업에는 섹션을 강제하지 않습니다."""

    OrganizationProfileValidator().validate(
        None,
        enabled_policy_ids=(
            "P01",
            "S02",
            "S03",
            "B01",
            "B02",
            "B03",
        ),
    )


def test_s03_requires_explicit_test_log_policy() -> None:
    """S03 활성화 시 protectTestLogs의 명시적인 boolean을 요구합니다."""

    data = _profile_data()
    security = data["securityContext"]
    assert isinstance(security, dict)
    security.pop("protectTestLogs")
    profile = OrganizationProfile.model_validate(data)

    with pytest.raises(OrganizationProfilePolicyError) as error_info:
        OrganizationProfileValidator().validate(
            profile,
            enabled_policy_ids=("S03",),
        )

    assert error_info.value.missing_sections == (
        "securityContext.protectTestLogs",
    )


@pytest.mark.parametrize("source_type", ["CHAT_TEXT", "OCR_TEXT"])
def test_source_type_accepts_exact_values(source_type: str) -> None:
    """Gateway가 알려줄 두 가지 텍스트 출처만 허용합니다."""

    assert TypeAdapter(SourceType).validate_python(source_type) == source_type


@pytest.mark.parametrize("source_type", ["FILE", "TEXT", "ocr_text", ""])
def test_source_type_rejects_unknown_values(source_type: str) -> None:
    """파일 입력을 암시하거나 대소문자가 다른 출처값을 거부합니다."""

    with pytest.raises(ValidationError):
        TypeAdapter(SourceType).validate_python(source_type)
