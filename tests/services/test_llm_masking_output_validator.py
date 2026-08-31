"""LLM 마스킹 출력의 coverage와 원문 보존 불변식을 검증합니다."""

from __future__ import annotations

import pytest

from app.policies.mask_target_resolver import MaskTarget
from app.services.llm_masking_output_parser import (
    MaskEntityAssignment,
    ParsedMaskingOutput,
)
from app.services.llm_masking_output_validator import (
    LlmMaskingOutputValidator,
    LlmMaskingValidationError,
)


_NAMESPACE = "0123456789abcdef"


def _target(
    target_id: str,
    start: int,
    end: int,
    text: str,
    *,
    types: tuple[str, ...] = ("PERSONAL_IDENTITY",),
    sources: tuple[str, ...] = ("ner",),
) -> MaskTarget:
    """검증할 원문 구간 하나를 MaskTarget으로 만듭니다."""

    return MaskTarget(
        target_id=target_id,
        start=start,
        end=end,
        text=text,
        types=types,
        sources=sources,  # type: ignore[arg-type]
    )


def _assignment(
    target_id: str,
    entity_id: str,
) -> MaskEntityAssignment:
    """target과 동일 대상을 연결하는 내부 assignment를 만듭니다."""

    return MaskEntityAssignment(
        target_id=target_id,
        entity_id=entity_id,
    )


def _parsed(
    masked_text: str,
    *assignments: MaskEntityAssignment,
) -> ParsedMaskingOutput:
    """Parser 검증을 통과한 것과 같은 불변 출력을 만듭니다."""

    return ParsedMaskingOutput(
        masked_text=masked_text,
        assignments=assignments,
    )


def _validate(
    original_text: str,
    targets: tuple[MaskTarget, ...],
    output: ParsedMaskingOutput,
):
    """고정 namespace로 실제 Validator를 실행합니다."""

    return LlmMaskingOutputValidator().validate(
        original_text=original_text,
        targets=targets,
        placeholder_namespace=_NAMESPACE,
        output=output,
    )


def test_validate_reuses_placeholder_for_same_entity() -> None:
    """같은 대상을 지칭하는 떨어진 target에 정확히 같은 token을 사용합니다."""

    text = "홍길동은 승인했고 홍 팀장은 검토했습니다."
    targets = (
        _target("target-1", 0, 3, "홍길동"),
        _target("target-2", 10, 14, "홍 팀장", sources=("llm",)),
    )
    placeholder = "[[LPL_0123456789abcdef_0001]]"
    expected = f"{placeholder}은 승인했고 {placeholder}은 검토했습니다."

    response = _validate(
        text,
        targets,
        _parsed(
            expected,
            _assignment("target-1", "entity-1"),
            _assignment("target-2", "entity-1"),
        ),
    )

    assert response.masked_text == expected
    assert tuple(item.entity_id for item in response.replacements) == (
        "entity-1",
        "entity-1",
    )
    assert tuple(item.placeholder for item in response.replacements) == (
        placeholder,
        placeholder,
    )


def test_validate_assigns_distinct_placeholders_by_first_appearance() -> None:
    """새 entity는 첫 등장 순서에 따라 서로 다른 token을 받습니다."""

    text = "홍길동과 김철수, 다시 홍길동"
    targets = (
        _target("target-1", 0, 3, "홍길동"),
        _target("target-2", 5, 8, "김철수"),
        _target("target-3", 13, 16, "홍길동"),
    )
    first = "[[LPL_0123456789abcdef_0001]]"
    second = "[[LPL_0123456789abcdef_0002]]"
    masked = f"{first}과 {second}, 다시 {first}"

    response = _validate(
        text,
        targets,
        _parsed(
            masked,
            _assignment("target-1", "entity-1"),
            _assignment("target-2", "entity-2"),
            _assignment("target-3", "entity-1"),
        ),
    )

    assert tuple(item.entity_id for item in response.replacements) == (
        "entity-1",
        "entity-2",
        "entity-1",
    )
    assert response.replacements[0].placeholder == first
    assert response.replacements[1].placeholder == second
    assert response.replacements[2].placeholder == first


def test_validate_preserves_derived_types_and_sources_exactly() -> None:
    """LLM이 아닌 서버 target에서 근거 메타데이터를 그대로 복원합니다."""

    text = "홍길동"
    target = _target(
        "target-1",
        0,
        3,
        text,
        types=("PERSONAL", "PERSONAL_IDENTITY"),
        sources=("regex", "ner", "llm"),
    )
    placeholder = "[[LPL_0123456789abcdef_0001]]"

    response = _validate(
        text,
        (target,),
        _parsed(
            placeholder,
            _assignment("target-1", "entity-1"),
        ),
    )

    replacement = response.replacements[0]
    assert (replacement.start, replacement.end) == (0, 3)
    assert replacement.types == ("PERSONAL", "PERSONAL_IDENTITY")
    assert replacement.sources == ("regex", "ner", "llm")


