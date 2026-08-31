"""정책별 고정 Prompt의 로드, 선택과 조립 계약을 검증합니다."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.core.json_codec import dump_canonical_json_utf8
from app.prompts.policy_prompt_catalog import (
    COMMON_LLM_POLICY_IDS,
    PolicyPromptCatalog,
)
from app.prompts.policy_prompt_loader import (
    DEFAULT_POLICY_PROMPTS_PATH,
    POLICY_PROMPTS_FILENAME,
    PolicyPromptLoader,
)
from app.prompts.prompt_errors import (
    InvalidPromptTemplateContentError,
    PromptOutputTooLargeError,
    PromptTemplateTooLargeError,
)
from app.prompts.prompt_limits import PromptLimits
from app.schemas.detection import ALLOWED_POLICY_IDS


def _sources() -> dict[str, str]:
    """정확한 14개 정책의 구분 가능한 테스트 본문을 만듭니다."""

    return {
        policy_id: f"RULE_{policy_id}"
        for policy_id in ALLOWED_POLICY_IDS
    }


def _catalog() -> PolicyPromptCatalog:
    """테스트용 전체 정책 Catalog를 컴파일합니다."""

    return PolicyPromptCatalog.compile(
        _sources(),
        source_path="<test-policy-prompts>",
        max_output_bytes=1024 * 1024,
    )


def test_default_policy_prompt_file_contains_exact_policy_catalog() -> None:
    """배포 기본 파일이 정의된 14개 정책을 빠짐없이 제공합니다."""

    loaded = PolicyPromptLoader().load()
    catalog = PolicyPromptCatalog.compile(
        loaded,
        source_path=str(DEFAULT_POLICY_PROMPTS_PATH),
        max_output_bytes=1024 * 1024,
    )

    assert DEFAULT_POLICY_PROMPTS_PATH.name == POLICY_PROMPTS_FILENAME
    assert tuple(catalog.prompts) == ALLOWED_POLICY_IDS
    assert all(catalog.prompts[policy_id] for policy_id in ALLOWED_POLICY_IDS)
    assert "생년월일이나 일반 날짜" in catalog.prompts["P02"]
    assert "인사평가 S등급" in catalog.prompts["B01"]
    assert "privacy.persons" in catalog.prompts["P01"]
    assert "internalSystems" in catalog.prompts["S02"]
    assert "thirdPartyContext.entities" in catalog.prompts["B02"]
    assert "confidentialTechnologyContext.assets" in catalog.prompts["B03"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.pop("P01"),
        lambda data: data.update({"UNKNOWN": "rule"}),
        lambda data: data.update({"P01": ""}),
        lambda data: data.update({"P01": "{{ unsafe }}"}),
        lambda data: data.update({"P01": "\ud800"}),
    ],
)
def test_catalog_rejects_incomplete_or_unsafe_sources(mutate) -> None:
    """누락·추가·빈 본문·Jinja·비 UTF-8 정책을 fail-closed로 거부합니다."""

    sources = _sources()
    mutate(sources)

    with pytest.raises(InvalidPromptTemplateContentError):
        PolicyPromptCatalog.compile(
            sources,
            source_path="<invalid-policy-prompts>",
            max_output_bytes=1024 * 1024,
        )


def test_catalog_selects_all_enabled_policies_once() -> None:
    """후보 유무와 관계없이 모든 활성 정책을 고정 순서로 선택합니다."""

    selected = _catalog().select_policy_ids(
        enabled_policy_ids=(
            "P01",
            "P03",
            "P05",
            "P06",
            "S01",
            "B01",
        ),
        candidate_policy_ids=("P03", "P06", "P03"),
    )

    assert selected == ("P01", "P03", "P05", "P06", "S01", "B01")
    assert COMMON_LLM_POLICY_IDS == ALLOWED_POLICY_IDS


def test_catalog_assembles_fixed_order_without_duplicates() -> None:
    """호출 순서와 무관하게 각 정책 블록을 고정 순서로 한 번만 조립합니다."""

    assembled = _catalog().assemble(("B02", "P01", "P06"))

    assert assembled.startswith("<policy_instructions>\n")
    assert assembled.endswith("\n</policy_instructions>")
    assert assembled.index("[Policy P01]") < assembled.index("[Policy P06]")
    assert assembled.index("[Policy P06]") < assembled.index("[Policy B02]")
    for policy_id in ("P01", "P06", "B02"):
        assert assembled.count(f"[Policy {policy_id}]") == 1
        assert assembled.count(f"RULE_{policy_id}") == 1


def test_catalog_is_immutable_and_rejects_bad_selection() -> None:
    """정책 본문 변경과 중복·미등록 선택을 허용하지 않습니다."""

    catalog = _catalog()

    with pytest.raises(TypeError):
        catalog.prompts["P01"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        catalog.assemble(("P01", "P01"))
    with pytest.raises(ValueError):
        catalog.assemble(("UNKNOWN",))  # type: ignore[arg-type]


def test_catalog_enforces_assembled_output_limit() -> None:
    """선택된 정책 조립 결과도 Prompt 출력 byte 상한을 넘지 못합니다."""

    sources = _sources()
    encoded = dump_canonical_json_utf8(sources)
    catalog = PolicyPromptCatalog(
        prompts=sources,
        content_hash=sha256(encoded).hexdigest(),
        max_output_bytes=10,
    )

    with pytest.raises(PromptOutputTooLargeError):
        catalog.assemble(("P01",))


def test_policy_prompt_loader_rejects_invalid_and_duplicate_json(
    tmp_path: Path,
) -> None:
    """정책 파일의 JSON 문법과 중복 키를 공통 strict decoder로 거부합니다."""

    path = tmp_path / POLICY_PROMPTS_FILENAME
    path.write_text('{"P01":"a","P01":"b"}', encoding="utf-8")

    with pytest.raises(InvalidPromptTemplateContentError):
        PolicyPromptLoader(path).load()


def test_policy_prompt_loader_applies_file_byte_limit(tmp_path: Path) -> None:
    """정책 파일도 다른 고정 Prompt와 같은 UTF-8 byte 제한을 사용합니다."""

    path = tmp_path / POLICY_PROMPTS_FILENAME
    path.write_text(json.dumps(_sources()), encoding="utf-8")
    loader = PolicyPromptLoader(
        path,
        limits=PromptLimits(max_template_bytes=4),
    )

    with pytest.raises(PromptTemplateTooLargeError):
        loader.load()
