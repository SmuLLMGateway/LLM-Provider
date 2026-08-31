# Detection Contract

## 1. 목적

Detection Contract는 LPL의 탐지 Pipeline과 FastAPI Endpoint가 공통으로 사용하는 요청·응답 형식을 정의한다.

현재 구현의 기준 모델은 `app/schemas/detection.py`와
`app/schemas/detection_context.py`에 있는 다음 Pydantic 모델이다.

```text
DetectRequest
OrganizationProfile
SourceType
RegexCandidate
NerCandidate
Detection
DetectResponse
```

별도의 수동 JSON Schema 파일은 유지하지 않는다. FastAPI는 Pydantic 모델을 기준으로
`/docs`와 `/openapi.json`의 Schema를 자동 생성한다.

## 2. 전체 구조

```text
Gateway
   │
   │ DetectRequest
   ▼
LPL Detection Pipeline
   │
   │ DetectResponse
   ▼
Gateway
```

Gateway는 Regex로 먼저 찾은 결과를 `regexCandidates`에 넣어 LPL에 전달한다.
LPL은 먼저 NER 개체 탐지를 실행한다. 이후 Regex와 NER 후보를 근거로 후속 LLM이
각 후보를 판정하고 누락·문맥형 민감정보를 추가 탐지한다. 응답에는 전체 후보 판정과
확정된 NER·신규 LLM 결과를 분리해 반환한다.
Option B에서는 Gateway가 기존 Regex 결과와 이 응답을 전체 병합한 뒤 원문과 함께
`POST /mask`에 전달한다. 마스킹 계약은 [Masking API](./masking.md)를 따른다.

## 3. DetectRequest

요청 예시:

```json
{
  "nerDeploymentId": "ner-local-a",
  "llmDeploymentId": "llm-local-a",
  "text": "홍길동은 프로젝트 알파를 담당합니다.",
  "organizationProfile": {
    "organization": {
      "name": "ABC 주식회사",
      "aliases": ["ABC"],
      "type": "PRIVATE"
    },
    "publicContext": {
      "domains": ["abc.com"],
      "entities": []
    },
    "privacy": {
      "personNameScope": true,
      "persons": [
        {"name": "홍길동", "aliases": ["길동"]}
      ]
    },
    "securityContext": {
      "internalIpRanges": [],
      "internalDomains": [],
      "internalSystems": [],
      "cloudAssets": [],
      "securityAssets": [],
      "protectTestLogs": true
    },
    "confidentialTechnologyContext": {"assets": []},
    "thirdPartyContext": {"entities": []}
  },
  "sourceType": "CHAT_TEXT",
  "regexCandidates": [
    {
      "candidateId": "R001",
      "start": 0,
      "end": 3,
      "text": "홍길동",
      "policyId": "P01",
      "detailType": "PERSON_NAME",
      "score": 1.0
    }
  ]
}
```

`regexCandidates`도 생략할 수 있으며, 생략하면 빈 배열과 동일하게 처리한다.

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `nerDeploymentId` | string | O | `^[a-z0-9][a-z0-9._-]*$` | 먼저 실행할 `kind=ner` Deployment ID |
| `llmDeploymentId` | string | O | `^[a-z0-9][a-z0-9._-]*$` | 후속 추가 탐지에 사용할 `kind=llm` Deployment ID |
| `text` | string | O | 길이 1 이상, 유효한 UTF-8 | 탐지할 사용자 원문 |
| `organizationProfile` | object 또는 null | X | 아래 조직 Context 계약 | 제공 가능한 경우 사용하는 Gateway 관리 조직 기준정보 |
| `sourceType` | string | O | `CHAT_TEXT` 또는 `OCR_TEXT` | Gateway가 알고 있는 텍스트 생성 출처 |
| `regexCandidates` | array | X | 최대 10,000개, candidateId 고유, 요청 원문의 범위·내용과 일치 | Gateway의 미확정 Regex 후보. 기본값은 빈 배열 |

### 3.1 RegexCandidate

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `candidateId` | string | O | `^R[0-9]{3,}$`, 요청 내 고유 | Gateway가 생성한 후보 식별자 |
| `start` | integer | O | 0 이상 | 후보 시작 문자 위치, 포함 |
| `end` | integer | O | start보다 큼 | 후보 끝 문자 위치, 미포함 |
| `text` | string | O | 원문 span과 정확히 일치 | Regex가 찾은 실제 문자열 |
| `policyId` | string | O | 등록된 14개 Policy ID | Regex가 제안한 정책 |
| `detailType` | string | O | 대문자·숫자·밑줄, 최대 64자 | Regex 규칙의 세부 분류 |
| `score` | number | O | 0.0~1.0 | 후보 신뢰도 |

RegexCandidate에는 `type`과 `source`를 받지 않는다. LPL이 `policyId`에서 공통
Detection Type을 파생하고 내부 변환 시 `source="regex"`를 설정한다.

Python 모델에서는 camelCase ID를 snake_case로 사용한다.

```python
request.ner_deployment_id
request.llm_deployment_id
request.text
request.regex_candidates
```

Python 내부에서는 `regexCandidates`를 변경 불가능한
`tuple[RegexCandidate, ...]`로 보관한다. JSON 배열로 전달한 원본 리스트를 이후에
변경해도 요청 모델에는 전파되지 않으며, 검증이 끝난 요청 모델의 컬렉션에도
`append()`나 `clear()`를 호출할 수 없다. API 직렬화 시에는 다시 JSON 배열로
표현된다.

각 Regex 후보는 요청 원문과 함께 다음 조건을 검증한다.

```text
candidate.candidate_id는 R + 숫자 3자리 이상
candidate.detail_type은 대문자·숫자·밑줄 형식
candidate.end <= len(request.text)
candidate.text == request.text[candidate.start:candidate.end]
한 요청 안에서 candidateId는 고유
```

개수가 10,000개를 넘거나 출처·범위·문자열이 계약과 다르면 모델을 실행하지 않고
요청 전체를 `422 REQUEST_VALIDATION_FAILED`로 거부한다.

