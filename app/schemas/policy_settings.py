"""전역 활성 Policy ID 설정의 API·저장 계약입니다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.detection import ALLOWED_POLICY_IDS, PolicyId


class PolicySettings(BaseModel):
    """현재 LPL 인스턴스에서 검사할 전역 Policy ID 목록입니다."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )

    enabled_policy_ids: tuple[PolicyId, ...] = Field(
        alias="enabledPolicies",
        min_length=1,
        max_length=len(ALLOWED_POLICY_IDS),
    )

    @field_validator("enabled_policy_ids", mode="before")
    @classmethod
    def normalize_input(cls, value: object) -> object:
        """외부 JSON list를 변경 불가능한 tuple로 분리합니다."""

        if type(value) is list:
            return tuple(value)
        return value

    @field_validator("enabled_policy_ids")
    @classmethod
    def reject_duplicates_and_order(
        cls,
        value: tuple[PolicyId, ...],
    ) -> tuple[PolicyId, ...]:
        """중복을 거부하고 서버의 고정 Policy ID 순서로 정규화합니다."""

        if len(set(value)) != len(value):
            raise ValueError("enabledPolicies에는 중복 Policy ID가 없어야 합니다")
        selected = frozenset(value)
        return tuple(
            policy_id
            for policy_id in ALLOWED_POLICY_IDS
            if policy_id in selected
        )


def default_policy_settings() -> PolicySettings:
    """마이그레이션 호환을 위해 전체 정책이 활성인 기본값을 만듭니다."""

    return PolicySettings(enabledPolicies=ALLOWED_POLICY_IDS)


__all__ = ["PolicySettings", "default_policy_settings"]
