"""검증된 탐지 결과의 중복 제거와 결정적 정렬 정책을 검증합니다."""

from __future__ import annotations

from itertools import permutations

from app.policies.overlap_resolver import OverlapResolver
from app.schemas.detection import Detection, policy_id_for_detection_type


def _detection(
    *,
    start: int = 0,
    end: int = 3,
    text: str = "홍길동",
    detection_type: str = "PERSONAL_IDENTITY",
    source: str = "ner",
    score: float = 0.9,
) -> Detection:
    """겹침 정책 테스트에 사용할 Detection을 만듭니다."""

    return Detection.model_validate(
        {
            "start": start,
            "end": end,
            "text": text,
            "type": detection_type,
            "policyId": policy_id_for_detection_type(detection_type),
            "source": source,
            "score": score,
        }
    )


def test_resolve_prefers_source_for_same_span_and_type() -> None:
    """같은 (start, end, type)은 regex, ner, llm 순으로 하나만 남깁니다."""

    llm = _detection(source="llm", score=1.0)
    ner = _detection(source="ner", score=0.1)
    regex = _detection(source="regex", score=0.0)

    assert OverlapResolver().resolve((llm, regex, ner)) == (regex,)


def test_resolve_prefers_higher_score_within_same_source() -> None:
    """span·type·source가 같다면 더 높은 score를 보존합니다."""

    low = _detection(source="ner", score=0.2)
    high = _detection(source="ner", score=0.8)

    assert OverlapResolver().resolve((low, high)) == (high,)


def test_resolve_collapses_exact_duplicate() -> None:
    """완전히 같은 결과가 반복되어도 하나만 반환합니다."""

    item = _detection()

    assert OverlapResolver().resolve((item, item, item)) == (item,)


def test_resolve_preserves_overlapping_different_spans() -> None:
    """일부만 겹치는 서로 다른 span은 정보 손실 없이 모두 보존합니다."""

    outer = _detection(start=0, end=4, text="홍길동은")
    inner = _detection(start=0, end=3, text="홍길동")
    crossing = _detection(start=2, end=5, text="동은김")

    assert OverlapResolver().resolve((crossing, inner, outer)) == (
        outer,
        inner,
        crossing,
    )


def test_resolve_preserves_same_span_with_different_types() -> None:
    """같은 구간이어도 탐지 type이 다르면 별도 정보로 보존합니다."""

    person = _detection(detection_type="PERSONAL_IDENTITY", source="ner")
    personnel = _detection(
        detection_type="PERSONAL",
        source="llm",
    )

    assert OverlapResolver().resolve((person, personnel)) == (
        personnel,
        person,
    )


def test_resolve_preserves_touching_spans() -> None:
    """끝과 시작이 맞닿을 뿐 겹치지 않는 [start, end) 구간을 보존합니다."""

    first = _detection(start=0, end=3, text="홍길동")
    second = _detection(start=3, end=6, text="김철수")

    assert OverlapResolver().resolve((second, first)) == (first, second)


def test_resolve_applies_stable_document_order() -> None:
    """start, end, type 기준의 명시된 최종 정렬 순서를 적용합니다."""

    short = _detection(start=0, end=2, text="홍길")
    outer_person = _detection(start=0, end=3, text="홍길동")
    outer_account = _detection(
        start=0,
        end=3,
        text="홍길동",
        detection_type="FINANCE_ACCOUNT",
        source="llm",
    )
    later = _detection(start=4, end=7, text="김철수")

    result = OverlapResolver().resolve(
        (later, short, outer_person, outer_account)
    )

    assert result == (outer_account, outer_person, short, later)


def test_resolve_is_independent_of_input_order() -> None:
    """동일 집합의 입력 순서를 바꿔도 항상 같은 결과를 반환합니다."""

    items = (
        _detection(source="llm", score=1.0),
        _detection(source="regex", score=0.1),
        _detection(start=0, end=2, text="홍길", score=0.5),
        _detection(start=3, end=6, text="김철수", score=0.7),
    )
    expected = OverlapResolver().resolve(items)

    for ordered_items in permutations(items):
        assert OverlapResolver().resolve(ordered_items) == expected


def test_resolve_does_not_mutate_input_tuple_or_detection() -> None:
    """Resolver가 호출자가 소유한 순서나 Detection 값을 변경하지 않습니다."""

    low = _detection(score=0.1)
    high = _detection(score=0.9)
    original = (low, high)
    original_ids = tuple(map(id, original))

    result = OverlapResolver().resolve(original)

    assert original == (low, high)
    assert tuple(map(id, original)) == original_ids
    assert result == (high,)