Gateway는 사용자 또는 요청 정책에 따라 실행할 NER와 LLM Deployment ID를 선택해
모든 탐지 요청에 전달한다. 두 ID 중 하나라도 누락되거나 `null`이면 API 요청 검증
오류다. LPL은 전달된 ID를 다른 Deployment로 대체하지 않는다. NER 서버의 URL은
요청 필드가 아니며, Registry에 미리 등록된 `nerDeploymentId`가 Endpoint를
결정한다.

활성 정책은 요청마다 받지 않고 별도 `GET/PUT /policies/enabled` API와
`config/policy_settings.json`에서 관리한다. Detection Pipeline은 요청 시작 시 현재
불변 설정을 한 번 캡처한다. Regex 후보의 `policyId`가 비활성이면
`422 DETECTION_POLICY_DISABLED`로 거부하고, NER 결과와 LLM Prompt·출력 Schema도
활성 정책만 사용한다.

Pipeline은 요청 시작 시 Registry Snapshot을 한 번 캡처한다. `DeploymentResolver`는
같은 Snapshot에서 요청한 두 Deployment와 Prompt Artifact를
`DetectionExecutionPlan`으로 조립하고 Pipeline은 그 Plan만 소비한다. 따라서 요청
중 Deployment 설정 Reload가 성공해도 진행 중인 요청의 실행 구성은 바뀌지 않는다.

빈 문자열은 허용하지 않는다. 공백만 포함된 문자열을 거부하거나 입력 길이를 제한하는 정책은 아직 적용하지 않는다.

### 3.2 OrganizationProfile과 SourceType

`organizationProfile`은 선택 필드다. Gateway가 조직별 관리자 설정을 보유한 경우
전달하고, 제공하지 않는 기업은 필드를 생략하거나 `null`을 보낼 수 있다. LPL은 이를
파일이나 DB에 저장하지 않고 현재 요청의 후보 판정과 추가 탐지에만 사용한다.
프로필 객체를 제공한다면 다음 두 섹션은 필수다.

| 섹션 | 주요 필드 | 설명 |
|---|---|---|
| `organization` | `name`, `aliases`, `type` | 공식 조직명·별칭과 조직 유형 |
| `publicContext` | `domains`, `entities` | 이름 자체로는 기밀이 아닌 공식 도메인·공개 명칭 |

조직 Context의 Enum은 다음 값만 허용한다.

| 위치 | 허용값 |
|---|---|
| `organization.type` | `PRIVATE`, `PUBLIC`, `FINANCE`, `MEDICAL`, `EDUCATION`, `DEFENSE`, `OTHER` |
| `publicContext.entities[].type` | `PRODUCT`, `SERVICE`, `PROJECT`, `CUSTOMER`, `PARTNER`, `SYSTEM`, `TECH`, `OTHER` |
| `securityContext.internalSystems[].environment` | `PRODUCT`, `STAGING`, `DEV`, `TEST`, `OTHER` |
| `confidentialTechnologyContext.assets[].type` | `PROJECT`, `PRODUCT`, `TECH`, `REPO`, `RESEARCH`, `DATASET`, `OTHER` |
| `thirdPartyContext.entities[].relationship` | `CUSTOMER`, `PARTNER`, `SUPPLIER`, `CONTRACTOR`, `OTHER` |

`securityContext.internalSystems[].environment`의 운영 환경 값은 Gateway 계약에 따라
`PRODUCT`를 사용한다.

`privacy.persons`는 조직이 보호 대상으로 등록한 관계자의 `name`과 `aliases` 목록이다.
생략하면 빈 배열로 처리해 기존 프로필과 호환한다. `personNameScope=false`이면 이름
자체를 P01로 확정하지 않는다. `personNameScope=true`이고 `persons`가 비어 있지
않으면 등록 명단과 정확히 일치하는 인명만 P01로 확정하고 다른 인명은 자동 탐지하지
않는다. 명단이 비어 있을 때만 비공개 개인 문맥을 사용한 기존 판정 방식을 적용한다.
서로 다른 관계자의 이름과 별칭은 중복될 수 없다.

조직 프로필이 제공되면 등록값을 일반 추론보다 우선한다. 공개 Context의 값은 공개
범위에서 제외하고, 등록된 내부 인프라·시스템, 비공개 기술자산과 비공개 고객·협력사
정보는 각 S02·B03·B02 정책의 우선 탐지 근거로 사용한다. 프로필에 등록되지 않은
IP·도메인·시스템명은 형식만으로 조직 내부정보라고 확정하지 않는다.

활성 정책에 따라 다음 섹션이 조건부로 필요하다.

| 활성 정책 | 필수 조직 섹션 |
|---|---|
| `P01` | `privacy.personNameScope` |
| `S02` | `securityContext`의 내부 IP·도메인·시스템·클라우드·보안자산 목록 |
| `S03` | `securityContext.protectTestLogs`의 명시적인 boolean |
| `B02` | `thirdPartyContext.entities` |
| `B03` | `confidentialTechnologyContext.assets` |

목록에서 “해당 없음”은 필드를 생략하는 대신 빈 배열 `[]`로 표현한다. 프로필 전체가
없으면 조건부 섹션도 강제하지 않는다. 다만 프로필 객체를 제공하면서 활성 정책에
필요한 조건부 섹션이 없으면 `422 ORGANIZATION_PROFILE_INCOMPLETE`를 반환한다. 조직 프로필은
중첩 추가 필드, 중복 별칭·도메인, 잘못된 IP/CIDR·도메인과 비 UTF-8 문자열을
거부한다.

`sourceType`은 LPL에 전달되는 데이터 형식이 아니라 Gateway가 만든 텍스트의 출처다.
LPL은 항상 `text` 문자열만 받는다.

```json
"CHAT_TEXT"
```

허용값은 다음 두 개다.

