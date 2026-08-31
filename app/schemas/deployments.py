"""Deployment 관리 요청과 공개 조회·Probe 응답 모델을 정의합니다."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
)

from app.schemas.registry import (
    AdapterType,
    ContextTokens,
    DeploymentConfig,
    DeploymentKind,
    HttpBaseUrl,
    NonEmptyString,
    ResourceId,
    STANDARD_NER_ADAPTER_TYPE,
    TimeoutMs,
)


ProbeLatencyMs = Annotated[FiniteFloat, Field(ge=0)]


class DeploymentResponseModel(BaseModel):
    """알 수 없는 응답 필드와 응답 객체의 재할당을 막습니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        strict=True,
    )


class DeploymentRequestModel(BaseModel):
    """관리 요청을 camelCase JSON 필드만으로 엄격하게 검증합니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class DeploymentWriteRequest(DeploymentRequestModel):
    """종류별 추가·수정 요청을 내부 실행 설정으로 조립합니다."""

    enabled: bool
    deployment_kind: ClassVar[DeploymentKind]
    fixed_adapter_type: ClassVar[str | None] = None

    def to_deployment_config(self) -> DeploymentConfig:
        """경로로 확정된 종류와 선택적 고정 Backend 키를 결합합니다."""

        payload = self.model_dump(
            by_alias=True,
            mode="python",
            exclude_unset=True,
            exclude={"deployment_id"},
        )
        if self.fixed_adapter_type is not None:
            payload["adapterType"] = self.fixed_adapter_type
        return DeploymentConfig.model_validate(
            {
                "kind": self.deployment_kind,
                **payload,
            },
            by_alias=True,
            by_name=False,
        )


class NerDeploymentWriteRequest(DeploymentWriteRequest):
    """고정 공통 HTTP 계약을 사용하는 NER 서버 설정입니다."""

    deployment_kind = "ner"
    fixed_adapter_type = STANDARD_NER_ADAPTER_TYPE
    base_url: HttpBaseUrl = Field(
        alias="baseUrl",
        description="호출 경로까지 포함한 NER 서버의 전체 POST Endpoint",
    )
    timeout_ms: TimeoutMs = Field(alias="timeoutMs")


class NerDeploymentCreateRequest(NerDeploymentWriteRequest):
    """어댑터 선택 없이 새 NER Deployment를 추가합니다."""

    deployment_id: ResourceId = Field(alias="deploymentId")


class NerDeploymentUpdateRequest(NerDeploymentWriteRequest):
    """경로로 선택한 NER Deployment 전체 설정을 교체합니다."""


class LlmDeploymentWriteRequest(DeploymentWriteRequest):
    """Adapter 선택을 유지하는 LLM 서버 실행 설정입니다."""

    deployment_kind = "llm"
    adapter_type: AdapterType = Field(alias="adapterType")
    base_url: HttpBaseUrl | None = Field(
        default=None,
        alias="baseUrl",
    )
    model_name: NonEmptyString | None = Field(
        default=None,
        alias="modelName",
    )
    timeout_ms: TimeoutMs | None = Field(
        default=None,
        alias="timeoutMs",
    )


class LlmDeploymentCreateRequest(LlmDeploymentWriteRequest):
    """클라이언트가 지정한 ID와 함께 새 LLM Deployment를 추가합니다."""

    deployment_id: ResourceId = Field(alias="deploymentId")


class LlmDeploymentUpdateRequest(LlmDeploymentWriteRequest):
    """경로로 선택한 LLM Deployment 전체 설정을 교체합니다."""


class DeploymentEnabledUpdateRequest(DeploymentRequestModel):
    """기존 Deployment의 Runtime 활성 상태만 교체합니다."""

    enabled: bool


class DeploymentSummary(DeploymentResponseModel):
    """목록 화면에 필요한 최소 Deployment 상태입니다."""

    deployment_id: ResourceId = Field(alias="deploymentId")
    enabled: bool


class NerDeploymentDetail(DeploymentSummary):
    """Gateway에 공통 NER 서버 연결 정보만 제공합니다."""

    base_url: HttpBaseUrl = Field(
        alias="baseUrl",
        description="호출 경로까지 포함한 NER 서버의 전체 POST Endpoint",
    )
    timeout_ms: TimeoutMs = Field(alias="timeoutMs")


class LlmDeploymentDetail(DeploymentSummary):
    """Gateway에 필요한 LLM Backend 실행 정보를 제공합니다."""

    adapter_type: AdapterType = Field(alias="adapterType")
    base_url: HttpBaseUrl | None = Field(
        default=None,
        alias="baseUrl",
    )
    model_name: NonEmptyString | None = Field(
        default=None,
        alias="modelName",
    )
    timeout_ms: TimeoutMs | None = Field(
        default=None,
        alias="timeoutMs",
    )


LlmLimitsSource = Literal[
    "ollama_runtime",
    "ollama_model",
    "registry",
    "unknown",
]


class LlmDeploymentLimitsResponse(DeploymentResponseModel):
    """LLM Deployment의 모델·실행·요청 제한 정보를 반환합니다."""

    deployment_id: ResourceId = Field(alias="deploymentId")
    model_context_window_tokens: ContextTokens | None = Field(
        default=None,
        alias="modelContextWindowTokens",
    )
    runtime_context_window_tokens: ContextTokens | None = Field(
        default=None,
        alias="runtimeContextWindowTokens",
    )
    effective_context_window_tokens: ContextTokens | None = Field(
        default=None,
        alias="effectiveContextWindowTokens",
    )
    source: LlmLimitsSource


DeploymentDetail = NerDeploymentDetail | LlmDeploymentDetail


class DeploymentListResponse(DeploymentResponseModel):
    """Deployment 요약 목록을 결정적인 순서로 반환합니다."""

    deployments: tuple[DeploymentSummary, ...] = ()

    @field_validator("deployments", mode="before")
    @classmethod
    def normalize_deployments(cls, value: object) -> object:
        """Python list 입력도 외부에서 변경할 수 없는 tuple로 변환합니다."""

        if type(value) is list:
            return tuple(value)
        return value


class DeploymentProbeResponse(DeploymentResponseModel):
    """실제 Backend 요청에 성공한 Deployment의 연결 상태를 반환합니다."""

    deployment_id: ResourceId = Field(alias="deploymentId")
    status: Literal["available"]
    latency_ms: ProbeLatencyMs = Field(alias="latencyMs")


__all__ = [
    "DeploymentDetail",
    "DeploymentEnabledUpdateRequest",
    "DeploymentListResponse",
    "DeploymentProbeResponse",
    "DeploymentRequestModel",
    "DeploymentSummary",
    "DeploymentWriteRequest",
    "LlmDeploymentCreateRequest",
    "LlmDeploymentDetail",
    "LlmDeploymentLimitsResponse",
    "LlmLimitsSource",
    "LlmDeploymentUpdateRequest",
    "LlmDeploymentWriteRequest",
    "NerDeploymentCreateRequest",
    "NerDeploymentDetail",
    "NerDeploymentUpdateRequest",
    "NerDeploymentWriteRequest",
]
