"""실제 고정 Prompt가 NER 이후 추가 탐지 계약을 따르는지 검증합니다."""

from pathlib import Path

from app.schemas.detection import (
    ALLOWED_DETECTION_TYPES,
    CONTEXTUAL_DETECTION_TYPES,
    NER_DETECTION_TYPES,
    POLICY_ID_BY_DETECTION_TYPE,
)
from app.prompts.prompt_artifact import (
    FIXED_PROMPT_VARIABLES,
    PromptArtifact,
)
from app.prompts.prompt_loader import (
    DEFAULT_PROMPT_PATH,
    PromptLoader,
)
from app.prompts.prompt_renderer import PromptRenderer


def test_fixed_prompt_compiles_with_exact_runtime_contract() -> None:
    """config/prompts.j2가 고정 두 변수만 참조하며 컴파일됩니다."""

    source = PromptLoader().load()
    artifact = PromptArtifact.compile(
        source,
        renderer=PromptRenderer(),
        template_path=str(DEFAULT_PROMPT_PATH),
    )
    project_root = Path(__file__).resolve().parents[2]

    assert DEFAULT_PROMPT_PATH == (
        project_root / "config" / "prompts.j2"
    )
    assert artifact.compiled_template.referenced_variables == (
        FIXED_PROMPT_VARIABLES
    )


def test_fixed_prompt_matches_additional_detection_contract() -> None:
    """Prompt가 모든 후보 판정과 신규 탐지를 함께 요구합니다."""

    source = PromptLoader().load()

    assert "Gateway의 Regex 후보와 NER 후보를 판정" in source
    assert "organizationProfile" in source
    assert "organizationProfile이 null" in source
    assert "sourceType" in source
    assert "OCR_TEXT" in source
    assert "publicContext" in source
    assert "판단할 수 없으면 UNCERTAIN" in source
    assert "candidateDecisions" in source
    assert "newDetections" in source
    for decision in ("CONFIRMED", "REJECTED", "UNCERTAIN"):
        assert f'"{decision}"' in source
    assert "candidateId를 정확히 한 번씩" in source
    assert "입력에 없는 candidateId" in source
    assert "후보에 이미 있는 대상을 newDetections에 다시 출력하지 마세요" in source
    assert "text와 type이 동일한 항목" in source
    assert "연속된 문자열을 문자 단위로 그대로 복사" in source
    assert "조사, 띄어쓰기, 문장부호" in source
    assert "조사 \"은\"을 빼서 출력하면 안 됩니다" in source
    assert "활성 정책과 기존 탐지 결과 Context(JSON)" in source
    assert "<detection_context_json>" in source
    assert "</detection_context_json>" in source
    assert "순수 JSON 객체" in source
    assert "Markdown 코드 블록" in source
    assert "문자 위치나 인덱스를 계산하거나 출력하지 마세요" in source
    for field in ("text", "type", "score"):
        assert f'"{field}"' in source
    assert '"start"' not in source
    assert '"end"' not in source
    assert 'source="llm"' in source
    for detection_type in ALLOWED_DETECTION_TYPES:
        assert f'"{detection_type}"' in source
        assert f"({POLICY_ID_BY_DETECTION_TYPE[detection_type]})" in source
    for detection_type in NER_DETECTION_TYPES:
        assert detection_type in source
    assert "NER가 놓친 개체만 보완하세요" in source
    assert "Regex가 놓친 비표준 형식만 보완하세요" in source
    assert "policyId는 출력하지 마세요" in source
    assert "enabledPolicies에 없는 type은 절대 출력하지 마세요" in source
    assert "원문에서 정확히 한 번만 나타나도록" in source
    assert "여러 번 나타나면 LPL이 전체 출력을 거부" in source
    assert len(CONTEXTUAL_DETECTION_TYPES) == 7
    assert "privacy.persons가 비어 있지 않으면" in source
    assert "등록값 우선 규칙" in source
    assert "반드시 SECURITY_INFRA" in source
    assert "반드시 R&D" in source
    assert "반드시 CLIENT" in source
    assert "허용 목록에 없는 type" in source
    assert '{"candidateDecisions":[],"newDetections":[]}' in source


def test_fixed_prompt_renders_text_and_serialized_evidence() -> None:
    """실제 Prompt Artifact가 원문과 JSON 근거 문자열을 그대로 삽입합니다."""

    source = PromptLoader().load()
    artifact = PromptArtifact.compile(
        source,
        renderer=PromptRenderer(),
        template_path=str(DEFAULT_PROMPT_PATH),
    )
    text = "홍길동은 프로젝트 알파를 담당합니다."
    evidence = (
        '{"enabledPolicies":[{"policyId":"P01",'
        '"type":"PERSONAL_IDENTITY"}],"regexCandidates":[],'
        '"nerCandidates":[{"candidateId":"N001","start":0,'
        '"end":3,"text":"홍길동","policyId":"P01",'
        '"entityType":"PERSON","score":0.9}]}'
    )

    rendered = artifact.render(
        text=text,
        existing_detections=evidence,
    )

    assert f"<original_text>\n{text}\n</original_text>" in rendered
    assert (
        "<detection_context_json>\n"
        f"{evidence}\n"
        "</detection_context_json>"
    ) in rendered