| 값 | 설명 |
|---|---|
| `CHAT_TEXT` | 사용자가 직접 입력한 일반 채팅 텍스트 |
| `OCR_TEXT` | Gateway가 파일·이미지를 OCR 처리해 추출한 텍스트 |

`OCR_TEXT`이면 LLM은 OCR 오류와 비표준 문자·구분자 가능성을 고려하지만 입력에 없는
값을 추론해 만들 수 없다. 파일명, 문서 제목, 페이지, 섹션과 언어는 LPL 계약에
포함하지 않는다.

조직 프로필과 `sourceType`을 합친 canonical JSON은 기본 `524,288 bytes` 이하로
제한한다. 제한을 넘으면 NER·LLM을 호출하기 전에 `413 DETECTION_REQUEST_TOO_LARGE`로
거부한다. Prompt 전체에는 별도의 Context·렌더링 출력 상한도 다시 적용한다.

### 3.3 NerCandidate

`NerCandidate`는 외부 `/detect` 요청 필드가 아니다. LPL이 표준 NER 응답을 검증하고
Regex와의 의미 중복을 제거한 뒤, 후속 LLM의 판단 근거로 내부 생성한다.

```json
{
  "candidateId": "N001",
  "start": 0,
  "end": 3,
  "text": "홍길동",
  "policyId": "P01",
  "entityType": "PERSON",
  "score": 0.98
}
```

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `candidateId` | string | O | `^N[0-9]{3,}$` | LPL이 요청 범위에서 생성한 후보 ID |
| `start` | integer | O | 0 이상 | 후보 시작 문자 위치, 포함 |
| `end` | integer | O | start보다 큼 | 후보 끝 문자 위치, 미포함 |
| `text` | string | O | 원문 span과 정확히 일치 | NER가 찾은 실제 문자열 |
| `policyId` | string | O | `P01` 또는 `P04` | NER가 제안한 정책 |
| `entityType` | string | O | `PERSON` 또는 `LOCATION` | NER 개체 형식 |
| `score` | number | O | 0.0~1.0 | NER 신뢰도 |

정책과 개체 형식은 `P01/PERSON`, `P04/LOCATION`으로 고정한다. 후보 ID는 NER
Backend의 반환 순서가 아니라 검증·중복 제거 후 원문 위치 순서대로 `N001`부터
부여한다. 따라서 동일 입력과 동일 탐지 결과에서는 같은 순서를 얻는다.

## 4. Detection

Detection은 원문에서 탐지된 구간 하나를 나타낸다.

```json
{
  "start": 0,
  "end": 3,
  "text": "홍길동",
  "type": "PERSONAL_IDENTITY",
  "policyId": "P01",
  "source": "ner",
  "score": 0.98
}
```

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `start` | integer | O | 0 이상 | 탐지 구간의 시작 위치, 포함 |
| `end` | integer | O | `start`보다 큼 | 탐지 구간의 끝 위치, 미포함 |
| `text` | string | O | 길이 1 이상 | 원문에서 탐지된 실제 문자열 |
| `type` | string | O | 아래 14개 정책 코드 중 하나 | 탐지 유형 |
| `policyId` | string | O | `type`과 고정 매핑 | 적용된 정책 ID |
| `source` | string | O | `regex`, `ner` 또는 `llm` | 결과를 생성한 탐지 방식 |
| `score` | number | O | 0.0 이상 1.0 이하 | Backend가 반환한 신뢰 점수 |

### 4.1 start와 end 규칙

탐지 범위는 0부터 시작하는 반개방 구간 `[start, end)`을 사용한다.

```python
detection.text == request.text[detection.start:detection.end]
```

예를 들어 `홍길동`이 원문의 첫 세 글자라면 다음과 같다.

```text
start = 0
end = 3
text = "홍길동"
```

Adapter는 모델 서버가 byte offset이나 token offset을 반환하더라도 LPL 공통 형식으로 변환하기 전에 Python 문자열 문자 위치로 정규화해야 한다.

`Detection` 모델 자체는 원문을 알지 못하므로 다음 조건까지만 검증한다.

```text
start >= 0
end >= 1
end > start
```

`DetectRequest.regex_candidates`는 요청 원문을 함께 갖고 있으므로 다음 조건도
요청 모델에서 즉시 검증한다. Backend가 새로 반환한 결과에는 Detection Pipeline의
Span Validator가 같은 검증을 적용해야 한다.

```text
end <= len(original_text)
text == original_text[start:end]
```

### 4.2 type 규칙

`type`은 다음 14개 정책 코드 중 하나여야 한다. 대소문자와 공백까지 정확히
일치해야 하며, 목록에 없는 값이나 유사한 이름은 허용하지 않는다.

| 카테고리 | Policy ID | 코드 | 의미 |
|---|---|---|---|
| `PRIVATE` | `P01` | `PERSONAL_IDENTITY` | 개인 신원 정보 |
| `PRIVATE` | `P02` | `UNIQUE_IDENTITY` | 고유식별번호 |
| `PRIVATE` | `P03` | `CONTACT` | 연락처 |
| `PRIVATE` | `P04` | `LOCATION` | 주소·정밀 위치 |
| `PRIVATE` | `P05` | `PAYMENT` | 결제 정보 |
| `PRIVATE` | `P06` | `FINANCE_ACCOUNT` | 금융계좌정보 |
| `PRIVATE` | `P07` | `PERSONAL_FINANCE` | 개인 금융·경제정보 |
| `PRIVATE` | `P08` | `SENSITIVE_PERSONAL` | 민감 개인정보 |
| `SECURITY` | `S01` | `AUTH` | 인증정보·Secret |
| `SECURITY` | `S02` | `SECURITY_INFRA` | 보안·인프라 정보 |
| `SECURITY` | `S03` | `SYSTEM_LOG` | 시스템 접근·운영 로그 |
| `INTERNAL_INFO` | `B01` | `PERSONAL` | 인사·인력 운영정보 |
| `INTERNAL_INFO` | `B02` | `CLIENT` | 고객·협력사 기밀정보 |
| `INTERNAL_INFO` | `B03` | `R&D` | R&D·영업비밀·기술정보 |

