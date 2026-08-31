# 공통 JSON 처리

## 1. 목적

LPL은 Registry 파일, 모델 서버 응답, LLM 탐지 출력처럼 서로 다른 경계에서 JSON을
사용합니다. 각 컴포넌트가 `json.loads()`와 `json.dumps()`를 직접 호출하면 중복 키,
비표준 숫자, UTF-8 오류와 예외의 민감정보 노출을 서로 다르게 처리할 수 있습니다.

`app/core/json_codec.py`와 `app/core/json_value.py`는 JSON 문법 처리와 Python 값
검증을 애플리케이션 공통 계층으로 제공합니다.

```text
외부 JSON 문자열·바이트
        │
        ▼
load_strict_json()       문법·UTF-8·중복 키·입력 크기 검증
        │
        ▼
Pydantic/도메인 검증     필드와 참조 관계 등 의미 검증

Python 값
        │
        ├─ normalize_json_value()       JSON 호환 값 검증과 깊은 복사
        ├─ dump_json_utf8()             일반 UTF-8 JSON
        └─ dump_canonical_json_utf8()   정렬된 결정적 UTF-8 JSON
```

공통 계층은 Detection 필드, OpenAI 응답의 `choices` 또는 Deployment 실행 역할
같은 도메인 규칙을 판단하지 않습니다. 해당 검증은 Parser, Adapter, Pydantic
모델, Registry Validator와 `DeploymentResolver`가 계속 담당합니다.

## 2. 엄격한 JSON 읽기

`load_strict_json()`은 정확한 `str` 또는 `bytes`만 받습니다. `bytes`는 UTF-8로
디코딩하며 다음 입력을 거부합니다.

- UTF-8로 해석할 수 없는 바이트
- JSON 문법 오류와 JSON 뒤에 붙은 설명
- 같은 객체 안의 중복 키
- JSON 표준에 없는 `NaN`, `Infinity`, `-Infinity`
- 지수 계산 결과가 무한대가 되는 숫자와 짝이 없는 Unicode surrogate
- `max_bytes`를 초과하는 UTF-8 입력

```python
from app.core.json_codec import load_strict_json

value = load_strict_json(
    b'{"llmDeploymentId":"llm-local-a"}',
    max_bytes=16_384,
)
```

문자열 입력의 크기도 문자 수가 아니라 `len(source.encode("utf-8"))`로 계산합니다.
`max_bytes=None`이면 크기 제한을 적용하지 않습니다. 제한값을 설정할 때는 bool이
아닌 1 이상의 정수를 사용합니다.

실패하면 `StrictJsonDecodeError`가 발생합니다.

| `code` | 의미 | 추가 속성 |
|---|---|---|
| `INVALID_UTF8` | UTF-8 디코딩 실패 | 없음 |
| `INVALID_JSON` | 문법, 중복 키, 비유한 숫자 또는 잘못된 Unicode 값 | 없음 |
| `TOO_LARGE` | 입력 바이트 제한 초과 | `actual_bytes`, `max_bytes` |

오류 메시지, 오류 속성과 연결된 원인 예외에는 입력 원문을 보관하지 않습니다. 따라서
로그에는 오류 코드와 필요한 크기 정보만 기록하고, 원문을 별도로 추가하지 않아야
합니다.

## 3. UTF-8 JSON 쓰기

### 3.1 일반 직렬화

`dump_json_utf8()`은 `ensure_ascii=False`, `allow_nan=False`를 적용하고 UTF-8
`bytes`를 반환합니다. 기본 출력은 공백이 없는 compact 형식이며, `indent`를
지정하면 사람이 읽기 쉬운 형식으로 출력합니다.

```python
from app.core.json_codec import dump_json_utf8

compact = dump_json_utf8({"name": "홍길동"})
pretty = dump_json_utf8({"name": "홍길동"}, indent=2)
```

`indent`는 `None` 또는 bool이 아닌 0 이상의 정수만 허용합니다.

### 3.2 정규 직렬화

`dump_canonical_json_utf8()`은 객체 키를 정렬하고 compact 형식을 사용합니다.
같은 JSON 값을 구성한 순서가 달라도 같은 바이트가 필요할 때 사용합니다.

```python
from app.core.json_codec import dump_canonical_json_utf8

assert dump_canonical_json_utf8({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
```

대표적인 사용처는 Snapshot ID와 content hash 계산입니다. 이 함수는 암호학적
해시를 직접 계산하지 않고, 해시에 넣을 결정적인 입력 바이트만 만듭니다.

