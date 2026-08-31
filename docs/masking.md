# Masking API

## 1. 목적과 책임 경계

`POST /mask`는 Gateway가 통합한 개인정보·민감정보 Detection을 근거로 Local LLM이
같은 대상을 그룹화하고 외부 LLM Provider에 전달할 마스킹 문장을 만드는 내부
Runtime API다.

```text
Gateway
├─ POST /detect의 CONFIRMED Regex와 detections를 전체 병합
├─ 사용자 Option B 선택 후 POST /mask 호출
│        │
│        ▼
│    LPL Masking Pipeline
│    ├─ 입력 Detection 검증과 겹친 span의 합집합 component 계산
│    ├─ Local LLM으로 동일 대상 그룹과 placeholder 결정
│    └─ 전체 coverage와 비탐지 원문 불변성을 fail-closed 검증
│        │
│        ▼
├─ maskedText 수신
└─ maskedText만 외부 LLM Provider에 전달
```

Gateway는 사용자 인증, Option B 선택, 전체 Detection 병합, 외부 Provider 호출과
요청 상태를 담당한다. LPL은 Local LLM 실행과 마스킹 결과 검증까지만 담당하며 외부
Provider를 호출하지 않는다.

`/mask`는 추가 탐지 Endpoint가 아니다. 요청의 `detections` 밖에 있는 문자열을 새로
마스킹하지 않으므로 Gateway는 `candidateDecisions`의 CONFIRMED Regex와 `/detect`가
반환한 `detections`를 빠짐없이 전달해야 한다. REJECTED와 검토 전 UNCERTAIN 후보는
자동으로 포함하지 않는다. 탐지되지 않은 민감정보의 안전성은 `/mask`만으로 보장할 수
없다.

## 2. Endpoint

```http
POST /mask
Content-Type: application/json
```

성공 시 `200 OK`와 `MaskResponse`를 반환한다. 스트리밍은 지원하지 않는다.

## 3. 요청 계약

```json
{
  "text": "홍길동은 보고서를 작성했고 홍 팀장은 이를 검토했습니다.",
  "llmDeploymentId": "llm-local-a",
  "detections": [
    {
      "start": 0,
      "end": 3,
      "text": "홍길동",
      "type": "PERSONAL_IDENTITY",
      "policyId": "P01",
      "source": "ner",
      "score": 0.99
    },
    {
      "start": 15,
      "end": 19,
      "text": "홍 팀장",
      "type": "PERSONAL_IDENTITY",
      "policyId": "P01",
      "source": "llm",
      "score": 0.91
    }
  ]
}
```

| 필드 | JSON 타입 | 필수 | 설명 |
|---|---|---:|---|
| `text` | string | O | 마스킹할 원문. 빈 문자열은 허용하지 않는다. |
| `llmDeploymentId` | string | O | 마스킹에 사용할 활성 `kind=llm` Deployment ID |
| `detections` | array | O | Gateway가 병합한 전체 Regex·NER·LLM Detection. 빈 배열은 허용한다. |

요청은 알 수 없는 필드와 Python snake_case alias를 거부한다. 각 Detection은 공통
Detection 계약을 따르며 `source`에는 `regex`, `ner`, `llm`을 모두 허용한다.

모델을 실행하기 전에 모든 Detection에 대해 다음을 검증한다.

```text
0 <= start < end <= len(text)
detection.text == text[start:end]
type이 비어 있지 않은 유효한 UTF-8 문자열이고 source가 regex·ner·llm 중 하나
score가 0.0~1.0 범위
Detection 개수가 운영 상한 이내
```

합집합 target을 만든 뒤 Backend에 보낼 canonical user JSON의 UTF-8 byte 상한도
별도로 검증한다.

하나라도 잘못되면 전체 요청을 `422 REQUEST_VALIDATION_FAILED`로 거부하며 LLM을
호출하지 않는다. `llmDeploymentId`가 없거나 비활성화됐거나 종류가 맞지 않을 때도
다른 Deployment로 대체하지 않는다.

### 3.1 겹친 Detection

같은 범위에 여러 type이 있거나 일부 범위가 겹친 Detection도 허용한다. Pipeline은
Detection 원본을 삭제하거나 의미상 하나로 합치지 않고, 문자 coverage만 다음과
같이 비어 있지 않은 최대 합집합 component로 계산한다.

```text
[0, 5), [3, 8), [10, 12)
→ [0, 8), [10, 12)
```