`policyId`는 요청자가 임의 조합할 수 없다. 예를 들어 `CONTACT`는 반드시 `P03`,
`PERSONAL`은 반드시 `B01`, `R&D`는 반드시 `B03`이어야 한다. GLiNER는
`PERSONAL_IDENTITY`, `LOCATION`을 먼저
탐지하고, Regex는 주로 `UNIQUE_IDENTITY`, `CONTACT`, `PAYMENT` 후보를 제공한다.
나머지는 LLM이 문맥을 중심으로 판단한다.

Gateway가 `/detect`나 `/mask`에 목록 밖 타입을 보내면 요청 검증 단계에서 `422
REQUEST_VALIDATION_FAILED`로 거부한다. NER 또는 LLM이 목록 밖 타입을 반환하면
비신뢰 Backend 출력으로 보고 해당 실행 전체를 fail-closed로 거부한다.

### 4.3 source 규칙

| 값 | 의미 |
|---|---|
| `regex` | Gateway 등 LPL 호출 전 단계의 Regex 탐지가 생성한 기존 결과 |
| `ner` | 전용 NER Backend가 생성한 결과 |
| `llm` | NER 이후 후속 LLM이 추가 탐지한 누락·문맥형 민감정보 |

`regex`는 RegexCandidate를 내부 Detection으로 변환할 때 LPL이 설정한다. LPL이 반환하는 새
탐지 결과의 `source`는 실행한 Backend에 따라 `ner` 또는 `llm`이다.

### 4.4 score 규칙

`score`는 모든 Detection에서 필수이며 `0.0~1.0` 범위를 사용한다.

모델마다 점수의 산출 방식과 보정 수준이 다를 수 있으므로 서로 다른 Backend의 점수를 절대적으로 동일한 신뢰도로 간주하지 않는다. 점수를 제공하지 않는 Backend를 지원할 경우 Adapter의 기본값 정책 또는 nullable 전환을 별도로 결정해야 한다.

### 4.5 LLM 출력 계약과 Parser

후속 탐지 LLM은 설명이나 Markdown 코드 블록이 아닌 순수 JSON 객체를 반환한다.
모델은 기존 후보에 대해서는 ID와 판정만, 새 탐지에 대해서는 좌표 없는 최소 필드만
출력한다.

```json
{
  "candidateDecisions": [
    {
      "candidateId": "R001",
      "decision": "CONFIRMED"
    },
    {
      "candidateId": "N001",
      "decision": "UNCERTAIN"
    }
  ],
  "newDetections": [
    {
      "text": "홍길동의 인사평가는 C등급",
      "type": "PERSONAL",
      "score": 0.87
    }
  ]
}
```

`candidateDecisions` 항목은 `candidateId`, `decision`만 포함한다. 판정값은
`CONFIRMED`, `REJECTED`, `UNCERTAIN` 중 하나다. 입력 Context의 모든 Regex·NER
candidateId가 정확히 한 번씩 존재해야 하며 누락, 중복 또는 알 수 없는 ID가 있으면
전체 모델 출력을 거부한다. 서버는 모델이 후보 원문·좌표·정책을 다시 출력하게 하지
않고 검증된 후보 객체에서 응답 필드를 복원한다.

`newDetections` 항목은 다음 세 필드만 포함한다.

```text
text, type, score
```

LLM은 `start`, `end` 또는 `source`를 출력하지 않는다. 좌표 계산은 모델의 역할이
아니다. `LlmDetectionOutputParser`는 각 항목을 변경할 수 없는
`LlmDetectionCandidate`로 검증한다. `LlmDetectionSpanResolver`는 식별자·연락처처럼
반복 가능한 타입이면
`text`와 정확히 일치하는 모든 원문 위치를 찾고, 문맥형 타입이면 정확히 한 위치에만
일치하는지 확인한다. Resolver가 생성한 `Detection`에만 서버가 계산한 `start`,
`end`와 `source="llm"`을 넣는다.

Parser는 LLM 출력을 비신뢰 입력으로 보고 다음 값을 거부한다.

- 잘못된 JSON, 중복된 객체 키, `NaN`, `Infinity`
- JSON 앞뒤의 설명이나 Markdown 코드 블록
- 최상위 객체의 필수 배열 누락 또는 알 수 없는 필드
- 잘못된 후보 판정 상태와 후보 ID 누락·중복·위조
- 필수 필드 누락, 알 수 없는 필드, 잘못된 자료형
- `text`, `type`, `score`의 후보 제약 위반
- 설정한 바이트 크기, 후보 판정 수 또는 신규 탐지 수 제한을 넘는 출력

기본 제한은 UTF-8 기준 `1,048,576`바이트, 후보 판정 `20,000`개와 신규 탐지
`1,000`개다. Parser 생성 시 `max_output_bytes`, `max_candidate_decisions`,
`max_detections`에 1 이상의 정수를 전달해 더 작은 운영 한도를 적용할 수 있다.
오류에는 기계 판독 가능한 code와 해당되는 경우
`item_index`, 실제·허용 바이트 수 또는 실제·허용 항목 수가 포함된다. 오류
메시지와 속성에는 민감한 LLM 원문을 포함하지 않는다.

첫 출력의 후보 `text`가 원문에 없으면 Resolver는 좌표를 추측하는 대신 실패 항목
index와 정확 복사 규칙만 알려 Local LLM에 전체 JSON을 한 번
재요청한다. 문맥형 후보 문자열이 원문에서 반복되는
`LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS`도 같은 방식으로 한 번 교정 요청한다. 교정
요청에는 원문을 별도로 복제하지 않으며 첫 호출의 Prompt와 모델 응답을 같은 로컬
대화 Context로 사용한다. 두 번째 출력도 계약을 지키지 않으면 좌표를 추측하거나
퍼지 매칭하지 않고 기존 오류 코드로 전체 요청을 거부한다. 반복 가능한 타입은 같은
문자열이 원문에 여러 번 있으면 모든 출현 위치를 같은 `type`과 `score`로 확장한다.
문맥형 타입은 문자열이 정확히 한 번만 나타나야 하며, 반복되면
`LLM_DETECTION_OUTPUT_TEXT_AMBIGUOUS`로 거부한다. 확장된 Detection 수가 상한을
넘는 경우도 전체 출력을 거부한다. 이후 Span Validator가 서버에서 만든 좌표와 원문
일치를 다시 검증한다.