def test_validate_masks_overlap_component_once() -> None:
    """이미 병합된 겹침 합집합 target을 하나의 placeholder로 치환합니다."""

    text = "abcdefgh 뒤"
    target = _target(
        "target-1",
        0,
        8,
        "abcdefgh",
        types=("SENSITIVE_PERSONAL", "SECURITY_INFRA"),
        sources=("ner", "llm"),
    )
    placeholder = "[[LPL_0123456789abcdef_0001]]"

    response = _validate(
        text,
        (target,),
        _parsed(
            f"{placeholder} 뒤",
            _assignment("target-1", "entity-1"),
        ),
    )

    assert len(response.replacements) == 1
    assert response.replacements[0].start == 0
    assert response.replacements[0].end == 8


def test_validate_masks_only_detected_repeated_literal_occurrence() -> None:
    """같은 문자열의 미탐지 occurrence는 전역 replace하지 않고 보존합니다."""

    text = "홍길동과 홍길동"
    target = _target("target-1", 0, 3, "홍길동")
    placeholder = "[[LPL_0123456789abcdef_0001]]"

    response = _validate(
        text,
        (target,),
        _parsed(
            f"{placeholder}과 홍길동",
            _assignment("target-1", "entity-1"),
        ),
    )

    assert response.masked_text.endswith("과 홍길동")


@pytest.mark.parametrize(
    "masked_text",
    [
        "[[LPL_0123456789abcdef_0001]]은  승인했습니다.",
        "[[LPL_0123456789abcdef_0001]] 승인했습니다.",
        "[[LPL_0123456789abcdef_0001]]은 승인했어요.",
        "[[LPL_0123456789abcdef_0001]]은 승인했습니다!",
        "설명: [[LPL_0123456789abcdef_0001]]은 승인했습니다.",
        "홍길동은 승인했습니다.",
    ],
)
def test_validate_rejects_any_non_sensitive_text_mutation(
    masked_text: str,
) -> None:
    """공백·조사·문장·문장부호·추가 설명·미마스킹을 모두 거부합니다."""

    text = "홍길동은 승인했습니다."
    target = _target("target-1", 0, 3, "홍길동")

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate(
            text,
            (target,),
            _parsed(
                masked_text,
                _assignment("target-1", "entity-1"),
            ),
        )

    assert error_info.value.code == "LLM_MASKING_TEXT_MISMATCH"


def test_validate_rejects_unicode_normalization_outside_target() -> None:
    """비민감 구간의 NFC/NFD 정규화도 원문 변경으로 간주합니다."""

    text = "홍길동 e\u0301"
    target = _target("target-1", 0, 3, "홍길동")
    placeholder = "[[LPL_0123456789abcdef_0001]]"

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate(
            text,
            (target,),
            _parsed(
                f"{placeholder} é",
                _assignment("target-1", "entity-1"),
            ),
        )

    assert error_info.value.code == "LLM_MASKING_TEXT_MISMATCH"


def test_validate_preserves_unicode_and_line_breaks_byte_for_byte() -> None:
    """target 밖 이모지·결합 문자·개행·탭을 정확히 보존합니다."""

    text = "앞😀\n홍길동\te\u0301뒤"
    target = _target("target-1", 3, 6, "홍길동")
    placeholder = "[[LPL_0123456789abcdef_0001]]"
    expected = f"앞😀\n{placeholder}\te\u0301뒤"

    response = _validate(
        text,
        (target,),
        _parsed(
            expected,
            _assignment("target-1", "entity-1"),
        ),
    )

    assert response.masked_text == expected


@pytest.mark.parametrize(
    "assignments",
    [
        (),
        (
            _assignment("target-1", "entity-1"),
            _assignment("target-2", "entity-2"),
            _assignment("target-2", "entity-2"),
        ),
        (
            _assignment("target-2", "entity-1"),
            _assignment("target-1", "entity-2"),
        ),
        (
            _assignment("target-1", "entity-1"),
            _assignment("target-1", "entity-1"),
        ),
        (
            _assignment("target-1", "entity-1"),
            _assignment("unknown", "entity-2"),
        ),
    ],
)
def test_validate_rejects_missing_extra_duplicate_or_reordered_targets(
    assignments: tuple[MaskEntityAssignment, ...],
) -> None:
    """모든 target이 원문 순서로 정확히 한 번 할당돼야 합니다."""

    text = "홍길동 김철수"
    targets = (
        _target("target-1", 0, 3, "홍길동"),
        _target("target-2", 4, 7, "김철수"),
    )

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate(text, targets, _parsed("ignored", *assignments))

    assert error_info.value.code == "LLM_MASKING_TARGET_MISMATCH"


