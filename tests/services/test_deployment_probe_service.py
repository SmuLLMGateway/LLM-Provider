"""Deployment Probe 서비스의 NER·LLM 최소 실제 요청을 검증합니다."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from app.backends.errors import BackendTransportError
from app.backends.llm.mock import MockLlmBackend
from app.backends.llm.openai_compatible import (
    OpenAICompatibleLlmBackend,
)
from app.backends.ner.gliner import GlinerNerBackend
from app.backends.ner.hf_inference_token_classification import (
    HfInferenceTokenClassificationNerBackend,
)
from app.backends.ner.http import HttpNerBackend
from app.backends.ner.mock import MockNerBackend
from app.backends.provider_registry import (
    BackendProviderLookupError,
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.policies.span_validator import DetectionSpanValidationError
from app.registry.deployment_resolver import DeploymentResolutionError
from app.schemas.detection import Detection
from app.schemas.generation import LlmResult, LlmTokenUsage
from app.schemas.registry import DeploymentConfig
from app.services.deployment_probe import (
    DeploymentProbeService,
    LLM_PROBE_MAX_TOKENS,
    LLM_PROBE_TEXT,
    LlmProbeBackendResultError,
    NER_PROBE_TEXT,
)


@dataclass(slots=True)
class StaticSnapshotManager:
    """지정한 Deployment Map을 하나의 Snapshot처럼 반환합니다."""

    deployments: Mapping[str, DeploymentConfig]
    capture_count: int = 0

    def capture(self) -> SimpleNamespace:
        """현재 Snapshot 조회 횟수를 기록합니다."""

        self.capture_count += 1
        return SimpleNamespace(deployments=self.deployments)


@dataclass(slots=True)
class RecordingNerBackend:
    """Probe 입력과 Deployment를 기록한 뒤 예약된 결과를 반환합니다."""

    result: object = field(default_factory=list)
    error: Exception | None = None
    calls: list[tuple[str, DeploymentConfig]] = field(
        default_factory=list
    )

    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> object:
        """호출 정보를 기록하고 결과 또는 오류를 전달합니다."""

        self.calls.append((text, deployment))
        if self.error is not None:
            raise self.error
        return self.result


@dataclass(slots=True)
class RecordingLlmBackend:
    """Probe의 LLM 호출 인자를 기록하고 예약된 결과를 반환합니다."""

    result: object = field(
        default_factory=lambda: LlmResult(text="private model output")
    )
    error: Exception | None = None
    mutate_inputs: bool = False
    calls: list[
        tuple[
            list[dict[str, object]],
            DeploymentConfig,
            dict[str, object],
            dict[str, object] | None,
        ]
    ] = field(default_factory=list)

    async def generate(
        self,
        messages: list[dict[str, object]],
        deployment: DeploymentConfig,
        parameters: dict[str, object],
        output_schema: dict[str, object] | None = None,
    ) -> object:
        """호출 당시 입력을 복사해 보관한 뒤 결과 또는 오류를 전달합니다."""

        self.calls.append(
            (
                deepcopy(messages),
                deployment,
                deepcopy(parameters),
                deepcopy(output_schema),
            )
        )
        if self.mutate_inputs:
            messages.clear()
            parameters.clear()
        if self.error is not None:
            raise self.error
        return self.result


def _deployment(
    *,
    kind: str = "ner",
    adapter_type: str = "recording",
    enabled: bool = True,
    base_url: str | None = None,
) -> DeploymentConfig:
    """테스트에 필요한 최소 Deployment 설정을 만듭니다."""

    data: dict[str, object] = {
        "kind": kind,
        "adapterType": adapter_type,
        "enabled": enabled,
    }
    if base_url is not None:
        data["baseUrl"] = base_url
        data["timeoutMs"] = 1_000
    if kind == "llm" and adapter_type == "openai_compatible":
        data["modelName"] = "test-model"
    return DeploymentConfig.model_validate(data)


def _providers(
    adapter_type: str,
    backend: object,
) -> BackendProviderRegistry:
    """NER Adapter 하나가 등록된 Provider Registry를 만듭니다."""

    return BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type=adapter_type,
                provider=backend,
            )
        ]
    )


def _llm_providers(
    adapter_type: str,
    backend: object,
) -> BackendProviderRegistry:
    """LLM Adapter 하나가 등록된 Provider Registry를 만듭니다."""

    return BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="llm",
                adapter_type=adapter_type,
                provider=backend,
            )
        ]
    )


@pytest.mark.asyncio
async def test_probe_calls_selected_backend_with_minimal_text_and_times_it() -> None:
    """고정 최소 입력으로 한 번 호출하고 전체 실행 지연시간을 반환합니다."""

    deployment = _deployment()
    manager = StaticSnapshotManager({"ner-a": deployment})
    backend = RecordingNerBackend()
    timestamps = iter((10.0, 10.012345))
    service = DeploymentProbeService(
        registry_manager=manager,
        backend_providers=_providers("recording", backend),
        clock=lambda: next(timestamps),
    )

    result = await service.probe_ner(deployment_id="ner-a")

    assert NER_PROBE_TEXT == "A"
    assert result.deployment_id == "ner-a"
    assert result.status == "available"
    assert result.latency_ms == pytest.approx(12.345)
    assert manager.capture_count == 1
    assert backend.calls == [(NER_PROBE_TEXT, deployment)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        [],
        [
            Detection(
                start=0,
                end=1,
                text="A",
                type="PERSONAL_IDENTITY",
                policyId="P01",
                source="ner",
                score=0.9,
            )
        ],
    ],
)
async def test_probe_accepts_empty_or_valid_detection_results(
    result: list[Detection],
) -> None:
    """탐지 개수와 관계없이 공통 Span 계약을 통과하면 성공합니다."""

    deployment = _deployment()
    backend = RecordingNerBackend(result=result)
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-a": deployment}
        ),
        backend_providers=_providers("recording", backend),
    )

    response = await service.probe_ner(deployment_id="ner-a")

    assert response.status == "available"


@pytest.mark.asyncio
async def test_probe_rejects_invalid_backend_detection_result() -> None:
    """최소 원문의 범위를 벗어난 Backend 결과를 성공으로 처리하지 않습니다."""

    deployment = _deployment()
    backend = RecordingNerBackend(
        result=[
            Detection(
                start=0,
                end=2,
                text="AB",
                type="PERSONAL_IDENTITY",
                policyId="P01",
                source="ner",
                score=0.9,
            )
        ]
    )
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-a": deployment}
        ),
        backend_providers=_providers("recording", backend),
    )

    with pytest.raises(DetectionSpanValidationError) as error_info:
        await service.probe_ner(deployment_id="ner-a")

    assert error_info.value.code == "INVALID_SPAN"


@pytest.mark.asyncio
async def test_probe_propagates_backend_errors_without_rewriting_them() -> None:
    """공통 Backend 오류는 기존 HTTP 오류 Handler가 분류하도록 유지합니다."""

    deployment = _deployment()
    expected = BackendTransportError(
        "TEST_NER_TRANSPORT_ERROR",
        "internal endpoint detail",
    )
    backend = RecordingNerBackend(error=expected)
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-a": deployment}
        ),
        backend_providers=_providers("recording", backend),
    )

    with pytest.raises(BackendTransportError) as error_info:
        await service.probe_ner(deployment_id="ner-a")

    assert error_info.value is expected


@pytest.mark.asyncio
async def test_probe_calls_disabled_ner_with_enabled_execution_copy() -> None:
    """비활성 NER도 원본 설정을 바꾸지 않고 실제 연결을 점검합니다."""

    deployment = _deployment(enabled=False)
    backend = RecordingNerBackend()
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-disabled": deployment}
        ),
        backend_providers=_providers("recording", backend),
    )

    response = await service.probe_ner(
        deployment_id="ner-disabled"
    )

    assert response.status == "available"
    assert deployment.enabled is False
    assert len(backend.calls) == 1
    text, probe_config = backend.calls[0]
    assert text == NER_PROBE_TEXT
    assert probe_config is not deployment
    assert probe_config.enabled is True
    assert probe_config == deployment.model_copy(
        update={"enabled": True}
    )


@pytest.mark.asyncio
async def test_disabled_ner_probe_passes_real_backend_runnable_guard() -> None:
    """Probe 실행용 복사본은 실제 NER Backend의 활성 상태 검사를 통과합니다."""

    deployment = _deployment(
        adapter_type="mock",
        enabled=False,
    )
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-disabled": deployment}
        ),
        backend_providers=_providers(
            "mock",
            MockNerBackend(),
        ),
    )

    response = await service.probe_ner(
        deployment_id="ner-disabled"
    )

    assert response.status == "available"
    assert deployment.enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment_id", "deployments"),
    [
        ("unknown", {}),
        (
            "llm-a",
            {
                "llm-a": _deployment(
                    kind="llm",
                    adapter_type="openai_compatible",
                )
            },
        ),
    ],
)
async def test_probe_hides_missing_or_non_ner_deployment_as_not_found(
    deployment_id: str,
    deployments: Mapping[str, DeploymentConfig],
) -> None:
    """존재하지 않거나 NER가 아닌 ID는 같은 Not Found 오류를 사용합니다."""

    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(deployments),
        backend_providers=BackendProviderRegistry(),
    )

    with pytest.raises(DeploymentResolutionError) as error_info:
        await service.probe_ner(deployment_id=deployment_id)

    assert error_info.value.code == "DEPLOYMENT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_probe_reports_missing_provider_before_timing(
    enabled: bool,
) -> None:
    """선택한 Adapter 구현체가 없으면 Provider 조회 오류를 유지합니다."""

    timestamps: list[float] = []
    deployment = _deployment(enabled=enabled)
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-a": deployment}
        ),
        backend_providers=BackendProviderRegistry(),
        clock=lambda: timestamps.append(1.0) or 1.0,
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        await service.probe_ner(deployment_id="ner-a")

    assert error_info.value.code == "BACKEND_PROVIDER_NOT_REGISTERED"
    assert timestamps == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "response_data"),
    [
        (
            "http_ner",
            {"detections": []},
        ),
        (
            "hf_inference_token_classification",
            [],
        ),
        (
            "gliner_http",
            {"text": NER_PROBE_TEXT, "entities": []},
        ),
    ],
)
async def test_probe_uses_each_http_adapter_real_request_contract(
    adapter_type: str,
    response_data: object,
) -> None:
    """Probe 입력을 각 Adapter 고유의 실제 POST 형식으로 변환합니다."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """외부 전송 없이 실제 Adapter 요청 형식을 기록합니다."""

        requests.append(request)
        return httpx.Response(200, json=response_data)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        trust_env=False,
    ) as client:
        backend = {
            "http_ner": HttpNerBackend(client),
            "hf_inference_token_classification": (
                HfInferenceTokenClassificationNerBackend(client)
            ),
            "gliner_http": GlinerNerBackend(client),
        }[adapter_type]
        base_url = (
            "http://ner.example.test/v1/ner/detect"
            if adapter_type == "http_ner"
            else "http://ner.example.test/ner"
        )
        deployment = _deployment(
            adapter_type=adapter_type,
            base_url=base_url,
        )
        service = DeploymentProbeService(
            registry_manager=StaticSnapshotManager(
                {"ner-http": deployment}
            ),
            backend_providers=_providers(adapter_type, backend),
        )

        result = await service.probe_ner(deployment_id="ner-http")

    assert result.status == "available"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    expected_url = (
        "http://ner.example.test/v1/ner/detect"
        if adapter_type == "http_ner"
        else "http://ner.example.test/ner"
    )
    assert str(request.url) == expected_url
    assert request.headers["content-type"] == "application/json"
    payload = json.loads(request.content)
    if adapter_type == "http_ner":
        assert payload == {"text": NER_PROBE_TEXT}
    elif adapter_type == "hf_inference_token_classification":
        assert payload == {"inputs": NER_PROBE_TEXT}
    else:
        assert payload == {
            "text": NER_PROBE_TEXT,
            "labels": ["사람", "회사", "조직", "주소", "장소"],
            "threshold": 0.4,
        }


@pytest.mark.asyncio
async def test_llm_probe_calls_backend_with_one_token_limit_and_times_it() -> None:
    """고정 메시지와 1토큰 제한으로 실제 생성을 한 번 실행합니다."""

    deployment = _deployment(
        kind="llm",
        adapter_type="recording_llm",
    )
    manager = StaticSnapshotManager({"llm-a": deployment})
    backend = RecordingLlmBackend()
    timestamps = iter((20.0, 20.012345))
    service = DeploymentProbeService(
        registry_manager=manager,
        backend_providers=_llm_providers(
            "recording_llm",
            backend,
        ),
        clock=lambda: next(timestamps),
    )

    response = await service.probe_llm(deployment_id="llm-a")

    assert LLM_PROBE_TEXT == "A"
    assert LLM_PROBE_MAX_TOKENS == 1
    assert response.deployment_id == "llm-a"
    assert response.status == "available"
    assert response.latency_ms == pytest.approx(12.345)
    assert manager.capture_count == 1
    assert backend.calls == [
        (
            [{"role": "user", "content": "A"}],
            deployment,
            {"max_tokens": 1},
            None,
        )
    ]
    assert "private model output" not in repr(response)


@pytest.mark.asyncio
async def test_llm_probe_creates_fresh_inputs_for_every_call() -> None:
    """Backend가 입력을 변경해도 다음 Probe 요청에는 영향을 주지 않습니다."""

    deployment = _deployment(
        kind="llm",
        adapter_type="recording_llm",
    )
    backend = RecordingLlmBackend(mutate_inputs=True)
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"llm-a": deployment}
        ),
        backend_providers=_llm_providers(
            "recording_llm",
            backend,
        ),
    )

    await service.probe_llm(deployment_id="llm-a")
    await service.probe_llm(deployment_id="llm-a")

    assert [call[0] for call in backend.calls] == [
        [{"role": "user", "content": "A"}],
        [{"role": "user", "content": "A"}],
    ]
    assert [call[2] for call in backend.calls] == [
        {"max_tokens": 1},
        {"max_tokens": 1},
    ]


@pytest.mark.asyncio
async def test_llm_probe_executes_mock_backend_contract() -> None:
    """Mock LLM도 같은 공통 generate 계약으로 Probe할 수 있습니다."""

    deployment = _deployment(
        kind="llm",
        adapter_type="mock",
    )
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"llm-mock": deployment}
        ),
        backend_providers=_llm_providers(
            "mock",
            MockLlmBackend(),
        ),
    )

    response = await service.probe_llm(
        deployment_id="llm-mock"
    )

    assert response.status == "available"


@pytest.mark.asyncio
async def test_llm_probe_uses_openai_compatible_real_request_contract() -> None:
    """OpenAI 호환 서버에 최소 Chat Completions 요청을 실제로 구성합니다."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """외부 전송 없이 OpenAI 호환 요청을 기록합니다."""

        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "B"},
                        "finish_reason": "length",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    ) as client:
        backend = OpenAICompatibleLlmBackend(client)
        deployment = _deployment(
            kind="llm",
            adapter_type="openai_compatible",
            base_url="http://llm.example.test/v1",
        )
        service = DeploymentProbeService(
            registry_manager=StaticSnapshotManager(
                {"llm-openai": deployment}
            ),
            backend_providers=_llm_providers(
                "openai_compatible",
                backend,
            ),
        )

        response = await service.probe_llm(
            deployment_id="llm-openai"
        )

    assert response.status == "available"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "http://llm.example.test/v1/chat/completions"
    )
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": "A",
            }
        ],
        "max_tokens": 1,
        "stream": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {},
        LlmResult.model_construct(text=123),
        LlmResult.model_construct(
            text="B",
            usage=LlmTokenUsage.model_construct(
                input_tokens=-1,
                output_tokens=1,
                total_tokens=0,
            ),
        ),
    ],
)
async def test_llm_probe_rejects_unvalidated_backend_results(
    result: object,
) -> None:
    """Pydantic 검증을 우회한 결과도 Probe 성공으로 처리하지 않습니다."""

    deployment = _deployment(
        kind="llm",
        adapter_type="recording_llm",
    )
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"llm-a": deployment}
        ),
        backend_providers=_llm_providers(
            "recording_llm",
            RecordingLlmBackend(result=result),
        ),
    )

    with pytest.raises(LlmProbeBackendResultError) as error_info:
        await service.probe_llm(deployment_id="llm-a")

    assert error_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_llm_probe_calls_disabled_with_enabled_execution_copy() -> None:
    """비활성 LLM도 원본 설정을 바꾸지 않고 실제 연결을 점검합니다."""

    deployment = _deployment(
        kind="llm",
        adapter_type="recording_llm",
        enabled=False,
    )
    backend = RecordingLlmBackend()
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"llm-disabled": deployment}
        ),
        backend_providers=_llm_providers(
            "recording_llm",
            backend,
        ),
    )

    response = await service.probe_llm(
        deployment_id="llm-disabled"
    )

    assert response.status == "available"
    assert deployment.enabled is False
    assert len(backend.calls) == 1
    messages, probe_config, parameters, output_schema = (
        backend.calls[0]
    )
    assert messages == [
        {
            "role": "user",
            "content": LLM_PROBE_TEXT,
        }
    ]
    assert parameters == {
        "max_tokens": LLM_PROBE_MAX_TOKENS,
    }
    assert output_schema is None
    assert probe_config is not deployment
    assert probe_config.enabled is True
    assert probe_config == deployment.model_copy(
        update={"enabled": True}
    )


@pytest.mark.asyncio
async def test_disabled_llm_probe_passes_real_backend_runnable_guard() -> None:
    """Probe 실행용 복사본은 실제 LLM Backend의 활성 상태 검사를 통과합니다."""

    deployment = _deployment(
        kind="llm",
        adapter_type="mock",
        enabled=False,
    )
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"llm-disabled": deployment}
        ),
        backend_providers=_llm_providers(
            "mock",
            MockLlmBackend(),
        ),
    )

    response = await service.probe_llm(
        deployment_id="llm-disabled"
    )

    assert response.status == "available"
    assert deployment.enabled is False


@pytest.mark.asyncio
async def test_llm_probe_hides_ner_deployment_as_not_found() -> None:
    """LLM 경로에 전달한 NER ID는 존재하지 않는 것처럼 처리합니다."""

    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"ner-a": _deployment()}
        ),
        backend_providers=BackendProviderRegistry(),
    )

    with pytest.raises(DeploymentResolutionError) as error_info:
        await service.probe_llm(deployment_id="ner-a")

    assert error_info.value.code == "DEPLOYMENT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_llm_probe_reports_missing_provider_before_timing(
    enabled: bool,
) -> None:
    """LLM Provider가 없으면 실제 호출과 지연시간 측정을 시작하지 않습니다."""

    timestamps: list[float] = []
    deployment = _deployment(
        kind="llm",
        adapter_type="recording_llm",
        enabled=enabled,
    )
    service = DeploymentProbeService(
        registry_manager=StaticSnapshotManager(
            {"llm-a": deployment}
        ),
        backend_providers=BackendProviderRegistry(),
        clock=lambda: timestamps.append(1.0) or 1.0,
    )

    with pytest.raises(BackendProviderLookupError) as error_info:
        await service.probe_llm(deployment_id="llm-a")

    assert error_info.value.code == "BACKEND_PROVIDER_NOT_REGISTERED"
    assert timestamps == []
