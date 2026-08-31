"""Adapter 카탈로그 서비스의 이름 목록 계약을 검증합니다."""

from app.backends.backend_registry import (
    BackendRegistration,
    BackendRegistry,
)
from app.backends.provider_registry import (
    BackendProviderRegistration,
    BackendProviderRegistry,
)
from app.services.adapter_catalog import AdapterCatalogService


class _StubNerBackend:
    """Provider 등록 계약만 만족하는 NER 테스트 대역입니다."""

    async def detect(self, text, deployment):
        """고정된 빈 탐지 결과를 반환합니다."""

        return []


class _StubLlmBackend:
    """Provider 등록 계약만 만족하는 LLM 테스트 대역입니다."""

    async def generate(
        self,
        messages,
        deployment,
        parameters,
        output_schema=None,
    ):
        """호출되지 않는 테스트용 생성 메서드입니다."""

        raise AssertionError("Adapter 목록 조회는 Backend를 호출하지 않습니다")


def _make_service() -> AdapterCatalogService:
    """계약과 Provider의 교집합을 검증할 Registry를 조립합니다."""

    backend_registry = BackendRegistry(
        [
            BackendRegistration(
                kind="ner",
                adapter_type="zeta_contract_only",
            ),
            BackendRegistration(
                kind="llm",
                adapter_type="alpha",
            ),
            BackendRegistration(
                kind="ner",
                adapter_type="alpha",
            ),
        ]
    )
    provider_registry = BackendProviderRegistry(
        [
            BackendProviderRegistration(
                kind="ner",
                adapter_type="alpha",
                provider=_StubNerBackend(),
            ),
            BackendProviderRegistration(
                kind="llm",
                adapter_type="alpha",
                provider=_StubLlmBackend(),
            ),
            BackendProviderRegistration(
                kind="ner",
                adapter_type="provider_only",
                provider=_StubNerBackend(),
            ),
        ]
    )
    return AdapterCatalogService(
        backend_registry,
        provider_registry,
    )


def test_list_adapters_returns_sorted_names_for_each_kind() -> None:
    """NER와 LLM 목록을 종류별 문자열 배열로 분리해 반환합니다."""

    service = _make_service()

    assert service.list_adapters("ner").adapters == ("alpha",)
    assert service.list_adapters("llm").adapters == ("alpha",)


def test_list_adapters_requires_contract_and_provider_registration() -> None:
    """계약 또는 구현체 중 하나만 등록된 Adapter는 목록에서 제외합니다."""

    response = _make_service().list_adapters("ner")

    assert "zeta_contract_only" not in response.adapters
    assert "provider_only" not in response.adapters


def test_adapter_response_exposes_only_adapter_name_array() -> None:
    """내부 계약과 Provider 정보 없이 Adapter 이름만 직렬화합니다."""

    dumped = _make_service().list_adapters("ner").model_dump(
        by_alias=True,
        mode="json",
    )

    assert dumped == {"adapters": ["alpha"]}