@pytest.mark.parametrize(
    "entity_ids",
    [
        ("entity-2", "entity-1"),
        ("entity-1", "entity-3"),
        ("entity-0", "entity-1"),
        ("entity-01", "entity-1"),
        ("entity-a", "entity-1"),
        ("entity-100000", "entity-1"),
    ],
)
def test_validate_rejects_invalid_or_non_contiguous_entity_order(
    entity_ids: tuple[str, str],
) -> None:
    """새 entity ID는 첫 등장 기준 entity-1부터 연속이어야 합니다."""

    text = "홍길동 김철수"
    targets = (
        _target("target-1", 0, 3, "홍길동"),
        _target("target-2", 4, 7, "김철수"),
    )

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate(
            text,
            targets,
            _parsed(
                "ignored",
                _assignment("target-1", entity_ids[0]),
                _assignment("target-2", entity_ids[1]),
            ),
        )

    assert error_info.value.code == "LLM_MASKING_ENTITY_INVALID"


def test_validate_rejects_placeholder_collision_with_original() -> None:
    """원문에 생성 token literal이 이미 있으면 결과를 사용하지 않습니다."""

    placeholder = "[[LPL_0123456789abcdef_0001]]"
    text = f"홍길동 {placeholder} 기존 값"
    target = _target("target-1", 0, 3, "홍길동")

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate(
            text,
            (target,),
            _parsed(
                f"{placeholder} {placeholder} 기존 값",
                _assignment("target-1", "entity-1"),
            ),
        )

    assert error_info.value.code == (
        "LLM_MASKING_PLACEHOLDER_COLLISION"
    )


def test_validate_empty_targets_is_identity_only() -> None:
    """빈 target 출력은 원문 항등 변환과 빈 replacement만 허용합니다."""

    response = _validate("원문 그대로", (), _parsed("원문 그대로"))

    assert response.masked_text == "원문 그대로"
    assert response.replacements == ()

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate("원문 그대로", (), _parsed("변경된 원문"))
    assert error_info.value.code == "LLM_MASKING_TEXT_MISMATCH"


@pytest.mark.parametrize(
    "namespace",
    ["", "0123", "ABCDEF0123456789", "g" * 16, "0" * 17],
)
def test_validate_rejects_invalid_placeholder_namespace(
    namespace: str,
) -> None:
    """요청 namespace는 정확한 16자리 소문자 16진수여야 합니다."""

    with pytest.raises(ValueError, match="16자리"):
        LlmMaskingOutputValidator().validate(
            original_text="원문",
            targets=(),
            placeholder_namespace=namespace,
            output=_parsed("원문"),
        )


@pytest.mark.parametrize(
    ("original_text", "targets", "output"),
    [
        (1, (), _parsed("x")),
        ("x", [], _parsed("x")),
        ("x", (object(),), _parsed("x")),
        ("x", (), object()),
    ],
)
def test_validate_rejects_wrong_internal_types(
    original_text: object,
    targets: object,
    output: object,
) -> None:
    """Parser와 target resolver 이후의 exact 내부 타입을 강제합니다."""

    with pytest.raises(TypeError):
        LlmMaskingOutputValidator().validate(  # type: ignore[arg-type]
            original_text=original_text,
            targets=targets,
            placeholder_namespace=_NAMESPACE,
            output=output,
        )


def test_validation_error_does_not_retain_sensitive_values() -> None:
    """검증 오류가 원문이나 모델 출력 문자열을 속성에 보관하지 않습니다."""

    original_secret = "ORIGINAL-SECRET-7f42"
    output_secret = "MODEL-SECRET-9c81"
    target = _target(
        "target-1",
        0,
        len(original_secret),
        original_secret,
    )

    with pytest.raises(LlmMaskingValidationError) as error_info:
        _validate(
            original_secret,
            (target,),
            _parsed(
                output_secret,
                _assignment("target-1", "entity-1"),
            ),
        )

    error = error_info.value
    exposed = (str(error), repr(error), repr(vars(error)))
    assert all(original_secret not in value for value in exposed)
    assert all(output_secret not in value for value in exposed)
    assert not hasattr(error, "original_text")
    assert not hasattr(error, "masked_text")