## 5. DetectResponse

응답은 모든 입력 후보의 판정과 LPL이 반환할 확정 탐지를 분리한다. Python 내부의
두 배열은 생성 후 변경되지 않는 tuple로 보관한다.

```json
{
  "candidateDecisions": [
    {
      "candidateId": "R001",
      "source": "regex",
      "start": 16,
      "end": 29,
      "text": "010-1234-5678",
      "policyId": "P03",
      "detailType": "PHONE_NUMBER",
      "score": 1.0,
      "decision": "CONFIRMED"
    },
    {
      "candidateId": "N001",
      "source": "ner",
      "start": 0,
      "end": 3,
      "text": "홍길동",
      "policyId": "P01",
      "entityType": "PERSON",
      "score": 0.98,
      "decision": "CONFIRMED"
    }
  ],
  "detections": [
    {
      "start": 0,
      "end": 3,
      "text": "홍길동",
      "type": "PERSONAL_IDENTITY",
      "policyId": "P01",
      "source": "ner",
      "score": 0.98
    },
    {
      "start": 0,
      "end": 14,
      "text": "홍길동의 인사평가는 C등급",
      "type": "PERSONAL",
      "policyId": "B01",
      "source": "llm",
      "score": 0.87
    }
  ]
}
```

`candidateDecisions`에는 Regex 후보를 요청 순서대로, 그 뒤에 NER 후보를 원문 순서대로
담는다. 모델이 출력한 값 중 `candidateId`와 `decision`만 사용하고 나머지 필드는
서버가 검증된 후보에서 복원한다. `detections`에는 `CONFIRMED` NER 후보와
`newDetections`를 검증·중복 제거한 LLM 결과만 들어간다. Gateway가 이미 소유한
Regex 후보는 중복 반환하지 않으므로 `CONFIRMED` Regex는 `candidateDecisions`를
근거로 Gateway가 공통 Detection으로 변환한다. `REJECTED`와 `UNCERTAIN` 후보는
`detections`에 자동 포함하지 않는다.

후보와 탐지 결과가 모두 없으면 두 빈 목록을 반환한다.

```json
{
  "candidateDecisions": [],
  "detections": []
}
```

Pydantic/FastAPI가 직렬화할 때 내부 tuple은 JSON 배열로 변환된다. 따라서
Gateway가 보는 HTTP 계약과 OpenAPI의 `array` 타입은 기존과 동일하다.
Python 코드에서 리스트를 전달해도 모델 검증 단계에서 tuple로 정규화한다.

`Detection` 모델 자체는 정렬 순서나 중복·겹침 정책을 적용하지 않는다. Backend가
반환한 batch는 `DetectionResultValidator`를 통과시킨 뒤 응답에 사용한다.

```text
Backend 또는 LLM Parser 결과
        │ 비신뢰 batch
        ▼
SpanValidator
 ├─ exact list/tuple 및 exact Detection 확인
 ├─ Detection 필드 전체 재검증
 ├─ 0 <= start < end <= len(original_text)
 ├─ detection.text == original_text[start:end]
 └─ 선택한 Backend와 source 일치 확인
        │ 검증된 tuple
        ▼
OverlapResolver
 ├─ 의미 중복 축약
 ├─ 서로 다른 겹침 보존
 └─ 결정적 문서 순서 정렬
```

```python
from app.policies import DetectionResultValidator

validated = DetectionResultValidator().validate(
    original_text,
    backend_detections,
    expected_source="ner",
)
```

하나의 항목이라도 잘못되면 batch 전체를 거부한다. 잘못된 항목만 버리거나 중복
제거로 먼저 숨기지 않는다. `model_construct()`처럼 Pydantic 검증을 우회해 만들어진
객체도 새 `Detection`으로 다시 검증한다. 기본 batch 상한은 `10,000`개이며 운영
환경에서는 `SpanValidator(max_detections=...)`로 더 낮출 수 있다.

`expected_source`를 지정하면 NER batch에는 `ner`, LLM Parser 결과에는 `llm`, 기존
Regex 결과에는 `regex`만 허용할 수 있다. 검증 오류에는 code와 항목 index, 필요한
개수 제한 정보만 들어가며 원문이나 탐지 문자열은 보관하지 않는다.

| 오류 code | 의미 |
|---|---|
| `INVALID_TEXT` | 원문 타입 또는 UTF-8이 잘못됨 |
| `INVALID_CONTAINER` | batch가 정확한 `list` 또는 `tuple`이 아님 |
| `TOO_MANY_DETECTIONS` | batch 항목 수가 설정 상한을 초과함 |
| `INVALID_ITEM` | 항목이 정확한 `Detection`이 아니거나 필드 재검증에 실패함 |
| `INVALID_SPAN` | `0 <= start < end <= len(original_text)`를 위반함 |
| `TEXT_MISMATCH` | 탐지 문자열이 원문의 해당 구간과 다름 |
| `SOURCE_MISMATCH` | 탐지 출처가 `expected_source`와 다름 |

### 5.1 중복과 겹침 정책

중복 키는 `(start, end, type)`이다. Span 검증 뒤에는 같은 범위의 `text`가 원문으로
결정되므로 별도 중복 키에 넣지 않는다. 같은 키가 여러 번 나오면 다음 순서로 하나를
선택한다.

```text
source 우선순위: regex > ner > llm
같은 source: score가 높은 결과 우선
```