끝과 시작이 같은 반개방 구간 `[0, 3)`, `[3, 5)`는 겹치지 않으므로 서로 다른
component로 유지한다. 즉 다음 span의 `start`가 현재 component의 `end`보다 작을
때만 병합한다. 각 component의 `types`와 `sources`는 component에 기여한 Detection
값의 중복 없는 결정적 정렬 목록이다.

### 3.2 빈 Detection 단축 경로

`detections`가 빈 배열이어도 Pipeline은 Registry Snapshot을 캡처하고
`llmDeploymentId`의 존재, `enabled`와 `kind=llm`을 동일하게 검증한다. 정상 실행
계획을 조립한 뒤에는 namespace를 만들거나 개인정보를 새로 찾기 위해 Backend를
호출하지 않고 다음 응답을 반환한다.

```json
{
  "maskedText": "요청의 원문과 완전히 같은 문자열",
  "replacements": []
}
```

이 동작은 Gateway가 전달한 탐지 집합에 대한 항등 변환일 뿐 원문에 민감정보가
없음을 증명하지 않는다.

## 4. Local LLM 입력과 출력

마스킹은 코드에 고정된 읽기 전용 `config/mask_prompt.j2`를 사용한다. 이 Prompt는
외부 Jinja 변수가 없는 정적 System Prompt다. 사용자 원문을 Jinja2 Context로
렌더링하지 않는다.

Pipeline은 검증한 원문, 합집합 component, 각 component의 type·source 근거와
서버가 생성한 요청 전용 placeholder namespace를 canonical JSON으로 직렬화하여
별도 `user` 메시지로 전달한다. Runtime 요청은 Prompt 경로, 본문, Prompt ID,
namespace나 임의 모델 파라미터를 받지 않는다.

LLM 출력은 설명이나 Markdown 코드 블록이 없는 다음 형태의 순수 JSON이어야 한다.

```json
{
  "maskedText": "[[LPL_a1b2c3d4e5f60708_0001]]은 ...",
  "assignments": [
    {"targetId": "target-1", "entityId": "entity-1"}
  ]
}
```

모델은 원문 span, type, source나 replacement의 placeholder 필드를 새로 작성하지
않고 서버가 제공한 `targetId`와 문맥상 같은 대상을 나타내는 `entityId`의 관계만
출력한다. `entityId`는 첫 등장 순서대로 `entity-1`, `entity-2`, ... 형식이어야
한다. `maskedText` 안에서는 서버가 제공한 namespace와 entity 순번에 맞는
placeholder만 사용할 수 있다. LPL이 검증된 입력 target에서 `start`, `end`,
`types`, `sources`를 복원하고 요청 namespace와 entity 첫 등장 순번으로
placeholder를 만든다. `assignments`는 target과 수와 순서가 정확히 같아야 하며 각
`targetId`를 정확히 한 번 포함해야 한다.

모델 출력은 항상 비신뢰 입력으로 취급하며 `LlmMaskingOutputParser`와
`LlmMaskingOutputValidator`를 모두 통과해야 한다. Backend가 구조화 출력 기능을
제공하더라도 서버 측 검증을 생략하지 않는다. Pipeline은 현재 target ID와 정확한 assignment 수를 제한한
요청별 JSON Schema를 `output_schema`로 전달하고 `max_tokens=16384`를 사용한다.
canonical user JSON은 기본 `12,288 bytes`, LLM 원출력은 `32,768 bytes`로
제한하고 Detection과 assignment는 각각 최대 128개를 허용한다. 또한 모든 target이
서로 다른 entity라고 가정한 최악의 정상 출력 JSON을 호출 전에 계산해
`16,384 bytes`를 넘으면 `413 MASK_REQUEST_TOO_LARGE`로 거부한다. 이 제한은
`max_tokens=16384` 안에서 완료될 가능성이 없는 요청을 모델에 보내지 않기 위한
MVP 운영 상한이며, 모델별 context window를 Registry에서 관리하게 되면 Deployment별
계산으로 교체할 수 있다.

## 5. 성공 응답

