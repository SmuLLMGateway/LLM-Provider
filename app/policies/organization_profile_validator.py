"""활성 정책에 필요한 조직 프로필 섹션의 완전성을 검증합니다."""

from __future__ import annotations

from app.schemas.detection import PolicyId
from app.schemas.detection_context import OrganizationProfile


class OrganizationProfilePolicyError(ValueError):
    """활성 정책에 필요한 조직 기준정보가 빠지면 발생합니다."""

    def __init__(self, *, missing_sections: tuple[str, ...]) -> None:
        self.missing_sections = tuple(missing_sections)
        super().__init__(
            "활성 정책에 필요한 조직 프로필 섹션이 없습니다: "
            + ", ".join(self.missing_sections)
        )


class OrganizationProfileValidator:
    """조직 프로필과 활성 정책 사이의 조건부 필수 규칙을 적용합니다."""

    def validate(
        self,
        profile: OrganizationProfile | None,
        *,
        enabled_policy_ids: tuple[PolicyId, ...],
    ) -> None:
        """필요한 섹션 이름만 안전한 오류에 남기고 본문은 보관하지 않습니다."""

        if profile is None:
            return
        if type(profile) is not OrganizationProfile:
            raise TypeError("profile은 OrganizationProfile이어야 합니다")
        enabled = frozenset(enabled_policy_ids)
        missing: list[str] = []
        if "P01" in enabled and profile.privacy is None:
            missing.append("privacy")
        if ("S02" in enabled or "S03" in enabled) and (
            profile.security_context is None
        ):
            missing.append("securityContext")
        elif (
            "S03" in enabled
            and profile.security_context is not None
            and profile.security_context.protect_test_logs is None
        ):
            missing.append("securityContext.protectTestLogs")
        if "B02" in enabled and profile.third_party_context is None:
            missing.append("thirdPartyContext")
        if (
            "B03" in enabled
            and profile.confidential_technology_context is None
        ):
            missing.append("confidentialTechnologyContext")
        if missing:
            raise OrganizationProfilePolicyError(
                missing_sections=tuple(missing)
            )


__all__ = [
    "OrganizationProfilePolicyError",
    "OrganizationProfileValidator",
]