범위나 `type`이 다르면 서로 겹치더라도 모두 보존한다. 하나를 임의로 버리면 다른
결과만 탐지한 민감 구간이 누락될 수 있고, 둘을 합쳐 새 Detection을 만들면
`type`, `source`, `score`의 출처가 왜곡되기 때문이다. Option B에서 Gateway는
CONFIRMED Regex와 `/detect`의 `detections`를 모두 `/mask`에 전달한다. Masking Pipeline은
Detection 원본을 변경하지 않고 겹치는 span만 별도의 비중첩 합집합 component로
계산하고, 각 component의 `types`와 `sources`를 원래 Detection에서 파생한다.

`/mask`는 제공된 Detection을 마스킹하는 Endpoint이지 추가 탐지 Endpoint가 아니다.
따라서 이 단계에서 누락된 민감정보는 `/mask`가 새로 찾아 치환하지 않으며 Gateway는
탐지 결과를 빠짐없이 병합해 전달해야 한다.

최종 결과는 다음 키로 정렬한다.

```text
start 오름차순
→ end 내림차순
→ type 오름차순
→ source 우선순위
→ score 내림차순
```

따라서 Backend의 비동기 완료 순서나 입력 배열 순서가 달라도 같은 결과가 나온다.
끝과 시작이 같은 반개방 구간(`[0, 3)`, `[3, 5)`)은 겹침으로 보지 않고 모두
보존한다.

## 6. 검증 책임

| 검증 대상 | 담당 컴포넌트 |
|---|---|
| 필수 필드, 자료형, 알 수 없는 필드 | Pydantic Detection 모델 |
| LLM JSON 형식, 좌표 없는 후보 필드, 크기·후보 개수 제한 | `LlmDetectionOutputParser` |
| 후보 문자열의 모든 원문 위치 계산, 확장 상한, `source="llm"` 설정 | `LlmDetectionSpanResolver` |
| `start >= 0`, `end > start` | `Detection` |
| 기존 탐지가 요청 원문 범위 안에 있는지 | `DetectRequest` |
| 기존 탐지의 `text`가 요청 원문 구간과 일치하는지 | `DetectRequest` |
| Backend가 반환한 항목의 공통 Detection 계약 재검증 | Span Validator |
| Backend가 반환한 탐지가 원문 범위·내용과 일치하는지 | Span Validator |
| 실행 역할과 `source`가 일치하는지 | Span Validator의 `expected_source` |
| 모델별 Label을 공통 `type`으로 변환 | Backend Adapter |
| 점수를 `0.0~1.0`으로 정규화 | Backend Adapter |
| 의미 중복 축약과 서로 다른 겹침의 결정적 보존·정렬 | Overlap Resolver |
| 요청한 Deployment와 Backend 선택 | Deployment Resolver, Backend Registry |

모델 출력은 항상 비신뢰 입력으로 취급한다. NER 또는 LLM이 반환한 값을 곧바로
`DetectResponse`에 포함하지 않고 `DetectionResultValidator`의 Span 검증과 중복·겹침
정책을 모두 통과한 결과만 반환한다. `DetectionPipeline`은 각 Backend batch 직후 이
공통 경계를 호출한다.

## 7. Detection Pipeline

`app/services/detection_pipeline.py`는 FastAPI와 독립적으로 Deployment 해석, 두
탐지 단계와 결과 검증을 조립한다.

```text
DetectionPipeline.detect(
    text,
    ner_deployment_id,
    llm_deployment_id,
    regex_candidates,
)
→ Active Snapshot 한 번 캡처
→ DeploymentResolver가 같은 Snapshot에서 DetectionExecutionPlan 조립
→ RegexCandidate에서 type과 source=regex를 파생해 내부 Detection으로 변환
→ 요청한 kind=ner Deployment로 NerBackend.detect() 필수 실행
→ NER 결과 Span·source 검증
→ Regex 중복 제거 후 신규 NER 결과에 N001부터 결정적 후보 ID 부여
→ RegexCandidate + NerCandidate를 엄격한 JSON Context로 직렬화
→ plan.detection_prompt → LLM이 전체 후보 판정 + 누락 정보 추가 탐지
→ 모든 candidateId의 정확히 한 번 판정 여부 검증
→ 서버가 신규 후보 text의 원문 좌표 계산 → LLM 결과 검증
→ 전체 의미 중복 축약과 결정적 정렬
→ 전체 candidateDecisions + CONFIRMED NER·신규 LLM detections 응답
```

후속 LLM 호출은 렌더링한 Prompt를 단일 `user` 메시지로 전달한다. 출력 변동을
줄이기 위해 `temperature=0`을 사용하고 분류 작업에 불필요한 장문 Thinking을 막기
위해 `reasoning_effort="none"`을 전달한다. strict JSON Schema에는 입력 후보 수와
candidateId enum이 반영된 `candidateDecisions`, 그리고 `text`, `type`, `score`와
활성 `type`만 허용하는 `newDetections`를 넣는다. Prompt의
`existing_detections` 변수에는 `enabledPolicies`, 원본 `regexCandidates`와 새
`nerCandidates`, 요청의 `organizationProfile`, `sourceType`을 가진 compact JSON
객체를 넣는다. Regex 후보는 candidateId,
좌표, policyId, detailType과 score를 보존한다. NER 후보는 candidateId, 좌표,
policyId, entityType과 score를 포함한다. NER candidateId는 검증과 중복 제거가
끝난 원문 순서대로 `N001`, `N002`처럼 LPL이 요청 범위에서 생성한다.
P01은 `PERSON`, P04는 `LOCATION`만 허용한다. LLM은 모든 후보를
`CONFIRMED`, `REJECTED`, `UNCERTAIN` 중 하나로 판정하고 누락 항목도 추가로 찾는다.
Pipeline은 후보 존재 여부와 관계없이 현재 전역 `enabledPolicies`의 모든 정책을
검사한다. 활성 정책은 `config/policy_prompts.json`에서 고정
순서로 한 번씩 조립하며 Context의 `enabledPolicies`와 output schema의 type enum도
같은 전체 활성 집합을 사용한다. 따라서 Regex·NER가 아무 후보도 찾지 못해도 LLM이
P01~P08, S01~S03, B01~B03 중 활성화된 타입으로 누락 정보를 추가 탐지할 수 있다.
구조화 출력을 지원하는 Backend도
신뢰 경계가 아니므로 응답은 Parser, Span Resolver와 Span Validator를 다시 통과해야
한다. Schema의 `maxItems`도 Parser의 현재 후보 개수 상한과 동일하게 설정한다.
정확한 원문 substring이 아니거나 문맥형 substring이 고유하지 않은 출력만 최대 한
번 교정 재요청한다. 재요청도 같은 `temperature`, `reasoning_effort`, JSON Schema와
Deployment를 사용하며 그 밖의 JSON·정책·후보 계약 오류는 재시도하지 않는다.