두 직렬화 함수는 비표준 숫자나 직렬화할 수 없는 객체를
`StrictJsonEncodeError`로 변환합니다. 실패한 값을 오류 객체나 원인 예외에
보관하지 않습니다.

## 4. JSON 값 정규화

`normalize_json_value()`는 신뢰할 수 없는 Python 값을 JSON 호환 값으로 제한하고
새 `dict`와 `list`로 깊은 복사합니다.

허용 값:

- `None`
- 정확한 `bool`, `int`, 유한한 `float`, `str`
- 문자열 키를 가진 `Mapping`
- `list`

거부 값:

- callable과 임의 객체
- `tuple`, `set` 등 JSON 배열이 아닌 컬렉션
- 문자열이 아닌 Mapping 키
- `NaN`, `Infinity`, `-Infinity`
- UTF-8로 인코딩할 수 없는 문자열
- 순환 참조
- JSON scalar의 사용자 정의 하위 타입

```python
from app.core.json_value import normalize_json_value

original = {"items": [{"score": 0.98}]}
normalized = normalize_json_value(original)

assert normalized == original
assert normalized is not original
assert normalized["items"] is not original["items"]
```

같은 객체가 여러 위치에서 재사용되는 것은 허용하며 각 위치에 독립적으로
복사합니다. 현재 순회 경로가 자신을 다시 참조할 때만 순환 참조로 거부합니다.

주요 오류 코드는 다음과 같습니다.

| `code` | 의미 |
|---|---|
| `CALLABLE_NOT_ALLOWED` | callable 입력 |
| `UNSUPPORTED_TYPE` | 지원하지 않는 Python 타입 |
| `NON_STRING_KEY` | 문자열이 아닌 Mapping 키 |
| `NON_FINITE_NUMBER` | `NaN` 또는 무한대 |
| `INVALID_UTF8` | UTF-8로 인코딩할 수 없는 문자열 |
| `CYCLIC_REFERENCE` | 현재 순회 경로의 순환 참조 |
| `DEPTH_EXCEEDED` | 중첩 깊이 제한 초과 |
| `ITEMS_EXCEEDED` | 전체 항목 수 제한 초과 |
| `STRING_TOO_LARGE` | 문자열 UTF-8 크기 제한 초과 |

## 5. 선택적 자원 제한

정규화 제한은 기본적으로 모두 `None`이며, 신뢰 경계의 특성에 맞게 호출자가
명시적으로 설정합니다.

| 인자 | 의미 |
|---|---|
| `max_depth` | 루트 컨테이너를 1로 계산한 최대 `dict`/`list` 중첩 깊이 |
| `max_items` | 모든 Mapping 항목과 list 원소를 합산한 최대 개수 |
| `max_string_bytes` | 문자열 값 또는 Mapping 키 하나의 최대 UTF-8 바이트 수 |

```python
normalized = normalize_json_value(
    value,
    max_depth=16,
    max_items=10_000,
    max_string_bytes=1_048_576,
)
```

제한값은 `None` 또는 bool이 아닌 1 이상의 정수만 허용합니다. 값 검증 실패는
`InvalidJsonValueError`로 보고되며 `code`와 `path`를 제공합니다. Mapping 키는
경로에 원문을 노출하지 않고 `*`로 표시합니다. 제한 오류에는 종류에 따라 다음
속성이 추가됩니다.

| 제한 | 실제값 | 설정 상한 |
|---|---|---|
| 중첩 깊이 | `actual_depth` | `max_depth` |
| 전체 항목 수 | `actual_items` | `max_items` |
| 문자열 UTF-8 크기 | `actual_bytes` | `max_bytes` |

오류는 실패한 값 자체를 포함하지 않습니다.

## 6. 사용 원칙

- JSON 문법을 읽는 코드는 `load_strict_json()`을 사용합니다.
- JSON 호환 Python 값이 필요한 경계는 `normalize_json_value()`를 사용합니다.
- 필드 구조와 업무 규칙은 Pydantic 모델과 도메인 Validator에서 검증합니다.
- 저장·응답용 일반 JSON은 `dump_json_utf8()`을 사용합니다.
- 해시 입력처럼 동일 값에 동일 바이트가 필요할 때만
  `dump_canonical_json_utf8()`을 사용합니다.
- 예외 로그에 사용자 원문이나 모델 출력을 덧붙이지 않습니다.