```json
{
  "maskedText": "[[LPL_a1b2c3d4e5f60708_0001]]은 보고서를 작성했고 [[LPL_a1b2c3d4e5f60708_0001]]은 이를 검토했습니다.",
  "replacements": [
    {
      "start": 0,
      "end": 3,
      "entityId": "entity-1",
      "placeholder": "[[LPL_a1b2c3d4e5f60708_0001]]",
      "types": ["PERSONAL_IDENTITY"],
      "sources": ["ner"]
    },
    {
      "start": 15,
      "end": 19,
      "entityId": "entity-1",
      "placeholder": "[[LPL_a1b2c3d4e5f60708_0001]]",
      "types": ["PERSONAL_IDENTITY"],
      "sources": ["llm"]
    }
  ]
}
```

| 필드 | 설명 |
|---|---|
| `maskedText` | 검증된 replacements를 원문에 적용한 최종 문자열 |
| `replacements` | 원문에서 실제로 치환한 비중첩 span 목록 |
| `start`, `end` | `maskedText`가 아닌 원문의 Python Unicode code point 기준 반개방 인덱스 `[start, end)` |
| `entityId` | `^entity-[1-9][0-9]{0,4}$` 형식의 요청 범위 동일 대상 식별자 |
| `placeholder` | `^\[\[LPL_[0-9a-f]{16}_[0-9]{4,5}\]\]$` 형식의 치환 문자열 |
| `types` | 해당 replacement span에 기여한 Detection type을 중복 제거한 사전순 목록 |
| `sources` | 해당 replacement span에 기여한 source를 중복 제거해 `regex`, `ner`, `llm` 순으로 정렬한 목록 |

응답은 모델 이름, Token Usage, 원문, Prompt나 Deployment 설정을 포함하지 않는다.

### 5.1 동일 대상 규칙

Local LLM은 문맥을 근거로 별칭, 직함과 반복 mention이 같은 실제 대상을 가리키는지
판단한다. 같은 대상으로 판단한 모든 replacement는 같은 `entityId`와 정확히 같은
`placeholder`를 사용한다. 서로 다른 `entityId`가 같은 placeholder를 공유하거나
한 `entityId`가 여러 placeholder를 사용하는 출력은 거부한다. `entity-1`은
namespace가 `a1b2c3d4e5f60708`이면
`[[LPL_a1b2c3d4e5f60708_0001]]`을 사용한다.

의미상 동일성은 모델 판단이므로 Validator가 실제 세계의 동일 대상을 증명할 수는
없다. Prompt는 애매하면 서로 다른 entity로 두도록 지시한다. 별도 entity로 나뉘어도
모든 탐지 span은 마스킹되므로 민감 문자열 coverage는 유지된다.

`entityId`와 placeholder 매핑은 요청 범위에서만 유효하다. LPL은 이를 저장하지 않고
다음 요청에서 재사용하지 않는다. 여러 메시지에서 같은 placeholder를 유지하려면
대화 상태를 소유한 Gateway가 별도 상태 계약을 관리해야 하며 현재 `/mask` 요청은
이전 매핑을 받지 않는다.

## 6. Fail-closed 검증

Pipeline은 LLM 출력 전체에 대해 최소한 다음 불변식을 검증한다.

1. 출력이 엄격한 JSON 계약과 UTF-8 byte·항목 수 상한을 만족한다.
2. Replacement가 원문의 유효한 비중첩 span이고 결정적인 문서 순서로 정렬된다.
3. Replacement span의 합집합이 입력 Detection 합집합 component와 정확히 같다.
4. 탐지되지 않은 모든 원문 문자는 순서와 값이 그대로 보존된다.
5. 각 `types`와 `sources`가 해당 span에 기여한 Detection 근거와 정확히 같다.
6. `entityId`와 placeholder가 허용된 문법, 요청 namespace와 일대일 대응을 만족한다.
7. 서버가 생성한 namespace와 placeholder가 원문의 literal 문자열과 충돌하지 않는다.
8. replacements로 재구성한 문자열과 모델이 반환한 `maskedText`가 정확히 같다.

하나라도 실패하면 잘못된 항목만 버리거나 나머지를 부분 반환하지 않는다. 원문,
부분 마스킹 문장 또는 모델 출력으로 fallback하지 않고 요청 전체를 오류로 반환한다.
Gateway는 성공한 `maskedText`만 외부 Provider로 전달해야 한다.

## 7. 오류 응답

오류 응답은 원문, Detection 문자열, `maskedText`, entity 매핑, Prompt, LLM 원출력과
내부 URL을 포함하지 않는 공통 형식을 사용한다.