요청 중 Deployment 설정이 Reload되어도 Pipeline은 다시 Snapshot을 캡처하지 않는다.
Snapshot은 컴파일된 Prompt Artifact의 저장과 수명을 보장하고, Resolver는
Deployment와 Artifact 객체 자체를 같은 Snapshot에서 선택해 Plan에 담는다.
Pipeline은 Plan을 받은 뒤 Snapshot을 다시 조회하지 않는다.

### 7.1 표준 HTTP NER 실행

모든 NER Deployment는 사용자 선택 없이 동일한 `HttpNerBackend`와 표준 서버
계약을 사용한다.

```text
DetectRequest.nerDeploymentId
→ Registry에서 kind=ner Deployment 해석
→ 등록된 baseUrl 전체 Endpoint로 POST {"text": "..."} 전송
→ 응답 detections 항목: start/end/text/type/score
→ Adapter가 source="ner" 설정
→ 공통 Span·source 검증
→ 후속 LLM 추가 탐지
```

Runtime 요청에서 URL이나 모델 이름을 직접 받지 않는다. NER Deployment는
`baseUrl`과 `timeoutMs`가 필수이며 `adapterType`과 `modelName`을 받지 않는다.
`baseUrl`은 path를 포함할 수 있는 전체 POST Endpoint이며 LPL은 별도 경로를
붙이지 않는다. query, fragment와 URL userinfo는 허용하지 않는다.

