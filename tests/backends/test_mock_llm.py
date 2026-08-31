"""고정 결과를 반환하는 Mock LLM Backend의 실행 계약을 검증합니다."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.backends.llm.base import LlmBackend
from app.backends.llm.mock import (
    MockLlmBackend,
    MockLlmConfigurationError,
)
from app.schemas.generation import LlmResult, LlmTokenUsage
from app.schemas.registry import DeploymentConfig


def _deployment(
    *,
    kind: str = "llm",
    adapter_type: str = "mock",
    enabled: bool = True,
) -> DeploymentConfig:
    """Mock LLM 테스트용 Deployment 설정을 생성합니다."""

    return DeploymentConfig.model_validate(
        {
            "kind": kind,
            "adapterType": adapter_type,
            "enabled": enabled,
        }
    )


@pytest.mark.asyncio
async def test_mock_llm_returns_default_result() -> None:
    """결과를 주입하지 않으면 빈 후보 판정·탐지 객체를 반환합니다."""

    result = await MockLlmBackend().generate(
        [{"role": "user", "content": "민감한 원문"}],
        _deployment(),
        {"temperature": 0},
        {"type": "object"},
    )

    assert result == LlmResult(
        text='{"candidateDecisions":[],"newDetections":[]}',
        model_name="mock-llm",
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_mock_llm_returns_injected_frozen_result() -> None:
    """생성자에 주입한 공통 결과 인스턴스를 그대로 재사용합니다."""

    expected = LlmResult(
        text="테스트 응답",
        model_name="mock-custom",
        finish_reason="length",
        usage=LlmTokenUsage(
            input_tokens=7,
            output_tokens=3,
            total_tokens=10,
        ),
    )
    backend = MockLlmBackend(expected)

    result = await backend.generate([], _deployment(), {})

    assert result is expected


def test_mock_llm_rejects_non_result_constructor_value() -> None:
    """생성자에는 검증된 공통 LlmResult만 주입할 수 있습니다."""

    with pytest.raises(TypeError, match="LlmResult"):
        MockLlmBackend({"text": "응답"})  # type: ignore[arg-type]


def test_mock_llm_satisfies_common_backend_protocol() -> None:
    """Mock 구현체를 공통 LLM Backend로 등록할 수 있습니다."""

    assert isinstance(MockLlmBackend(), LlmBackend)


@pytest.mark.asyncio
async def test_mock_llm_does_not_mutate_or_record_inputs() -> None:
    """요청 원문을 포함할 수 있는 호출 입력을 변경하거나 Backend에 보관하지 않습니다."""

    messages: list[dict[str, object]] = [
        {
            "role": "user",
            "content": "저장하면 안 되는 원문",
            "metadata": {"requestId": "request-secret"},
        }
    ]
    deployment = _deployment()
    parameters: dict[str, object] = {
        "temperature": 0,
        "stop": ["END"],
    }
    output_schema: dict[str, object] = {
        "type": "array",
        "items": {"type": "object"},
    }
    original_messages = deepcopy(messages)
    original_parameters = deepcopy(parameters)
    original_output_schema = deepcopy(output_schema)
    backend = MockLlmBackend()

    await backend.generate(
        messages,
        deployment,
        parameters,
        output_schema,
    )

    assert messages == original_messages
    assert parameters == original_parameters
    assert output_schema == original_output_schema
    backend_state = getattr(backend, "__dict__", {})
    assert messages not in backend_state.values()
    assert deployment not in backend_state.values()
    assert parameters not in backend_state.values()
    assert output_schema not in backend_state.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment", "message"),
    [
        (_deployment(kind="ner"), "kind는 llm"),
        (_deployment(adapter_type="other_llm"), "adapterType은 mock"),
    ],
)
async def test_mock_llm_rejects_wrong_deployment(
    deployment: DeploymentConfig,
    message: str,
) -> None:
    """LLM 종류와 Mock Adapter가 일치하지 않는 Deployment를 거부합니다."""

    with pytest.raises(MockLlmConfigurationError, match=message):
        await MockLlmBackend().generate([], deployment, {})


@pytest.mark.asyncio
async def test_mock_llm_rejects_disabled_deployment() -> None:
    """비활성화된 Mock LLM Deployment 실행을 거부합니다."""

    with pytest.raises(
        MockLlmConfigurationError,
        match="비활성화된 Deployment",
    ):
        await MockLlmBackend().generate(
            [],
            _deployment(enabled=False),
            {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "parameters", "output_schema", "message"),
    [
        ("invalid", {}, None, "messages는 배열"),
        (["invalid"], {}, None, r"messages\[0\]는 객체"),
        ([], "invalid", None, "parameters는 객체"),
        ([], {1: "invalid"}, None, "parameters의 키는 문자열"),
        ([], {}, "invalid", "output_schema는 객체"),
        ([], {}, {1: "invalid"}, "output_schema의 키는 문자열"),
    ],
)
async def test_mock_llm_converts_common_input_errors(
    messages: object,
    parameters: object,
    output_schema: object,
    message: str,
) -> None:
    """공통 호출 인자 오류를 Mock LLM 설정 오류로 일관되게 변환합니다."""

    with pytest.raises(MockLlmConfigurationError, match=message):
        await MockLlmBackend().generate(
            messages,  # type: ignore[arg-type]
            _deployment(),
            parameters,  # type: ignore[arg-type]
            output_schema,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mock_llm_rejects_nested_callable_without_executing_it() -> None:
    """중첩 입력의 callable을 실행하지 않고 공통 JSON 계약에서 거부합니다."""

    called = False

    def callback() -> str:
        nonlocal called
        called = True
        return "실행되면 안 됩니다"

    with pytest.raises(MockLlmConfigurationError, match="callable"):
        await MockLlmBackend().generate(
            [
                {
                    "role": "user",
                    "content": "원문",
                    "metadata": {"callback": callback},
                }
            ],
            _deployment(),
            {},
        )

    assert called is False


@pytest.mark.asyncio
async def test_mock_llm_rejects_nested_non_json_value() -> None:
    """중첩된 임의 Python 객체를 Mock에서도 JSON 입력 오류로 거부합니다."""

    with pytest.raises(
        MockLlmConfigurationError,
        match="지원하지 않는 JSON 값 타입",
    ):
        await MockLlmBackend().generate(
            [{"role": "user", "content": "원문"}],
            _deployment(),
            {"metadata": {"unsupported": object()}},
        )