```json
{
  "detail": {
    "code": "LLM_MASKING_TEXT_MISMATCH",
    "message": "LLM 마스킹 결과가 보안 검증을 통과하지 못했습니다"
  }
}
```

| HTTP 상태 | 코드 | 조건 |
|---:|---|---|
| 422 | `REQUEST_VALIDATION_FAILED` | 요청 필드·자료형 또는 Detection의 span·원문 일치가 잘못됨 |
| 422 | `MASK_DETECTIONS_INVALID` | Pipeline의 공통 Span 검증을 통과하지 못함 |
| 413 | `MASK_REQUEST_TOO_LARGE` | 원문·canonical user JSON 또는 예상 최악 출력이 byte 상한을 초과함 |
| 404 | `DEPLOYMENT_NOT_FOUND` | 요청한 LLM Deployment가 없음 |
| 409 | `DEPLOYMENT_DISABLED` | 요청한 LLM Deployment가 비활성화됨 |
| 422 | `DEPLOYMENT_KIND_MISMATCH` | LLM 역할에 NER Deployment를 요청함 |
| 502 | `LLM_MASKING_OUTPUT_INVALID_JSON` | LLM 출력이 엄격한 JSON이 아님 |
| 502 | `LLM_MASKING_OUTPUT_INVALID_TOP_LEVEL` | `maskedText`와 `assignments` 최상위 계약이 다름 |
| 502 | `LLM_MASKING_OUTPUT_INVALID_ASSIGNMENT` | `targetId`, `entityId` 할당 항목이 잘못됨 |
| 502 | `LLM_MASKING_OUTPUT_TOO_LARGE` | LLM 출력 UTF-8 byte 상한을 초과함 |
| 502 | `LLM_MASKING_OUTPUT_TOO_MANY_ASSIGNMENTS` | assignment 수 상한을 초과함 |
| 502 | `LLM_MASKING_TARGET_MISMATCH` | target 수·순서·coverage가 입력과 다름 |
| 502 | `LLM_MASKING_ENTITY_INVALID` | entity ID 형식이나 첫 등장 연속 순서가 잘못됨 |
| 502 | `LLM_MASKING_TEXT_MISMATCH` | 서버가 재구성한 maskedText와 LLM 출력이 다름 |
| 502 | `LLM_MASKING_PLACEHOLDER_COLLISION` | placeholder가 원문 literal과 충돌함 |
| 500 | `MASK_PROMPT_RENDER_FAILED` | 고정 마스킹 Prompt를 렌더링할 수 없음 |
| 500 | `MASKING_BACKEND_RESULT_INVALID` | Backend 결과가 공통 `LlmResult` 계약과 다름 |
| 500 | `MASKING_INTERNAL_VALIDATION_FAILED` | 입력 직렬화나 안전한 namespace 생성에 실패함 |
| 503 | `REGISTRY_NOT_INITIALIZED` | Registry가 준비되지 않음 |
| 503 | `APPLICATION_RUNTIME_UNAVAILABLE` | FastAPI Runtime을 조회할 수 없음 |
| 503 | `BACKEND_PROVIDER_NOT_REGISTERED` 등 | 실행할 Provider가 준비되지 않음 |
| 504/502/500 | Adapter별 Backend 코드 | Local LLM timeout·전송·응답·설정 오류 |

Adapter 고유 `detail.code`는 공통 Backend 오류 처리에서 그대로 유지한다. Route는
Adapter 전용 예외를 직접 해석하지 않는다.

## 8. Stateless와 운영 보안

LPL은 원문, Detection, maskedText, replacement, entity 매핑이나 모델 출력을 영구
저장하지 않는다. 요청이 끝나면 요청 전용 namespace와 매핑도 폐기한다.

`/mask`는 민감한 원문을 직접 받으므로 Gateway 뒤의 신뢰 가능한 로컬 네트워크에서만
제공한다.

- Access log와 Application log에 요청 본문, Prompt 입력, 모델 출력과 응답 본문을
  기록하지 않는다.
- Reverse Proxy 또는 ASGI 계층과 Masking 입력 Validator에서 독립적인 요청 byte
  상한을 적용한다.
- 사용자 원문을 Prompt 명령으로 신뢰하지 않고 데이터로 전달한다.
- LLM 실패나 검증 실패 시 원문을 외부 Provider에 보내는 우회 경로를 두지 않는다.
- TLS, 인증·인가와 사용자별 Deployment 정책은 Gateway 및 배포 경계에서 적용한다.
