"""Detection을 비중첩 MaskTarget으로 만드는 정책을 검증합니다."""

from __future__ import annotations

from itertools import permutations

import pytest

from app.policies.mask_target_resolver import MaskTargetResolver
from app.schemas.detection import Detection, policy_id_for_detection_type


def _detection(
    text: str,
    start: int,
    end: int,
    *,
    detection_type: str = "PERSONAL_IDENTITY",
    source: str = "ner",
    score: float = 0.9,
) -> Detection:
    """주어진 원문 좌표와 정확히 일치하는 Detection을 만듭니다."""

    return Detection.model_validate(
        {
            "start": start,
            "end": end,
            "text": text[start:end],
            "type": detection_type,
            "policyId": policy_id_for_detection_type(detection_type),
            "source": source,
            "score": score,
        }
    )


def test_resolve_returns_empty_tuple_for_empty_detections() -> None:
    """마스킹할 Detection이 없으면 대상도 만들지 않습니다."""

    assert MaskTargetResolver().resolve("민감정보 없음", ()) == ()


def test_resolve_keeps_separate_spans_in_document_order() -> None:
    """입력 순서와 관계없이 서로 떨어진 span을 원문 순서로 반환합니다."""

    text = "홍길동과 김철수"
    later = _detection(text, 5, 8)
    earlier = _detection(text, 0, 3)

    targets = MaskTargetResolver().resolve(text, (later, earlier))

    assert tuple((item.target_id, item.start, item.end) for item in targets) == (
        ("target-1", 0, 3),
        ("target-2", 5, 8),
    )
    assert tuple(item.text for item in targets) == ("홍길동", "김철수")


def test_resolve_merges_nested_and_crossing_overlap_components() -> None:
    """전이적으로 겹치는 모든 Detection을 최대 합집합 구간 하나로 만듭니다."""

    text = "abcdefghijk"
    first = _detection(
        text,
        0,
        5,
        detection_type="SECURITY_INFRA",
        source="ner",
    )
    crossing = _detection(
        text,
        3,
        8,
        detection_type="PERSONAL",
        source="llm",
    )
    nested = _detection(
        text,
        4,
        6,
        detection_type="SENSITIVE_PERSONAL",
        source="regex",
    )

    targets = MaskTargetResolver().resolve(
        text,
        (crossing, nested, first),
    )

    assert len(targets) == 1
    target = targets[0]
    assert (target.start, target.end, target.text) == (0, 8, "abcdefgh")
    assert target.types == ("PERSONAL", "SECURITY_INFRA", "SENSITIVE_PERSONAL")
    assert target.sources == ("regex", "ner", "llm")


def test_resolve_does_not_merge_touching_spans() -> None:
    """반개방 구간의 끝과 시작이 같은 Detection은 별도 target입니다."""

    text = "홍길동김철수"
    first = _detection(text, 0, 3, source="regex")
    second = _detection(text, 3, 6, source="llm")

    targets = MaskTargetResolver().resolve(text, (second, first))

    assert tuple((item.start, item.end) for item in targets) == (
        (0, 3),
        (3, 6),
    )
    assert targets[0].sources == ("regex",)
    assert targets[1].sources == ("llm",)


def test_resolve_does_not_cover_gap_between_sensitive_spans() -> None:
    """두 span 사이 비민감 문자를 component에 포함하지 않습니다."""

    text = "비밀X기밀"
    first = _detection(text, 0, 2)
    second = _detection(text, 3, 5)

    targets = MaskTargetResolver().resolve(text, (first, second))

    assert tuple(item.text for item in targets) == ("비밀", "기밀")
    assert all(not (item.start <= 2 < item.end) for item in targets)


def test_resolve_deduplicates_types_and_sources_deterministically() -> None:
    """같은 component의 근거는 중복 없이 지정된 순서로 정규화합니다."""

    text = "홍길동"
    detections = (
        _detection(text, 0, 3, detection_type="PERSONAL", source="llm"),
        _detection(text, 0, 3, detection_type="SENSITIVE_PERSONAL", source="ner"),
        _detection(text, 0, 3, detection_type="SENSITIVE_PERSONAL", source="regex"),
        _detection(text, 0, 3, detection_type="SENSITIVE_PERSONAL", source="ner"),
    )

    target = MaskTargetResolver().resolve(text, detections)[0]

    assert target.types == ("PERSONAL", "SENSITIVE_PERSONAL")
    assert target.sources == ("regex", "ner", "llm")


def test_resolve_is_independent_of_detection_input_order() -> None:
    """동일 Detection 집합의 순열은 항상 동일 target을 만듭니다."""

    text = "0123456789"
    detections = (
        _detection(text, 0, 4, detection_type="CLIENT", source="llm"),
        _detection(text, 2, 6, detection_type="SENSITIVE_PERSONAL", source="regex"),
        _detection(text, 8, 10, detection_type="PAYMENT", source="ner"),
    )
    expected = MaskTargetResolver().resolve(text, detections)

    for ordered in permutations(detections):
        assert MaskTargetResolver().resolve(text, ordered) == expected


def test_resolve_preserves_unicode_code_point_slices() -> None:
    """이모지와 결합 문자가 포함된 target text를 문자 좌표로 복원합니다."""

    text = "가😀나e\u0301끝"
    detection = _detection(text, 1, 5, detection_type="PERSONAL")

    target = MaskTargetResolver().resolve(text, (detection,))[0]

    assert target.text == "😀나e\u0301"
    assert (target.start, target.end) == (1, 5)


def test_resolve_does_not_mutate_input_detection_tuple() -> None:
    """정렬과 component 생성 중 호출자 소유 입력을 변경하지 않습니다."""

    text = "홍길동 김철수"
    first = _detection(text, 0, 3)
    second = _detection(text, 4, 7)
    detections = (second, first)

    MaskTargetResolver().resolve(text, detections)

    assert detections == (second, first)
    assert tuple(map(id, detections)) == (id(second), id(first))


@pytest.mark.parametrize(
    ("original_text", "detections"),
    [
        (1, ()),
        ("홍길동", []),
        ("홍길동", (object(),)),
    ],
)
def test_resolve_rejects_unvalidated_input_container(
    original_text: object,
    detections: object,
) -> None:
    """정확한 str과 Detection tuple이라는 내부 경계 계약을 강제합니다."""

    with pytest.raises(TypeError):
        MaskTargetResolver().resolve(  # type: ignore[arg-type]
            original_text,
            detections,
        )
