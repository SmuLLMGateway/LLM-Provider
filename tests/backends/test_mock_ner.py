"""빈 결과를 제공하는 Mock NER Backend의 실행 계약을 검증합니다."""

from __future__ import annotations

import pytest

from app.backends.ner.mock import (
    MockNerBackend,
    MockNerConfigurationError,
)
from app.schemas.registry import DeploymentConfig


def _deployment(
    *,
    kind: str = "ner",
    adapter_type: str = "mock",
    enabled: bool = True,
) -> DeploymentConfig:
    """Mock NER 테스트용 Deployment 설정을 생성합니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": kind,
            "adapterType": adapter_type,
            "enabled": enabled,
        }
    )


@pytest.mark.asyncio
async def test_mock_ner_returns_empty_detections() -> None:
    """유효한 원문과 Deployment에는 빈 공통 탐지 목록을 반환합니다."""

    result = await MockNerBackend().detect(
        "홍길동은 서울에서 근무합니다.",
        _deployment(),
    )

    assert result == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment", "message"),
    [
        (_deployment(kind="llm"), "kind는 ner"),
        (_deployment(adapter_type="other_ner"), "adapterType은 mock"),
    ],
)
async def test_mock_ner_rejects_wrong_deployment(
    deployment: DeploymentConfig,
    message: str,
) -> None:
    """NER 종류나 Mock Adapter와 맞지 않는 Deployment를 거부합니다."""

    with pytest.raises(MockNerConfigurationError, match=message):
        await MockNerBackend().detect("원문", deployment)


@pytest.mark.asyncio
async def test_mock_ner_rejects_disabled_deployment() -> None:
    """비활성화된 Mock NER Deployment의 실행을 거부합니다."""

    with pytest.raises(
        MockNerConfigurationError,
        match="비활성화된 Deployment",
    ):
        await MockNerBackend().detect(
            "원문",
            _deployment(enabled=False),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_text", [None, 123, True, ["원문"]])
async def test_mock_ner_rejects_non_string_text(
    invalid_text: object,
) -> None:
    """문자열이 아닌 원문 입력을 실행 전에 거부합니다."""

    with pytest.raises(MockNerConfigurationError, match="text는 문자열"):
        await MockNerBackend().detect(  # type: ignore[arg-type]
            invalid_text,
            _deployment(),
        )