외부 서버의 응답에는 `source`가 없어야 한다. 최상위에는 `detections`만, 각
Detection 항목에는 `start`, `end`, `text`, `type`, `score`만 정확히 허용된다.
Backend는 유효한 UTF-8, 중복 키와 비표준 숫자가 없는 엄격한 JSON 및 기본
`1,048,576 bytes`의 디코딩된 응답 크기 상한을 적용한다. 검증을 통과한 항목에
`source="ner"`를 설정한 뒤 Pipeline이 원문 범위와 문자열 일치를 다시 확인한다.
상세 요청·응답 Schema와 오류 계약은 [표준 HTTP NER](./backends.md#9-표준-http-ner-backend)를
참조한다.

### 7.2 레거시: Hugging Face 직접 Adapter

현재 Runtime에서는 지원하지 않는다. Hugging Face 형식의 서버를 사용하려면 서버
앞단 Wrapper가 등록된 Endpoint에서 표준 JSON 요청과 응답으로 변환해야 한다.
이전 `hf_inference_token_classification` Deployment는 새 Registry에서 거부된다.

### 7.3 레거시: GLiNER 전용 Adapter

현재 Runtime에서는 지원하지 않는다. GLiNER의 라벨, 임계값과 출력 변환은
`services/gliner_ner`가 내부에서 처리하고 LPL에는 등록된 전체 Endpoint에서 표준
JSON 계약만 제공한다. 이전 `gliner_http` Deployment는 새
Registry에서 거부된다.

기존 Regex 결과는 문맥 판단, 후보 판정과 신규 결과의 중복 억제에 사용한다.
Gateway가 이미 보유한 결과이므로 `detections`에는 다시 포함하지 않지만, 검증된
원본 정보와 판정은 `candidateDecisions`로 반환한다. Deployment, Provider,
Backend, Prompt, Parser와 Span 검증 오류는 호출 계층으로 전파하며, LLM Backend가 공통
`LlmResult`가 아닌 값이나 검증을 우회한 비문자 `text`를 반환한 경우에는
`DetectionBackendResultError`가 발생한다. Parser·Span·Backend 결과 계약 오류를
전달할 때는 안전한 오류 정보만 복제하여 Pipeline traceback에 렌더링된 Prompt,
모델 출력과 사용자 원문을 남기지 않는다.

## 8. FastAPI Endpoint와 직렬화

모든 모델은 알 수 없는 필드를 거부하고 생성 후 필드 재할당을 허용하지 않는다.
`DetectRequest.regex_candidates`, `DetectResponse.candidate_decisions`와
`DetectResponse.detections`는 모두 tuple이므로
`append()` 같은 생성 후 변경을 허용하지 않는다. 두 필드 모두 JSON과 OpenAPI에서는
배열로 표현된다.

Endpoint는 다음 경로로 제공한다.

```http
POST /detect
Content-Type: application/json
```

정상 응답은 `200 OK`이며 요청의 `text`, 필수 `nerDeploymentId`,
`llmDeploymentId`와 `regexCandidates`를 공유 `DetectionPipeline`에 전달한다.
후보 판정과 탐지 결과 tuple은 각각 JSON 배열로 직렬화된다.

`regexCandidates`는 아직 확정 Detection이 아니므로 그대로 `/mask`에 전달할 수
없다. `candidateDecisions`에서 `CONFIRMED`인 Regex 후보만 Gateway가 공통 Detection으로
조립하고, 이 응답의 `detections`와 병합해 `POST /mask`에 전달한다.

## 8.1 UNCERTAIN 처리

`candidateDecisions[].decision`이 `UNCERTAIN`이면 `/detect`는 이를 자동 마스킹
대상으로 확정하지 않는다. 응답에 상태를 그대로 보존하고 Gateway·Frontend가 사용자
검토를 진행한다.

```text
POST /detect
→ CONFIRMED / REJECTED / UNCERTAIN 반환
→ 웹에서 UNCERTAIN 검토
→ Gateway가 확정 Detection 조립
→ POST /mask
```

LPL은 `UNCERTAIN`을 `REJECTED`로 바꾸거나 자동으로 `finalDetections`에 포함하지
않는다. 검토가 끝나기 전에는 Gateway가 해당 원문을 외부 Provider로 보내면 안 된다.
현재 `/detect`와 `/mask`는 계속 분리하며 전역 `uncertainAction` 설정은 추가하지
않는다.

## 9. 오류 응답

API 오류는 사용자 원문, 탐지 문자열, 렌더링된 Prompt, 모델 출력이나 내부 URL을
반복하지 않고 다음 고정 형식을 사용한다.

```json
{
  "detail": {
    "code": "DETECTION_RESULT_INVALID",
    "message": "Backend 탐지 결과가 응답 계약과 다릅니다"
  }
}
```

| HTTP 상태 | 코드 | 조건 |
|---:|---|---|
| 422 | `REQUEST_VALIDATION_FAILED` | Deployment ID·sourceType 누락 또는 organizationProfile·RegexCandidate 필드 계약이 잘못됨 |
| 422 | `DETECTION_POLICY_DISABLED` | RegexCandidate의 policyId가 현재 전역 설정에서 비활성임 |
| 422 | `ORGANIZATION_PROFILE_INCOMPLETE` | 활성 정책에 필요한 조직 프로필 섹션이 누락됨 |
| 413 | `DETECTION_REQUEST_TOO_LARGE` | 고정 Prompt의 전체 Context byte 또는 렌더링 출력 byte 제한을 초과함 |
| 413 | `NER_INPUT_TOO_LONG` | 표준 NER 서버가 원문 전체를 자동 절단 없이 처리할 수 없음 |
| 404 | `DEPLOYMENT_NOT_FOUND` | 요청한 NER 또는 LLM Deployment가 없음 |
| 409 | `DEPLOYMENT_DISABLED` | 요청한 Deployment가 비활성화됨 |
| 422 | `DEPLOYMENT_KIND_MISMATCH` | NER 자리에 LLM 또는 LLM 자리에 NER Deployment를 요청함 |
| 503 | `REGISTRY_NOT_INITIALIZED` | Registry Manager가 준비되지 않음 |
| 503 | `APPLICATION_RUNTIME_UNAVAILABLE` | FastAPI Runtime을 조회할 수 없음 |
| 503 | `BACKEND_PROVIDER_NOT_REGISTERED`, `BACKEND_PROVIDER_KIND_MISMATCH` | 실행할 Provider가 준비되지 않음 |
| 504 | `HTTP_NER_TIMEOUT` | 외부 NER 서버 응답 제한 시간을 초과함 |
| 500 | `HTTP_NER_CONFIG_INVALID` | 표준 NER Deployment 또는 Backend 실행 설정이 잘못됨 |
| 502 | `HTTP_NER_REQUEST_FAILED` | 외부 NER 서버로 요청을 전달하지 못함 |
| 502 | `HTTP_NER_HTTP_ERROR`, `HTTP_NER_RESPONSE_INVALID`, `HTTP_NER_RESPONSE_TOO_LARGE` | 외부 NER 서버 응답을 공통 Detection으로 변환할 수 없음 |
| 504 | `OPENAI_COMPATIBLE_TIMEOUT` | 모델 서버 응답 제한 시간을 초과함 |
| 500 | `OPENAI_COMPATIBLE_CONFIG_INVALID` | 모델 서버 실행 설정이 잘못됨 |
| 502 | `OPENAI_COMPATIBLE_REQUEST_FAILED` | 모델 서버로 요청을 전달하지 못함 |
| 502 | `OPENAI_COMPATIBLE_HTTP_ERROR`, `OPENAI_COMPATIBLE_RESPONSE_INVALID`, `OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE` | 모델 서버 응답을 공통 결과로 변환할 수 없음 |
| 502 | `LLM_DETECTION_OUTPUT_*` | LLM 탐지 출력 JSON·후보·크기·개수 계약 위반 또는 후보 문자열이 원문에 없음 |
| 502 | `DETECTION_RESULT_INVALID` | NER 또는 LLM 결과의 Span·원문·source 계약 위반 |
| 500 | `DETECTION_PROMPT_RENDER_FAILED` | 고정 탐지 Prompt 렌더링 실패 |
| 500 | `DETECTION_BACKEND_RESULT_INVALID` | LLM Backend의 공통 결과 계약 위반 |

공통 Backend 오류의 HTTP 상태와 메시지는
`app/api/error_handlers.py`의 전역 handler가 선택한다. Adapter 고유 `code`는
유지되지만 `/detect` Route는 OpenAI 호환 예외와 구현체를 직접 알지 않는다.
새 Adapter는 `BackendConfigurationError`, `BackendTimeoutError`,
`BackendTransportError`, `BackendResponseError`, `BackendInputTooLargeError` 중
적절한 타입을 상속하면
Route 또는 HTTP 매핑을 추가로 수정할 필요가 없다.

## 10. 운영 보안 조건

`/detect`는 개인정보·기밀정보가 포함될 수 있는 원문을 직접 받지만 자체 사용자
인증·인가는 제공하지 않는다. Gateway 뒤의 내부 네트워크에 배치하고 네트워크 접근
제어 또는 상위 Gateway 인증을 반드시 적용해야 한다.

- Reverse Proxy 또는 ASGI 배포 계층에서 HTTP 요청 본문 byte 상한을 적용한다.
- Access log와 Application log에 요청 본문, 원문, 렌더링 Prompt와 모델 출력을
  기록하지 않는다.
- TLS 종료와 내부 구간 보호 정책을 배포 환경에 맞게 적용한다.
- 현재 애플리케이션 자체는 JSON 본문을 파싱하기 전의 전송 byte 상한을 강제하지
  않으므로 운영 계층의 제한이 필요하다.
