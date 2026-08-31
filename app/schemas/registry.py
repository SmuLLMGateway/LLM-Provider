"""MVP Registry 설정을 실행 시점에 검증하는 모델을 정의합니다."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    TypeAdapter,
    WithJsonSchema,
)


ResourceId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
AdapterType = Annotated[
    str,
    StringConstraints(
        min_length=1,
        pattern=r"^[a-z][a-z0-9._-]*$",
    ),
]
DeploymentKind = Literal["ner", "llm"]
STANDARD_NER_ADAPTER_TYPE = "http_ner"
_HTTP_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


def _validate_http_base_url(value: str) -> str:
    """HTTP(S) URL의 scheme과 host를 검증하고 원래 문자열을 유지합니다."""

    _HTTP_URL_ADAPTER.validate_python(value, strict=True)
    return value


HttpBaseUrl = Annotated[
    str,
    StringConstraints(min_length=1),
    AfterValidator(_validate_http_base_url),
    WithJsonSchema({"type": "string", "format": "uri"}),
]
TimeoutMs = Annotated[int, Field(ge=1, le=300_000)]
ContextTokens = Annotated[int, Field(ge=1, le=100_000_000)]


class RegistryModel(BaseModel):
    """알 수 없는 필드를 거부하고 입력을 엄격하게 검증합니다."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class _DeploymentSettings(RegistryModel):
    """내부 Deployment와 LLM 물리 파일이 공유하는 실행 설정입니다."""

    adapter_type: AdapterType = Field(alias="adapterType")
    enabled: bool

    base_url: HttpBaseUrl | None = Field(default=None, alias="baseUrl")
    model_name: NonEmptyString | None = Field(default=None, alias="modelName")
    timeout_ms: TimeoutMs | None = Field(default=None, alias="timeoutMs")
    context_window_tokens: ContextTokens | None = Field(
        default=None,
        alias="contextWindowTokens",
    )


class DeploymentConfig(_DeploymentSettings):
    """런타임에서 종류까지 확정된 Deployment 설정입니다."""

    kind: DeploymentKind


class NerDeploymentFileEntry(RegistryModel):
    """공통 HTTP 계약만 사용하는 NER 물리 파일 항목입니다."""

    base_url: HttpBaseUrl = Field(
        alias="baseUrl",
        description="호출 경로까지 포함한 NER 서버의 전체 POST Endpoint",
    )
    timeout_ms: TimeoutMs = Field(alias="timeoutMs")
    enabled: bool


class LlmDeploymentFileEntry(_DeploymentSettings):
    """LLM 물리 파일에 저장하는 kind 없는 실행 설정입니다."""


DeploymentMap = dict[ResourceId, DeploymentConfig]
NerDeploymentFileMap = dict[ResourceId, NerDeploymentFileEntry]
LlmDeploymentFileMap = dict[ResourceId, LlmDeploymentFileEntry]


class NerDeploymentRegistryFile(RootModel[NerDeploymentFileMap]):
    """어댑터 선택을 노출하지 않는 NER Deployment 파일입니다."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    def to_runtime_map(self) -> DeploymentMap:
        """NER 파일 항목에 내부 kind와 고정 Backend 키를 주입합니다."""

        return {
            deployment_id: DeploymentConfig.model_validate(
                {
                    "kind": "ner",
                    "adapterType": STANDARD_NER_ADAPTER_TYPE,
                    **deployment.model_dump(
                        by_alias=True,
                        mode="python",
                        exclude_unset=True,
                    ),
                },
                by_alias=True,
                by_name=False,
            )
            for deployment_id, deployment in self.root.items()
        }

    @classmethod
    def from_runtime_map(
        cls,
        deployments: DeploymentMap,
    ) -> Self:
        """고정 공통 HTTP NER 설정만 어댑터 키 없이 저장합니다."""

        invalid_ids = sorted(
            deployment_id
            for deployment_id, deployment in deployments.items()
            if deployment.kind != "ner"
            or deployment.adapter_type != STANDARD_NER_ADAPTER_TYPE
            or deployment.model_name is not None
        )
        if invalid_ids:
            raise ValueError(
                "NER Deployment 파일에는 공통 HTTP NER 설정만 "
                f"저장할 수 있습니다: {', '.join(invalid_ids)}"
            )

        return cls.model_validate(
            {
                deployment_id: {
                    "baseUrl": deployment.base_url,
                    "timeoutMs": deployment.timeout_ms,
                    "enabled": deployment.enabled,
                }
                for deployment_id, deployment in deployments.items()
            },
            by_alias=True,
            by_name=False,
        )


class LlmDeploymentRegistryFile(RootModel[LlmDeploymentFileMap]):
    """기존 Adapter 선택을 유지하는 LLM Deployment 파일입니다."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    def to_runtime_map(self) -> DeploymentMap:
        """LLM 파일 항목에 내부 kind를 주입합니다."""

        return {
            deployment_id: DeploymentConfig.model_validate(
                {
                    "kind": "llm",
                    **deployment.model_dump(
                        by_alias=True,
                        mode="python",
                        exclude_unset=True,
                    ),
                },
                by_alias=True,
                by_name=False,
            )
            for deployment_id, deployment in self.root.items()
        }

    @classmethod
    def from_runtime_map(
        cls,
        deployments: DeploymentMap,
    ) -> Self:
        """LLM 내부 설정에서 kind 없는 파일 모델을 만듭니다."""

        invalid_ids = sorted(
            deployment_id
            for deployment_id, deployment in deployments.items()
            if deployment.kind != "llm"
        )
        if invalid_ids:
            raise ValueError(
                "LLM Deployment 파일에는 다른 kind를 저장할 수 없습니다: "
                f"{', '.join(invalid_ids)}"
            )

        return cls.model_validate(
            {
                deployment_id: deployment.model_dump(
                    by_alias=True,
                    mode="python",
                    exclude={"kind"},
                    exclude_unset=True,
                )
                for deployment_id, deployment in deployments.items()
            },
            by_alias=True,
            by_name=False,
        )


class RegistryConfig(RegistryModel):
    """NER·LLM Deployment 파일에서 조립한 전체 실행 설정입니다."""

    deployments: DeploymentMap


__all__ = [
    "AdapterType",
    "ContextTokens",
    "DeploymentConfig",
    "DeploymentKind",
    "HttpBaseUrl",
    "LlmDeploymentFileEntry",
    "LlmDeploymentRegistryFile",
    "NerDeploymentFileEntry",
    "NerDeploymentRegistryFile",
    "NonEmptyString",
    "RegistryConfig",
    "ResourceId",
    "STANDARD_NER_ADAPTER_TYPE",
    "TimeoutMs",
]
