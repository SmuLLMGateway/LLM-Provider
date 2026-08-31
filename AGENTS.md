# AGENTS.md

## 1. 프로젝트 개요

이 프로젝트는 **사용자 입력에 포함된 개인정보·민감정보·기업 기밀정보를 탐지하고, 보안 정책에 따라 로컬 LLM 또는 외부 LLM Provider로 요청을 전달하는 Privacy-Preserving LLM Gateway 시스템**이다.

이 저장소는 전체 시스템 중 **LPL(Local Private LLM) 서비스**를 담당한다.

LPL 서비스의 핵심 역할은 다음과 같다.

- 이름, 주소, 회사명 등의 비정형 개인정보를 전용 NER로 우선 탐지
- Regex·NER 후보를 Local LLM으로 판정하고 누락된 민감정보와 문맥형 기밀정보를 추가 탐지
- Gateway가 병합한 전체 Detection을 바탕으로 Local LLM이 동일 대상을 그룹화하고
  외부 Provider 전달용 원문을 마스킹한 뒤 결과를 fail-closed로 검증
- Option A에서 사용자 원문을 Local LLM으로 처리
- 첫 사용자 메시지를 고정 System Prompt로 요약해 대화 제목 생성
- 요청의 Deployment ID에 따라 NER·LLM Endpoint를 선택하고 역할별 고정 프롬프트를 적용
- 서로 다른 모델 서버의 요청·응답 형식을 공통 형식으로 정규화

---

## 2. 전체 시스템 흐름

전체 요청은 크게 **탐지 단계**와 **처리 단계**로 나뉜다.

### 2.1 탐지 단계

```text
Frontend
   │
   │ 사용자 입력
   ▼
Gateway
   ├─ 전화번호, 카드번호 등 정형 정보 Regex 탐지
   └─ 원문 + Regex 탐지 결과를 LPL에 전달
          │
          ▼
      LPL Service
          ├─ NER 개체 탐지
          ├─ Local LLM Regex·NER 후보 판정
          ├─ Local LLM 누락·문맥형 정보 추가 탐지
          ├─ 결과 검증
          └─ 공통 Detection 형식으로 정규화
          │
          ▼
Gateway
   ├─ CONFIRMED Regex + LPL Detection 병합
   ├─ UNCERTAIN 사용자 검토
   └─ Frontend에서 탐지 영역 하이라이트
```

### 2.2 처리 단계

사용자는 탐지 결과를 확인한 뒤 처리 방식을 선택한다.

```text
Option A
원문 → LPL → Local Private LLM → 결과 반환

Option B
원문 + 전체 Detection → Gateway가 POST /mask 호출
                      → LPL Local LLM 마스킹·검증
                      → Gateway가 maskedText를 외부 Provider로 전달
                      → 결과 반환
```

- **Option A**는 민감정보를 포함한 원문을 외부로 보내지 않고 로컬 환경에서 처리한다.
- **Option B**는 Gateway가 `CONFIRMED` Regex와 LPL `detections`를 병합해 `POST /mask`로
  전달하고, LPL이 Local LLM으로 마스킹한 `maskedText`를 Gateway가 외부 LLM
  Provider로 전달한다.
- LPL은 마스킹 결과 생성·검증까지만 담당하며 외부 Provider를 호출하지 않는다.

### 2.3 대화 제목 생성

```text
첫 사용자 메시지
→ Gateway가 POST /titles 호출
→ LPL이 고정 제목 생성 System Prompt와 Local LLM으로 제목 생성·검증
→ Gateway가 반환된 title을 대화방 metadata로 저장
```

LPL은 어떤 메시지가 첫 메시지인지 판단하거나 대화방 제목을 저장하지 않는다.
호출 시점, 재시도와 영구 저장은 대화 상태를 소유한 Gateway가 담당한다.

---

## 3. Gateway와 LPL의 책임 경계

### 3.1 Gateway의 책임

Gateway는 전체 요청 흐름과 외부 시스템 경계를 관리한다.

- Frontend 요청 수신
- 사용자 인증과 사용자 정보 관리
- 요청 식별자 생성 및 요청 상태 관리
- Regex 기반 정형 민감정보 탐지
- 제공하는 조직의 관리자 `organizationProfile` 저장·관리와 요청별 `sourceType` 조립
- LPL 호출
- LPL 후보 판정에 따른 CONFIRMED Regex와 신규 Detection 병합
- UNCERTAIN 후보의 사용자 검토 상태 관리
- 사용자 Option A/B 선택 처리
- Option B에서 원문과 전체 Detection으로 LPL `POST /mask` 호출
- 외부 LLM Provider 호출
- 요청, 탐지 결과, 최종 결과의 영구 저장
- 첫 사용자 메시지 이후 제목 생성 호출과 반환된 대화 제목 저장
- Frontend에 최종 결과 반환

### 3.2 LPL의 책임

LPL은 로컬 모델 실행과 모델 조합을 담당한다.

- 고정 표준 HTTP 계약으로 전용 NER 서버 호출
- Regex·NER 후보 판정과 누락·문맥형 기밀정보 추가 탐지를 위한 Local LLM 호출
- 선택적인 요청 범위 조직 Context의 구조·활성 정책별 완전성과 sourceType 검증·Prompt 전달
- 전체 Detection의 겹친 span 합집합 계산, Local LLM 동일 대상 그룹화·마스킹과
  `maskedText`·replacement 결과의 fail-closed 검증
- Option A의 Local LLM 생성 처리
- 첫 사용자 메시지의 Local LLM 제목 생성과 출력 검증
- 요청별 Deployment ID 해석
- Deployment 추가·전체 수정·활성 상태 변경·삭제의 검증, 종류별 원자 저장과 Snapshot Reload·복원
- NER·LLM Backend 선택
- 고정 탐지·마스킹·제목 프롬프트의 검증·컴파일과 렌더링
- 모델별 요청·응답 형식 변환
- 모델 출력 검증과 정규화
- 모델 실행 정보 및 지연시간 측정

### 3.3 LPL이 담당하지 않는 영역

- 운영 사용자용 Frontend 화면 처리
- Regex 탐지
- 외부 Provider 호출
- 사용자별 Deployment 접근·조합 정책
- Gateway DB 관리
- 사용자 요청의 영구 저장
- 조직 프로필과 텍스트 출처의 영구 저장
- 대화방 제목의 저장, 수정·삭제와 제목 생성 시점 관리

LPL은 가능한 한 **Stateless Runtime**으로 유지한다. 사용자, 원문, 탐지 이력,
마스킹 결과나 `entityId ↔ placeholder` 매핑을 장기 보관하는 시스템이 아니라,
요청 시점에 선택된 실행 구성을 해석하고 모델을 실행하는 서비스이다. 마스킹
매핑은 요청 범위에서만 일관되며 여러 메시지에서 같은 placeholder가 필요하면
대화 상태를 소유한 Gateway가 별도 상태 계약을 관리한다.

`app/frontend`의 정적 화면은 이 책임 경계의 예외가 아니라 LPL API를 수동 검증하는
내부 개발 도구다. 운영 사용자용 화면, Gateway Frontend, 인증·권한 또는 사용자별
정책을 구현하지 않는다.

---

## 4. 권장 서버 구조

```text
Gateway
   │
   ▼
LPL API / Orchestrator
   ├─ Static Functional Test UI (`/ui/`)
   ├─ Deployment Resolver
   ├─ Deployment Management Service
   ├─ Execution Plan
   ├─ Registry Manager
   ├─ Registry File Store
   ├─ Fixed Prompt Artifacts
   ├─ Backend Contract Registry
   ├─ Backend Provider Registry
   ├─ Detection Pipeline
   ├─ Masking Pipeline
   ├─ Generation Pipeline
   ├─ Title Generation Pipeline
   ├─ NER Backend
   └─ LLM Backend
          │
          ├─ Standard HTTP NER Server
          ├─ OpenAI-compatible LLM Server
          └─ Custom LLM Server
```

초기에는 다음 구성을 기본으로 한다.

```text
LPL API
   ├─ GET /ui/로 내부 기능 테스트용 정적 화면 제공
   ├─ GET /adapters/llm으로 현재 실행 가능한 LLM Adapter 목록 조회
   ├─ GET /deployments/ner로 NER Deployment 운영 카탈로그 조회
   ├─ POST /deployments/ner로 NER Deployment 추가
   ├─ GET /deployments/ner/{deployment_id}로 NER Deployment Gateway용 운영 상세 조회
   ├─ PUT /deployments/ner/{deployment_id}로 NER Deployment 전체 설정 교체
   ├─ PATCH /deployments/ner/{deployment_id}/enabled로 NER Deployment 활성 상태 변경
   ├─ DELETE /deployments/ner/{deployment_id}로 비활성 NER Deployment 삭제
   ├─ GET /deployments/llm으로 LLM Deployment 운영 카탈로그 조회
   ├─ POST /deployments/llm으로 LLM Deployment 추가
   ├─ GET /deployments/llm/{deployment_id}로 LLM Deployment Gateway용 운영 상세 조회
   ├─ GET /deployments/llm/{deployment_id}/limits로 LLM 컨텍스트 한도 조회
   ├─ PUT /deployments/llm/{deployment_id}로 LLM Deployment 전체 설정 교체
   ├─ PATCH /deployments/llm/{deployment_id}/enabled로 LLM Deployment 활성 상태 변경
   ├─ DELETE /deployments/llm/{deployment_id}로 비활성 LLM Deployment 삭제
   ├─ POST /detect로 NER 개체 탐지 후 LLM 후보 판정·추가 탐지 실행
   ├─ POST /mask로 Local LLM 동일 대상 그룹화와 민감정보 마스킹 실행
   ├─ POST /generate로 Option A 생성 실행
   ├─ POST /titles로 첫 사용자 메시지의 대화 제목 생성
   ├─ POST /deployments/ner/{deployment_id}/probe로 NER 실제 연결 검사
   ├─ POST /deployments/llm/{deployment_id}/probe로 LLM 실제 연결 검사
   ├─ GET /policies/enabled로 현재 전역 활성 Policy ID 조회
   ├─ PUT /policies/enabled로 전역 활성 Policy ID 전체 교체
   ├─ NER 모델은 표준 API를 제공하는 별도 서버로 실행
   └─ Local LLM은 별도 추론 서버로 실행
```

FastAPI lifespan은 시작 시 Registry Snapshot과 기본 Backend Provider를 조립하고
`RegistryManager`, `DetectionPipeline`, `MaskingPipeline`, `GenerationPipeline`,
`TitleGenerationPipeline`, `LlmLimitsService`와 `DeploymentManagementService`를 초기화한다.
`GET /adapters/llm`은 애플리케이션이 실제로 조립한 동일한 `BackendRegistry`와
`BackendProviderRegistry`를 사용한다. 같은 `adapterType`으로 설정 계약과 실제
Provider가 모두 등록된 실행 가능한 LLM 구현체만 골라 `adapterType` 순으로
반환한다. NER는 선택 가능한 Adapter를 노출하지 않고 고정 공통 HTTP 계약만
사용하므로 `GET /adapters/ner`를 제공하지 않는다.
`GET /deployments/ner`, `GET /deployments/llm`과 각 종류별 상세 Endpoint는 같은
`RegistryManager`의 현재 Active Snapshot에서 요청 경로와 `kind`가 일치하는
Deployment를 조회한다. 목록은 `deploymentId`와 `enabled`만 반환한다. NER 상세는
`baseUrl`, `timeoutMs`를 추가하고, LLM 상세는 `adapterType`과 설정된 `baseUrl`,
`modelName`, `timeoutMs`를 추가한다. 상세 운영 정보는 Frontend에 전달하지 않는다.
종류별 `POST` Endpoint는 요청 본문의 `deploymentId`로 새 Deployment를 추가하고,
종류별 `PUT` Endpoint는 경로의 `deployment_id`에 해당하는 Deployment 전체 설정을
교체한다. 종류별 `PATCH .../enabled` Endpoint는 정확히 `enabled` boolean 하나만
받아 기존 실행 설정을 보존한 채 활성 상태만 변경한다. 경로가 이미 종류를
결정하므로 관리 요청 본문에는 `kind`를 받지 않는다. 종류별 `DELETE` Endpoint는
Request Body 없이 `enabled=false`인 Deployment만 삭제하고 성공 시 본문 없는
`204 No Content`를 반환한다. 활성 Deployment는 먼저 PATCH로 비활성화해야 한다.
변경은 `DeploymentManagementService`가 직렬화하며 해당 종류의 JSON 파일만
원자적으로 저장한 뒤 Active Snapshot을 Reload한다. 후보 활성화가 실패하면 이전
파일과 Snapshot으로 복원한다. 동일한 `enabled` 값을 다시 요청하는 PATCH는
멱등적으로 성공하며 `NO_CHANGE`도 정상 결과로 처리한다. 삭제 역시 같은 종류별
원자 저장, Snapshot Reload와 실패 시 복원 정책을 사용한다.
FastAPI Runtime은 오프라인 파일 편집 도구를 생성하거나 보관하지 않는다.
`POST /generate`는 필수 사용자 입력 `text`, 선택적 이전 Local LLM 응답 배열
`previousText`와 필수 `llmDeploymentId`를 받아 Generation Pipeline을 실행한다.
`previousText` 각 항목은 `role=user|assistant`와 `content`를 가지며 배열의 역할과
순서를 보존한다. 현재 `text`는 마지막 `user` 메시지로 전달한다.
`GET /deployments/llm/{deployment_id}/limits`는 Registry 토큰 설정과 조회 가능한
Ollama `/api/show`·`/api/ps` 값을 조합해 모델·실행·유효 컨텍스트, 출력 예약량,
최대 입력량과 source를 반환한다. 자동 조회 실패는 Registry fallback 또는
`source=unknown`으로 표현한다.
`POST /detect`는 필수 `nerDeploymentId`와
`llmDeploymentId`, 필수 `sourceType`과 선택적 `organizationProfile`을 받아 NER 후
LLM 후보 판정·추가 탐지를 실행한다. 조직 프로필은 Gateway가 제공 가능한 경우
전달하고 LPL은 요청 범위에서만 사용한다. 생략하거나 `null`이면 Prompt Context에
명시적인 null을 넣는다. 활성 정책은 요청에
포함하지 않고 별도 `GET/PUT /policies/enabled`와 `config/policy_settings.json`으로
관리한다. 빈 객체 `{}`나 필요한 Deployment ID가 누락된 요청은 유효하지 않다.
현재 비활성 정책을 참조하는 RegexCandidate도 거부한다. 응답의
`candidateDecisions`는 모든 Regex·NER 후보의 판정을 보존하고, `detections`에는
CONFIRMED NER와 신규 LLM 탐지만 포함한다. CONFIRMED Regex는 Gateway가 판정
응답과 자신이 보관한 후보를 이용해 Detection으로 조립한다. REJECTED와 검토 전
UNCERTAIN은 자동 마스킹 대상에 포함하지 않는다.
프로필 객체를 제공한 경우 `P01`, `S02`, `S03`, `B02`, `B03`가 활성 상태일 때 각각
필요한 privacy, securityContext, thirdPartyContext,
confidentialTechnologyContext가 빠지면
`422 ORGANIZATION_PROFILE_INCOMPLETE`를 반환한다.
조직 Context Enum은 Gateway 계약의 축약값을 사용한다. 조직 유형은
`PRIVATE | PUBLIC | FINANCE | MEDICAL | EDUCATION | DEFENSE | OTHER`, 공개 항목은
`PRODUCT | SERVICE | PROJECT | CUSTOMER | PARTNER | SYSTEM | TECH | OTHER`, 내부 시스템
환경은 `PRODUCT | STAGING | DEV | TEST | OTHER`, 기밀 자산은
`PROJECT | PRODUCT | TECH | REPO | RESEARCH | DATASET | OTHER`, 제3자 관계는
`CUSTOMER | PARTNER | SUPPLIER | CONTRACTOR | OTHER`다.
`privacy.persons`는 보호할 조직 관계자의 name·aliases 목록이며 생략하면 빈 배열로
처리한다. 명단이 비어 있지 않으면 등록 인명만 P01로 확정한다. 조직 프로필이
제공되면 공개 Context는 제외 근거로, 등록 내부 인프라·시스템은 S02, 비공개
고객·협력사는 B02, 기밀 기술자산은 B03의 우선 탐지 근거로 사용한다. 등록되지 않은
IP·도메인·시스템명은 형식만으로 조직 내부정보라고 확정하지 않는다.
`POST /mask`는 필수 `text`, `llmDeploymentId`와 필수 `detections` 배열을 받아
Masking Pipeline을 실행한다. `detections`에는 Gateway가 병합한 전체
`regex`·`ner`·`llm` Detection을 전달한다. 빈 배열은 유효하며 LLM을 호출하지 않고
Deployment 존재·활성 상태·종류를 검증한 뒤 원문과 빈 replacement 배열을 반환한다.
결과는 `maskedText`와 원문 좌표의
`start`, `end`, 요청 범위 `entityId`, `placeholder`, `types`, `sources`를 가진
replacement 배열이다.
`POST /titles`는 Gateway가 전달한 첫 사용자 메시지 `text`와 필수
`llmDeploymentId`를 받아 고정 제목 생성 System Prompt로 한 줄 제목을 만든다.
LPL은 대화방이나 제목을 저장하지 않으며 Gateway가 첫 사용자 메시지 후 이
Endpoint를 호출하고 응답의 `title`을 영구 저장한다.

### 4.1 내부 기능 테스트 UI

FastAPI는 `app/frontend`의 정적 파일을 같은 origin의 `/ui/`에 제공한다. 별도
Frontend 개발 서버, Node.js, 패키지 설치나 빌드 단계는 없다. 화면의 JavaScript는
상대 API 경로를 사용해 다음 기능을 수동 호출한다.

- NER·LLM Deployment 목록 조회, 추가와 전체 수정
- NER·LLM Deployment 실제 연결 Probe
- `/detect`, `/mask`, `/generate`, `/titles` 실행
- 성공·실패 상태, 성공 응답 JSON과 HTTP 오류 JSON 표시

이 화면은 내부 개발·통합 테스트 전용이다. 운영 사용자용 UI가 아니며 인증,
권한, 사용자별 Deployment 필터링이나 Secret 관리 기능을 제공하지 않는다. 입력한
원문과 Deployment 설정은 브라우저에서 같은 origin의 LPL API로 실제 전송된다.
따라서 인터넷이나 신뢰할 수 없는 네트워크에 직접 공개하지 않고 테스트 데이터와
허가된 설정만 사용한다. 자세한 실행 방법과 파일 역할은
[기능 테스트용 Frontend](./docs/frontend.md)를 따른다.

NER는 반드시 고정 공통 Backend 인터페이스 뒤에 두고, 모델 서버 또는 Wrapper가
Registry의 `baseUrl`에 등록된 전체 POST Endpoint에서 표준 JSON 계약을 제공해야 한다.

모델은 요청마다 다운로드하거나 다시 로딩하지 않는다. NER와 LLM은 별도 추론
서버에서 미리 로드되어 있어야 하며, LPL은 등록된 Deployment로 요청을 라우팅한다.

---

## 5. 핵심 설계: Adapter + Registry + Request Selection

이 프로젝트는 다양한 NER·LLM Endpoint를 Registry에 등록하고 요청에서 역할별
Deployment ID를 직접 선택한다.

```text
Adapter
+ Registry
+ Request Deployment Selection
```

---

## 6. Adapter

Adapter는 모델 서버마다 다른 요청·응답 형식을 LPL 내부의 공통 인터페이스로 변환한다.

### 6.1 고정 NER HTTP 계약

모든 NER Backend는 개념적으로 다음 역할을 제공한다.

```python
class NerBackend(Protocol):
    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        ...
```

NER Deployment는 사용자에게 `adapterType`을 받거나 Adapter를 선택하게 하지
않는다. 모든 NER 서버가 아래 고정 HTTP 계약을 제공하고, LPL은 내부의 단일
`HttpNerBackend`로만 호출한다. 내부 런타임 호환을 위한 `http_ner` 키는 저장 파일,
관리 API와 Frontend에 노출하지 않는다.

NER Deployment Probe는 별도 Health 경로를 추측하지 않는다. 현재
Snapshot에서 요청 경로의 종류와 ID가 일치하는 NER Deployment를 활성 상태와
관계없이 선택한 뒤 고정 최소 입력 `"A"`로 기존 `detect()`를 한 번 호출한다.
고정 요청 직렬화, HTTP 호출, 응답 정규화와 공통 Detection Span
검증까지 모두 통과해야 `status=available`과 `latencyMs`를 반환한다.
`enabled=false`인 Deployment도 활성화 전 연결 상태를 점검할 수 있도록 실제
요청을 보낸다. Probe는 관리 진단 작업일 뿐 Deployment 설정, `enabled` 값이나
Active Snapshot을 변경하거나 자동 활성화하지 않는다. Provider가 등록되지 않은
경우에는 실제 호출 없이 `503 BACKEND_PROVIDER_NOT_REGISTERED`를 반환하며, 그 밖의
실패는 기존 공통 Backend HTTP 오류 계약을 사용한다.

`baseUrl`은 호출 경로까지 포함한 NER 서버의 전체 POST Endpoint URL이며
`timeoutMs`와 함께 필수다. LPL은 별도 경로를 붙이지 않고 `POST {baseUrl}`을
호출한다. 외부 서버에는 단일 문자열 필드
`text`만 보내고, 응답 최상위에는
`detections`만, 각 항목에는 `start`, `end`, `text`, `type`, `score`만 정확히
허용한다. 외부 응답의 `source`는 금지하며 검증 후 LPL Adapter가 항상 `ner`로
설정한다. 엄격한 JSON과 응답 byte 상한을 적용하고, Runtime 요청에서는 URL이
아니라 Registry에 등록된 `nerDeploymentId`만 선택한다.

`services/gliner_ner`는 이 표준 계약을 실제로 제공하는 별도 FastAPI Docker
이미지다. 모델별 라벨, 임계값과 출력 변환을 서버 내부에서 소유하며, 시작 시 고정
revision의 `urchade/gliner_multi-v2.1`을 한 번 로드한다. 내부
`microsoft/mdeberta-v3-base` 설정과 Tokenizer도 별도 commit SHA로 고정하고,
원본 Hugging Face 캐시를 수정하지 않은 실행용 설정에 해당 snapshot 경로를
주입한다. GPU 추론은 프로세스당
하나의 Lock으로 직렬화하고 Uvicorn worker도 하나만 사용한다. 모델 출력은 서버에서
span, 원문 조각, 내부 라벨과 score를 다시 검증한 뒤 공통 `detections` 응답으로
만든다. 고정 한국어 라벨 세트는 모델 추론 중에만 영어 의미 라벨로 변환하고 결과를
다시 공통 `type`으로 복원한다. 기본 모델 한도를 넘는 입력은 GLiNER의
자동 truncation에 맡기지
않고 `413 NER_INPUT_TOO_LONG`으로 fail-closed 처리한다. `HttpNerBackend`는 표준
서버의 크기가 제한된 정확한 413 JSON 계약만 공통 장문 입력 오류로 승격하며 LPL
API도 413을 반환한다. LPL과 같은 Docker network에서 컨테이너 이름이
`ner-server`이면 Deployment `baseUrl`은
`http://ner-server:8008/v1/ner/detect`이다. 빌드와 실행은
[GLiNER NER 서버](./docs/ner_server.md)를 따른다.

이전 `gliner_http`, `hf_inference_token_classification`과 임의 `http_ner`
Deployment 형식은 더 이상 외부 Registry 계약이 아니다. 해당 서버를 사용하려면
서버 또는 앞단 Wrapper가 위 표준 API를 제공해야 한다.

### 6.2 LLM Adapter

모든 LLM Backend는 개념적으로 다음 역할을 제공한다.

```python
class LlmBackend(Protocol):
    async def generate(
        self,
        messages: list[dict[str, object]],
        deployment: DeploymentConfig,
        parameters: dict[str, object],
        output_schema: dict[str, object] | None = None,
    ) -> LlmResult:
        ...
```

LLM Deployment Probe도 별도 Health 경로를 가정하지 않는다. 현재 Snapshot에서
요청 경로의 종류와 ID가 일치하는 LLM Deployment를 활성 상태와 관계없이 선택하고
`user` 메시지 `"A"`와 공통 출력 제한 `max_tokens=1`로 기존 `generate()`를 실제
호출한다. `output_schema`는 사용하지 않으며 Adapter는 비스트리밍 요청으로
변환한다. 응답이 공통 `LlmResult` 계약을 다시 통과해야 `status=available`과
`latencyMs`를 반환한다. `enabled=false`여도 실제 요청이므로 모델을 로드하거나
Cold Start가 발생할 수 있지만, Probe가 Deployment를 자동 활성화하거나 설정과
Snapshot을 변경하지는 않는다. Provider가 등록되지 않은 경우에는
`503 BACKEND_PROVIDER_NOT_REGISTERED`를 반환한다. 출력 본문, 모델 이름과 Usage는
Probe 응답이나 영구 저장소에 남기지 않는다. 향후 Native LLM Adapter는 공통
`max_tokens` 값을 자신의 출력 토큰 제한 필드로 변환해야 한다.

현재 기본 구현체:

```text
MockLlmBackend
OpenAICompatibleLlmBackend
```

향후 확장 가능한 구현체:

```text
CustomHttpLlmBackend
```

### 6.3 Adapter 사용 원칙

- Detection, Masking과 Generation Pipeline은 특정 모델 SDK에 직접 의존하지 않는다.
- 모델별 조건문을 파이프라인에 누적하지 않는다.
- NER는 고정 `HttpNerBackend`와 표준 서버 계약만 사용하며 사용자에게 Adapter를
  선택하게 하지 않는다.
- LLM은 `BackendRegistry`와 `BackendProviderRegistry`의 `(llm, adapterType)` 키로
  설정 계약과 실제 Backend 구현체를 선택한다.
- 새 LLM Adapter는 설정 계약과 실제 Provider를 같은 키로 함께 등록한다.
- Backend 실행 오류는 `BackendConfigurationError`, `BackendTimeoutError`,
  `BackendTransportError`, `BackendResponseError` 중 의미에 맞는 공통 타입을
  상속하고 Adapter 고유의 안전한 `code`를 제공한다.
- FastAPI Route는 Adapter 구현체와 전용 예외를 직접 import하지 않으며,
  공통 Backend 오류는 전역 exception handler가 HTTP 응답으로 변환한다.
  이 계약을 따르는 새 Adapter는 API Route나 HTTP 매핑을 수정하지 않고 추가한다.
- 모든 Backend 결과는 LPL의 공통 도메인 모델로 변환한다.
- 새 NER 모델은 표준 HTTP 계약을 구현한 서버 또는 Wrapper로 연결하고, 새 LLM
  서버 형식은 기존 Pipeline을 수정하기보다 LLM Adapter를 추가한다.

### 6.4 Adapter 목록 API

LPL은 현재 프로세스에서 실행 가능한 LLM Adapter 목록을 다음 읽기 전용
Endpoint로 제공한다.

```text
GET /adapters/llm
```

응답에는 같은 `(llm, adapterType)`으로
`BackendRegistration` 설정 계약과 `BackendProviderRegistration` 실제 구현체가
모두 존재하는 항목만 포함한다. 계약만 있거나 Provider만 있는 항목은 실행 가능한
Adapter로 노출하지 않으며 문자열을 `adapterType` 순으로 정렬한다.

```json
{
  "adapters": [
    "mock",
    "openai_compatible"
  ]
}
```

응답은 `adapters`라는 배열 하나만 가지며 각 원소는 Deployment의 `adapterType`에
사용할 문자열이다. 종류는 `/llm` 경로로 이미 표현되므로 `kind`를 포함하지 않는다.
이 목록에 포함됐다는 사실은 LPL 프로세스가 해당 구현체를 선택해 호출할 수 있다는
뜻일 뿐 모델 Endpoint 연결 성공이나 모델 가용성을 보장하지 않는다. 실제 연결은
등록된 LLM Deployment Probe로 검사한다. NER는 Adapter 목록 API 없이 등록된
Deployment를 직접 Probe한다.

문자열 목록 외에 Deployment URL, Secret, 설정값, Provider 객체·클래스명이나
모듈 경로를 반환하지 않는다. Adapter별 설정 검증의 최종 기준은
Deployment POST·PUT에서 사용하는 서버의 `BackendRegistration` 계약이다.

---

## 7. Registry

Registry는 LPL에서 사용할 수 있는 다음 리소스를 관리한다.

```text
Deployments
```

초기 구현에서는 `config/ner_deployments.json`,
`config/llm_deployments.json`과 물리 파일 `config/prompts.j2`,
`config/policy_prompts.json`, `config/mask_prompt.j2`, `config/title_prompt.j2`를 사용한다. NER·LLM 파일은 저장 책임만 분리하며,
런타임은 두 Deployment 맵과 고정 Prompt Artifact를 하나의 Registry Snapshot으로
조립한다. 현재 관리 API는 이 파일 저장 계층을 사용하며, 향후 저장소를 DB로
교체하더라도 같은 관리 서비스와 Runtime Snapshot 계약을 유지할 수 있다.
실행 중인 온라인 Deployment 변경은 `DeploymentManagementService`만 소유한다.
`OfflineRegistryEditor`는 프로세스가 중지된 상태의 초기 설정, 마이그레이션과
수동 정비 전용이며 Snapshot Reload나 Rollback을 수행하지 않는다.

### 7.1 Deployment

Deployment는 모델의 이름 자체가 아니라 **현재 실제로 호출 가능한 모델 인스턴스**를 의미한다.

예:

```text
논리 모델: Qwen 계열 Local LLM
Deployment A: GPU 서버 1의 Qwen Endpoint
Deployment B: GPU 서버 2의 Qwen Endpoint
```

동일한 모델이라도 Endpoint, 모델 이름, timeout, 실행 환경이 다르면 별도 Deployment로 관리한다.

주요 속성:

```text
kind             ner | llm (파일명에서 주입되는 런타임 내부 값)
baseUrl          NER 전체 POST Endpoint 또는 LLM 서버 기본 주소
timeoutMs        모델 서버 호출 제한 시간
enabled          일반 Runtime 실행 허용 여부(관리용 Probe 제외)
adapterType      LLM에서만 사용하는 Adapter 종류
modelName        LLM Adapter가 사용하는 모델 이름 또는 alias
contextWindowTokens  LLM 전체 컨텍스트 토큰 한도(선택)
```

NER의 `adapterType`은 물리 파일이나 외부 API 필드가 아니다. 파일 로드시 내부 고정
Backend 키 `http_ner`를 주입하며, 모든 NER 서버는 표준 HTTP 계약을 구현한다.

### 7.2 Prompt

Prompt는 ID로 등록하거나 요청에서 선택하지 않는다. 런타임은 역할별 고정 Jinja2
파일 세 개와 정책별 고정 JSON 하나를 사용한다.

```text
config/prompts.j2          Regex·NER 후보 판정과 누락·문맥형 기밀정보 추가 탐지
config/policy_prompts.json P01~B02 정책별 세부 탐지 지침
config/mask_prompt.j2      동일 대상 그룹화와 마스킹 계획 생성
config/title_prompt.j2     첫 사용자 메시지의 대화 제목 생성
```

네 파일명은 코드 상수다.
`application_runtime()`은 `config_dir` 아래의 네 파일을 자동 사용하며 Runtime
요청, Registry 설정이나 Pipeline이 경로를 주입·선택하지 않는다.
`PromptLoader.load()`도 인자를 받지 않는다.

`PromptArtifact.compile()`이 비어 있는 본문, UTF-8, 크기, Jinja 문법과 외부 변수
계약을 직접 검증한다. 외부 변수는 정확히 문자열 `text`와
`existing_detections` 두 개뿐이다. Artifact는 Prompt 원문이나 metadata를 보관하지
않고 컴파일 Handle과 본문 해시만 보관하며 `render()`를 직접 제공한다. 렌더링
입력은 이 두 문자열로 고정하고, 전체 Context byte, 렌더링 출력 byte와 timeout만
제한한다.

`TitlePromptArtifact.compile()`은 제목 생성 Prompt가 외부 변수를 참조하지 않는
정적 템플릿인지 검증한다. 제목 생성 Pipeline은 렌더링한 본문을 `system`
메시지로, Gateway가 전달한 첫 사용자 메시지를 별도 `user` 메시지로 보내며
`max_tokens=32`를 사용한다. 모델 출력은 한 줄, 1~30자, UTF-8 120 bytes 이하의
일반 텍스트 제목인지 다시 검증한다.

`MaskPromptArtifact.compile()`도 외부 변수가 없는 정적 템플릿만 허용한다. Masking
Pipeline은 렌더링한 본문을 `system` 메시지로 사용하고, 검증된 원문·겹친 Detection
span의 합집합 component·서버 생성 placeholder namespace를 별도 canonical JSON
`user` 메시지로 전달한다. 모델은 `maskedText`와 각 `targetId`의 요청 범위
`entityId` 할당만 출력하며 원문 span·type·source·replacement placeholder 필드는
만들지 않는다.
LPL이 원문 좌표, `types`, `sources`, `entityId`와 placeholder를 검증된 입력에서
파생한다. 별도 user JSON에는 Prompt 렌더링 Context 제한과 독립된 UTF-8 byte
상한을 적용한다. Backend에는 현재 target ID와 정확한 assignment 수를 제한한 요청별
`output_schema`와 `max_tokens=16384`를 전달하며, 구조화 출력 사용 여부와 관계없이
Parser와 Validator로 다시 검증한다.
MVP 기본 상한은 Detection·assignment 128개, canonical user JSON 12,288 bytes,
LLM 원출력 32,768 bytes다. 호출 전 모든 target을 서로 다른 entity로 가정한 최악
출력 JSON도 계산하며 16,384 bytes를 넘으면 Backend를 호출하지 않고 거부한다.

모든 탐지 요청은 NER를 먼저 실행한다. 이후 Regex와 NER 결과를 기존 탐지 근거로
`prompts.j2`에 전달한다. LLM은 모든 후보를 `CONFIRMED`, `REJECTED`, `UNCERTAIN`으로
판정하고 누락된 민감정보의 `text`, `type`, `score`도 추가 탐지한다. 근거 JSON은 활성 정책,
요청의 `organizationProfile`, `sourceType`, 원본 `regexCandidates`와 LPL이
생성한 `nerCandidates`를 분리한다. 조직 프로필이 없으면 명시적인 null을 사용하고
조직별 공개·내부 여부가 필요한 판단은 임의로 확정하지 않는다. Regex 후보는
`candidateId`, span, text, policyId, detailType, score를 보존한다. NER 결과는
검증과 Regex 중복 제거 후 원문 순서대로 `N001`부터 요청 범위 ID를 부여하며,
`candidateId`, span, text, policyId, entityType, score를 포함한다. NER 후보는
P01/PERSON 또는 P04/LOCATION의 고정 조합만 허용한다. 정책별 Prompt는 후보 유무와
관계없이 현재 `enabledPolicies` 전체를 고정 정책 순서로 중복 없이 조립한다. 활성 정책은
Prompt Context와 LLM output schema의 허용 type에도 동일하게 적용한다.
LLM의 신규 탐지 문자열이 원문에 정확히 없거나 문맥형 문자열이 고유하지 않으면
첫 실패 항목 index와 정확 복사 규칙을 전달해 같은 Local LLM에 전체 JSON을 최대 한
번 교정 요청한다. 두 번째 출력도 실패하면 퍼지 매칭이나 좌표 추측 없이 기존 오류로
fail-closed 처리한다. 세 Jinja2 Prompt와 정책 JSON은 배포된 읽기 전용 리소스이며
애플리케이션에는 Prompt 생성·수정·삭제 API나 서비스가 없다. Prompt를 변경하려면
소스와 배포 산출물을 갱신해 애플리케이션을 새로 배포하고 프로세스를 재시작해야
한다. `RegistrySnapshotBuilder`는 첫 정상 build에서 네 파일을 한 번씩 읽고
컴파일한 Artifact와 정책 Catalog를 캐시하며, 이후 Deployment 설정 Reload에서는 같은 객체를
재사용한다.

`ActiveRegistrySnapshot`은 검증·컴파일된 탐지·마스킹·제목 Prompt Artifact와
Deployment의 저장과 수명을 보장한다. 일반 Runtime 실행 계획을 만드는
`DeploymentResolver`는 요청 시작 시 캡처한 하나의 Snapshot에서 요청의 ID를
조회하고 활성 상태와 `kind`를 검증한 뒤 역할별 불변 실행 계획으로 조립한다.
Pipeline은 완성된 Plan만 소비한다. 관리용 Deployment Probe는 활성화 전 연결
상태도 진단해야 하므로 이 Runtime 활성 상태 제한을 적용하지 않고 존재 여부와
요청 경로의 종류를 검증한다.

---

## 8. JSON 기반 분리 Registry

권장 위치:

```text
config/
├─ ner_deployments.json
├─ llm_deployments.json
├─ policy_settings.json
├─ prompts.j2
├─ policy_prompts.json
├─ mask_prompt.j2
└─ title_prompt.j2
```

저장소에 포함된 두 Deployment 파일의 초기 내용은 각각 빈 객체 `{}`다. 빈 Registry도
정상 Active Snapshot으로 활성화되며 종류별 목록 API는 `{"deployments":[]}`를
반환한다. 실행 API를 사용하려면 먼저 종류별 Deployment 관리 API로 필요한 Endpoint를
등록해야 한다. 아래 JSON은 초기값이 아니라 등록 형식을 설명하기 위한 예시다.

두 Deployment 파일은 모두 별도의 최상위 래퍼 필드 없이 Deployment ID를 키로
사용하는 원시 객체 맵(raw object map)이다. 각 설정 객체에는 `kind`를 저장하지
않는다. `RegistryFileStore`가 NER 파일에는 `ner`, LLM 파일에는 `llm`을 내부
`DeploymentConfig.kind`로 주입한다. 명시적인 `kind` 필드는 중복이나 충돌을
막기 위해 거부한다. 물리 파일은 분리되어 있어도 Deployment ID는 전체
Registry에서 고유해야 하며 같은 ID를 두 파일에 동시에 등록할 수 없다.

`config/ner_deployments.json` 예시:

```json
{
  "ner-gliner-local": {
    "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
    "timeoutMs": 5000,
    "enabled": true
  }
}
```

`config/llm_deployments.json` 예시:

```json
{
  "llm-local-a": {
    "adapterType": "openai_compatible",
    "baseUrl": "http://local-llm-a:8000/v1",
    "modelName": "local-detector-a",
    "timeoutMs": 10000,
    "enabled": true
  }
}
```

애플리케이션은 첫 정상 Snapshot을 만들 때 종류별 Deployment JSON과 네 고정 Prompt 리소스
파일을 함께 검증한다. `RegistryFileStore`는 같은 파일 Coordinator Lock 안에서
두 Deployment 파일을 읽어 파일별 내부 `kind`를 주입하고 전역 ID 고유성을
검증한 뒤 하나의 `RegistryConfig.deployments` 맵으로 병합한다. Snapshot,
Resolver와 Pipeline은
물리 파일 두 개가 아니라 이 병합된 불변 실행 상태를 사용한다.

```text
첫 build:
ner_deployments.json ───┐
llm_deployments.json ───┼─ 로드·kind 주입·ID·Backend 검증 ─ 병합 ─ Active Snapshot
fixed detection prompt ─┤
fixed policy prompts ────┤
fixed mask prompt ───────┤
fixed title prompt ──────┴─ 한 번 검증·컴파일 ─ PromptArtifact 캐시

이후 try_reload:
ner_deployments.json ───┐
llm_deployments.json ───┼─ 로드·검증 ─ 병합
cached PromptArtifacts ─┴─ 재사용 ─ Active Snapshot
```

최초 시작에서 JSON 또는 어느 한 고정 Prompt가 파싱, 필드, Backend 계약, 템플릿 검증에
실패하면 Snapshot을 활성화하지 않는다. 이후 Reload는 Deployment 설정만 갱신하며
네 Prompt 리소스를 다시 읽지 않는다. 실행 중인 요청은 처리 시작 시점에 선택한 정상
Snapshot을 끝까지 사용한다. 이전 단일 파일 `config/deployments.json`이 남아 있으면
조용히 무시하지 않고 마이그레이션 오류로 시작 또는 Reload를 거부한다.
이전 파일은 기존 `kind` 값으로 두 파일에 분리한 뒤 새 파일 항목에서 `kind`를
제거해야 한다.

### 8.1 설정 규칙

- 표준 JSON을 사용한다.
- `ner_deployments.json`과 `llm_deployments.json`의 최상위 값은 각각
  Deployment ID-설정 객체 맵이다.
- 물리 파일 항목에는 `kind`를 두지 않는다. 파일명이 내부 `kind`를 결정하며,
  명시적인 `kind` 필드는 거부한다.
- Deployment ID는 두 파일을 통틀어 고유해야 한다.
- `deployments` 같은 래퍼 필드를 파일 안에 다시 넣지 않는다.
- JSON 문법 파싱과 직렬화는 `app/core/json_codec.py`의 공통 함수를 사용한다.
- Registry JSON 구조는 `app/schemas/registry.py`의 Pydantic 모델을 유일한 기준으로 검증한다.
- 하나의 저장 작업에서 NER와 LLM 파일을 동시에 변경하지 않는다. 단건 추가·전체
  수정·활성 상태 변경은 해당 종류 파일 하나만 임시 파일로 완성한 뒤 원자 교체한다.
- 새 Adapter가 추가 설정을 필요로 하면 의미와 타입이 명확한 필드를 Pydantic
  Schema와 Backend 계약에 명시적으로 추가한다.
- 실제 API Key나 Secret을 파일에 평문으로 저장하지 않는다.
- 현재 MVP 스키마에는 인증 필드가 없으며, 필요한 Adapter는 환경변수나 Secret
  Manager를 사용하고 추후 명시적인 설정 계약을 추가한다.
- 프롬프트 본문은 역할별 고정 이름의 템플릿 파일에 둔다.
- 요청은 Registry에 등록된 Deployment ID만 전달하며 URL이나 모델 설정은 전달하지 않는다.
- `/detect`의 `nerDeploymentId`는 `kind=ner`, `llmDeploymentId`는 `kind=llm`이어야 한다.
- `/mask`의 `llmDeploymentId`는 `kind=llm`이어야 한다.
- `/generate`의 `llmDeploymentId`는 `kind=llm`이어야 한다.
- `/titles`의 `llmDeploymentId`는 `kind=llm`이어야 한다.
- `/detect`, `/mask`, `/generate`, `/titles`는 `enabled=false`인 Deployment를
  `DEPLOYMENT_DISABLED`로 거부한다.
- `/deployments/{kind}/{deployment_id}/probe`는 관리 진단 예외로 비활성
  Deployment도 실제 호출하지만 설정을 변경하거나 자동 활성화하지 않는다.
- `/deployments/{kind}/{deployment_id}/enabled`의 PATCH 본문은 정확히
  `{"enabled": boolean}`이며, 다른 실행 설정을 변경하지 않는다.
- `DELETE /deployments/{kind}/{deployment_id}`는 Request Body를 받지 않고
  비활성 Deployment만 삭제한다. 활성 Deployment는
  `DEPLOYMENT_MUST_BE_DISABLED`로 거부한다.
- 설정이나 Runtime 요청 어디에도 `promptId`, Prompt 경로나 본문을 두지 않는다.
  NER는 Prompt 없이 실행하고, 후속 LLM 후보 판정·추가 탐지는
  `config/prompts.j2`와 `config/policy_prompts.json`, 마스킹과
  제목 생성은 외부 변수가 없는 정적 `config/mask_prompt.j2`와
  `config/title_prompt.j2`를 각각 사용한다.
- 설정 파일은 로드 시 모든 Deployment의 Backend 계약을 검증한다.
- 알 수 없는 필드는 무시하지 않고 오류로 처리한다.
- 런타임 Snapshot에는 두 파일을 병합한 전체 Deployment 맵을 보관한다.
- 실행 중인 요청은 처리 시작 시점의 Registry Snapshot을 끝까지 사용한다.

### 8.2 Deployment 카탈로그와 운영 상세

LPL은 현재 Active Snapshot에 포함된 NER·LLM Deployment의 운영 요약과 Gateway용
운영 상세를 다음 조회 Endpoint로 제공한다.

```text
GET /deployments/ner
GET /deployments/ner/{deployment_id}
GET /deployments/llm
GET /deployments/llm/{deployment_id}
GET /deployments/llm/{deployment_id}/limits
```

각 목록은 경로가 지정한 종류만 Deployment ID 순서로 반환한다. 통합 목록
Endpoint와 `kind` Query Parameter는 제공하지 않는다. 목록 요약 필드는
`deploymentId`, `enabled` 두 개다. NER 상세 조회는 필수 `baseUrl`, `timeoutMs`를
추가하고 LLM 상세 조회는 필수 `adapterType`과 설정된 경우 `baseUrl`, `modelName`,
`timeoutMs`를 추가한다. 선택적 LLM 값이 없으면 `null` 대신 필드를 생략한다.
상세 경로와 Deployment의 실제 `kind`가 다르거나
ID가 존재하지 않으면 모두
`404 DEPLOYMENT_NOT_FOUND`를 반환한다.

응답 경로가 이미 NER와 LLM 종류를 나타내므로 목록·상세 응답에는 `kind`를
중복해서 포함하지 않는다. 내부 `DeploymentConfig.kind`와 이를 이용한 Registry
검증, 상세 경로 종류 검증 및 실행 요청의 역할 검증은 그대로 유지한다.

카탈로그는 사용 가능 항목만 추천하는 사용자용 목록이 아니라 현재 운영 Registry
상태를 보여 주는 API다. 따라서 `enabled=false`인 Deployment도 숨기지 않고
`enabled: false`로 반환한다. Gateway는 인증된 사용자의 접근 정책과 `enabled`
상태를 기준으로 실제 선택 가능한 항목만 필터링해 Frontend에 제공한다. LPL API
자체는 Gateway 뒤의 신뢰 가능한 내부망으로 제한하며, Gateway는 상세 응답의
`baseUrl`, `timeoutMs`와 LLM의 `adapterType`, `modelName`을 Frontend로 전달하지
않는다.

목록에는 `adapterType`, `baseUrl`, `modelName`, `timeoutMs`를 포함하지 않는다.
Gateway용 NER 상세에는 `baseUrl`, `timeoutMs`를 항상 포함하고, LLM 상세에는
`adapterType`을 항상 포함하며 `baseUrl`, `modelName`, `timeoutMs`는 Registry에
설정된 경우에만 포함한다. URL의 IP나 호스트를 별도
필드로 중복하지 않고 `baseUrl`의 host 부분으로 확인한다. Secret은 상세에서도
반환하지 않는다. 목록 응답은 Deployment ID와 활성 상태만
제공하며 내부 `modelName`을 목록 필드로 투영하지 않는다.

모든 `baseUrl`은 Active Snapshot 활성화 전에 공통 검증한다. NER `baseUrl`은
호출 경로까지 포함한 전체 POST Endpoint이며 path를 허용한다. query, fragment 또는
사용자명·비밀번호 형태의 URL userinfo가 포함된 설정은 거부한다. Token이나 Secret을
URL 또는 Registry 평문에 저장하지 않는다.

### 8.3 Deployment 추가·수정·활성 상태 변경·삭제

Deployment 관리 API는 종류별 경로만 제공한다.

```text
POST /deployments/ner
PUT  /deployments/ner/{deployment_id}
PATCH /deployments/ner/{deployment_id}/enabled
DELETE /deployments/ner/{deployment_id}
POST /deployments/llm
PUT  /deployments/llm/{deployment_id}
PATCH /deployments/llm/{deployment_id}/enabled
DELETE /deployments/llm/{deployment_id}
```

NER `POST` 요청 본문은 필수 `deploymentId`, `baseUrl`, `timeoutMs`, `enabled`를
받고 `PUT`은 `deploymentId`를 제외한 나머지 세 필드를 받는다. NER 요청에는
`adapterType`과 `modelName`을 허용하지 않는다. LLM `POST`는 필수
`deploymentId`, `adapterType`, `enabled`와 Adapter 계약에 필요한 선택적
`baseUrl`, `modelName`, `timeoutMs`를 받고, LLM `PUT`은 ID를 제외한 같은 전체
설정을 받는다. 두 종류 모두 경로가 `kind`를 결정하므로 본문에 `kind`를 받지
않고, `PUT`은 일부 필드만 수정하는 Patch가 아니라 기존 설정 전체를 교체한다.
`PATCH .../enabled`는 다음 본문만 허용하며 기존 실행 설정은 그대로 보존한다.

```json
{
  "enabled": true
}
```

성공하면 `200 OK`와 변경된 `DeploymentDetail`을 반환한다. 같은 값을 반복해서
보내도 성공하는 멱등 연산이며, 이때 Snapshot Reload의 `NO_CHANGE`도 정상으로
처리한다.
관리 API의 외부 JSON 필드명은 camelCase만 허용한다. `deployment_id`,
`adapter_type`, `base_url` 같은 Python snake_case 이름은
`422 REQUEST_VALIDATION_FAILED`로 거부한다.

`DELETE`는 Request Body 없이 현재 `enabled=false`인 Deployment만 제거한다.
성공하면 본문 없는 `204 No Content`를 반환한다. 활성 Deployment 삭제는
`409 DEPLOYMENT_MUST_BE_DISABLED`, 존재하지 않거나 경로 종류와 다른 대상은
`404 DEPLOYMENT_NOT_FOUND`로 거부한다.

추가·수정·활성 상태 변경·삭제 흐름은 다음과 같다.

```text
종류별 POST, PUT, PATCH .../enabled 또는 DELETE
→ 경로의 kind와 요청 설정 결합, 기존 enabled 교체 또는 비활성 항목 제거
→ Pydantic 구조 및 Adapter 계약 검증
→ DeploymentManagementService가 변경 작업 직렬화
→ RegistryFileStore 트랜잭션에서 RegistryMutator 적용
→ RegistryFileStore가 현재 두 파일을 병합해 검증
→ NER 요청은 ner_deployments.json만,
   LLM 요청은 llm_deployments.json만 임시 파일을 거쳐 원자 교체
→ RegistryManager.try_reload()
→ 새 병합 Snapshot 전체 검증
→ APPLIED 또는 NO_CHANGE이면 쓰기 결과 반환
   (POST·PUT·PATCH는 DeploymentDetail, DELETE는 본문 없는 204)
```

후보 Snapshot이 거부되거나 Reload 자체가 실패하면 Active Snapshot은 이전 정상
상태를 계속 제공한다. `DeploymentManagementService`는 저장 전 Registry를 사용해
변경한 종류 파일을 이전 내용으로 원자 복원하고 다시 Reload한다. 파일과 Snapshot
복원이 성공하면 `503 DEPLOYMENT_ACTIVATION_FAILED`, 복원까지 실패하면
`500 DEPLOYMENT_ROLLBACK_FAILED`를 반환한다. 중복 ID 추가는
`409 DEPLOYMENT_ALREADY_EXISTS`, 존재하지 않거나 경로 종류가 다른 대상의 수정과
활성 상태 변경·삭제는 `404 DEPLOYMENT_NOT_FOUND`, 활성 Deployment 삭제는
`409 DEPLOYMENT_MUST_BE_DISABLED`, 잘못된 ID·PATCH 본문·구조 또는 Adapter 계약
오류는 `422`로 거부한다. Registry 미초기화나 후보 활성화 실패는 `503`, 저장 또는
복원 실패는 `500`으로 반환한다.

새 Endpoint를 안전하게 등록하고 활성화하는 권장 흐름은 다음과 같다.

```text
enabled=false로 POST 등록
→ POST /deployments/{kind}/{deployment_id}/probe로 실제 연결 확인
→ 성공하면 PATCH /deployments/{kind}/{deployment_id}/enabled에 {"enabled": true}
→ 원자 저장과 Snapshot Reload가 성공한 뒤 일반 실행 요청에서 사용
```

Deployment 삭제의 권장 흐름은 다음과 같다.

```text
PATCH /deployments/{kind}/{deployment_id}/enabled에 {"enabled": false}
→ 비활성 상태의 원자 저장과 Snapshot Reload 성공 확인
→ DELETE /deployments/{kind}/{deployment_id}
→ 종류별 파일에서 원자 삭제하고 Snapshot Reload
→ 성공 시 204 No Content
```

삭제 요청 자체는 Deployment를 자동으로 비활성화하지 않는다. 활성 항목을 바로
삭제하면 `409 DEPLOYMENT_MUST_BE_DISABLED`를 반환한다.

`OfflineRegistryEditor`, `add_deployment_offline()`과
`update_deployment_offline()`은 LPL 프로세스를 완전히 중지한 상태에서만 사용한다.
이 경로는 파일만 원자 저장하며 실행 중 Active Snapshot을 바꾸거나 활성화 실패를
복원하지 않는다. Runtime이나 HTTP Route에서 Offline Editor를 조립·호출하지 않는다.

JSON 저장소의 Lock과 Active Snapshot은 프로세스 로컬이므로 Deployment 관리 API를
사용하는 현재 배포는 Uvicorn worker를 하나만 실행한다. 컨테이너 교체 뒤에도 관리
API 변경을 유지하려면 `config`를 쓰기 가능한 영속 볼륨으로 마운트하고 기본
컨테이너 사용자 UID/GID `10001:10001`에 쓰기 권한을 부여한다.
기존 Docker named volume `lpl-config`는 새 이미지의 번들 `config` 디렉터리를
가린다. `/mask` 기능이 포함된 이미지로 업그레이드할 때 기존 볼륨에
`mask_prompt.j2`가 없으면 첫 Snapshot build가 fail-closed로 실패한다. 운영 중
Deployment JSON을 먼저 보존한 뒤 새 Prompt 파일을 볼륨에 추가하거나 안전하게
볼륨을 재구성해야 하며, 누락된 Prompt를 선택적으로 무시하고 시작하지 않는다.

---

## 9. 모델·Endpoint 동적 선택과 고정 Prompt 사용 방식

Runtime 요청은 모델 URL, API Key나 Jinja2 템플릿 원문을 직접 전달하지 않는다.
Gateway는 요청마다 Registry에 등록된 Deployment ID를 전달한다. ID가 누락되거나
`null`이면 API 요청 검증 오류를 반환하고, 존재하지 않거나 비활성화되었거나 역할과
`kind`가 맞지 않으면 명시적인 Deployment 해석 오류를 반환한다.
이 일반 Runtime 활성 상태 제한은 관리용 Deployment Probe에는 적용하지 않는다.
LPL API는 Gateway 뒤의 내부 서비스로 제한하고, 사용자별 Deployment 허용 정책과
인증은 Gateway가 강제한다.

LPL의 실행 흐름은 다음과 같다.

```text
1. 원문 text, 필수 nerDeploymentId·llmDeploymentId·sourceType, 선택적 organizationProfile과 regexCandidates 수신
2. 현재 전역 PolicySettings Snapshot을 한 번 캡처
3. Active Registry Snapshot을 요청당 한 번 캡처
4. DeploymentResolver가 같은 Snapshot에서 NER·LLM과 Prompt를 `DetectionExecutionPlan`으로 조립
5. Plan의 NER Deployment `baseUrl` 전체 Endpoint로 POST 요청을 보내 개체 탐지
6. 조직 Context와 sourceType을 검증하고 RegexCandidate에서 type·source를 파생한 뒤 비활성 NER 결과를 제외한 활성 근거 JSON Context 조립
7. Plan의 `detection_prompt` Artifact를 기존 탐지 근거와 함께 렌더링
8. Plan의 Detection LLM Deployment와 adapterType에 맞는 LLM Adapter 선택
9. Local LLM이 전체 Regex·NER 후보를 판정하고 활성 정책 안에서 누락·문맥형 기밀정보를 추가 탐지
10. 후보 ID 완전성, 신규 NER·LLM 결과를 검증하고 중복을 정리해 공통 결과 형식으로 정규화
```

`regexCandidates`는 생략하면 빈 목록으로 처리하며, Gateway가 찾았지만 아직
확정하지 않은 후보를 `candidateId`, span, text, policyId, detailType, score로
전달한다. 요청 모델은 후보 ID 고유성, 범위와 문자열이 `text`의 실제 구간과
일치하는지 검증한다. 후보는 type이나 source를 받지 않으며 LPL이 policyId에서
type을 파생하고 내부 `source=regex` Detection으로 변환한다. LPL이 새로 생성하는
결과는 계속 `ner` 또는 `llm` source를 사용한다.

Option B 마스킹은 탐지와 별도 실행 계획과 Pipeline을 사용한다.

후보 판정 계약의 `UNCERTAIN`은 `/detect`에서 상태 그대로 반환한다. LPL은
`UNCERTAIN` 후보를 자동으로 확정하거나 `finalDetections`와 `maskedText`를 만들지
않는다. Gateway와 Frontend가 사용자 검토 결과를 반영해 확정한 Detection만 기존
`POST /mask`에 전달한다. 따라서 전역 `uncertainAction` 설정은 현재 범위에 두지
않으며, 검토 없이 외부 Provider로 요청을 진행해서도 안 된다.

```text
1. Gateway가 CONFIRMED Regex와 /detect의 CONFIRMED NER·신규 LLM 결과를 전체 병합
2. POST /mask에서 필수 text, llmDeploymentId, detections 수신
3. 요청 시작 시 Active Registry Snapshot 캡처
4. DeploymentResolver가 kind=llm, 활성 상태와 mask_prompt를
   MaskingExecutionPlan으로 조립
5. 입력 Detection의 원문 span·text·source를 검증
6. 겹치는 span만 최대 합집합 component로 병합하고 types·sources 근거를 계산
7. detections가 비었으면 검증된 Plan을 유지하되 Backend·namespace 생성 없이
   maskedText=text, replacements=[] 반환
8. config/mask_prompt.j2를 정적 system 메시지로 렌더링
9. 검증된 component와 서버 생성 namespace를 canonical JSON user 메시지로 전달
10. Local LLM이 maskedText와 targetId별 entityId assignment를 구조화 출력
11. LPL이 start/end/types/sources와 entityId/placeholder를 안전하게 파생
12. 모든 Detection span coverage, 비탐지 원문 불변과 maskedText 재구성을
    fail-closed로 검증
13. {"maskedText":"...","replacements":[...]} 반환
```

`/mask`는 새 민감정보를 탐지하지 않는다. `detections` 밖 원문은 반드시 그대로
유지하므로 Gateway가 전체 탐지 결과를 빠짐없이 전달해야 한다. `entityId`와
placeholder는 요청 범위에서만 일관되고 LPL은 매핑을 저장하지 않는다.
LLM assignment는 원문 순서의 `target-1`, `target-2`, ...를 정확히 한 번씩 같은
순서로 포함하고, 새 entity는 첫 등장 순서대로 `entity-1`, `entity-2`, ...를
사용한다. placeholder는 서버가 만든 16자리 소문자 16진수 namespace와 entity 첫
등장 순번으로 `[[LPL_<namespace>_<최소 4자리 순번>]]` 형식으로 생성한다.
`types`는 중복 제거 후 사전순, `sources`는 `regex`, `ner`, `llm` 순으로 LPL이
입력 Detection에서 파생한다.

Option A 생성도 같은 방식으로 동작한다.

```text
1. POST /generate에서 필수 text·llmDeploymentId와 선택적 previousText 수신
2. 요청 시작 시 Active Registry Snapshot 캡처
3. DeploymentResolver가 kind=llm과 활성 상태를 검증
4. LlmLimitsService가 Registry와 선택적 Ollama Native API로 컨텍스트 한도 조회
5. previousText의 user·assistant 역할을 보존하고 현재 text를 마지막 user 메시지로 구성한 뒤 입력 상한 검사
6. BackendProviderRegistry에서 해당 Adapter를 선택해 Local LLM 호출
7. 공통 LlmResult 형식으로 반환
```

Generation Pipeline은 Prompt 구성요소나 Jinja2 템플릿을 사용하지 않는다.
`previousText`의 각 객체는 `user` 또는 `assistant` 메시지로 순서대로, 현재 `text`는
마지막 `user` 메시지로 Backend에 전달한다. `system` 역할은 허용하지 않는다.
컨텍스트 한도 API는 조회만 담당하며 Generation Pipeline이 입력을 근사 계산하거나
사전 차단하지 않는다.

대화 제목 생성은 별도 실행 계획과 Pipeline을 사용한다.

```text
1. Gateway가 첫 사용자 메시지 후 POST /titles 호출
2. 필수 text와 llmDeploymentId 수신
3. 요청 시작 시 Active Registry Snapshot 캡처
4. DeploymentResolver가 kind=llm, 활성 상태와 title_prompt를
   TitleGenerationExecutionPlan으로 조립
5. config/title_prompt.j2를 정적 system 메시지로 렌더링
6. text를 별도 user 메시지로 구성하고 max_tokens=32로 Local LLM 호출
7. 모델 출력을 한 줄, 1~30자, UTF-8 120 bytes 이하의 일반 텍스트로 검증
8. {"title": "..."} 반환
9. Gateway가 title을 대화방 metadata로 영구 저장
```

LPL은 대화 ID, 대화 이력이나 생성된 제목을 저장하지 않는다. 첫 메시지 선택,
호출 시점, 재시도, 대체 제목 및 제목 저장 정책은 Gateway가 관리한다.

### 9.1 요청에 따른 변경 예시

```text
동일한 입력 + nerDeploymentId=ner-a + llmDeploymentId=llm-a
→ Entity Detection NER A + LLM A + prompts.j2

동일한 입력 + nerDeploymentId=ner-b + llmDeploymentId=llm-b
→ Entity Detection NER B + LLM B + prompts.j2
```

모델이나 Endpoint를 바꾸기 위해 파이프라인 코드를 수정하지 않고 요청의 Deployment
ID를 변경한다. `RegistryManager` Reload는 Deployment 설정 갱신을 위한 기능이며,
고정 탐지·마스킹·제목 Prompt를 운영 중 교체하는 수단이 아니다.

### 9.2 Endpoint 변경 원칙

- 일반 Runtime 요청에서 임의 Endpoint를 받지 않는다.
- Endpoint는 Deployment로 미리 등록한다.
- 요청은 역할별 Deployment ID를 기준으로 실행한다.
- 동일 모델의 새 Endpoint는 새 Deployment ID로 등록할 수 있다.
- Gateway가 요청에 넣는 Deployment ID를 변경하여 트래픽을 전환한다.
- 원문을 받는 Deployment는 신뢰 가능한 로컬 영역이어야 한다.

---

## 10. 탐지 파이프라인의 개념적 흐름

```text
Gateway의 Regex 탐지 결과
        │
        ▼
NER 개체 탐지 실행
        │
        ▼
Regex + 개체 탐지 결과를 후보 Context로 구성
        │
        ▼
Local LLM 후보 판정 + 누락·문맥형 정보 추가 탐지
        │
        ▼
모델 출력 검증 및 정규화
        │
        ▼
Gateway로 후보 판정 + 확정 NER·신규 LLM 결과 반환
```

개체 탐지 단계는 이름, 주소, 회사명 등 개체 기반 정보를 담당하며 전용 NER를
Prompt 없이 항상 먼저 실행한다. 이후 Local LLM은 Regex와 NER 결과를 근거로 받아
각 후보를 `CONFIRMED`, `REJECTED`, `UNCERTAIN`으로 판정하고 NER가 놓친 민감정보와
문맥형 기밀정보도 추가 탐지한다.

실제 정책은 `P01~P08`, `S01~S03`, `B01~B03`의 14개다. `policyId`와 `type`은
고정 1:1 매핑이며 모든 공통 Detection에 둘 다 포함한다. NER는 `P01`
`PERSONAL_IDENTITY`와 `P04` `LOCATION`을 주로 담당하고, Regex는 `P02`, `P03`,
`P05` 후보를 주로 제공한다. Local LLM은 누락 보완과 나머지 문맥형 정책을 담당한다.
카테고리는 개인정보 `PRIVATE`, 인증보안 `SECURITY`, 기업 내부정보
`INTERNAL_INFO`이며 `B01`은 `PERSONAL` 인사·인력 운영정보, `B02`는 `CLIENT`
고객·협력사 기밀정보, `B03`은 `R&D` R&D·영업비밀·기술정보로 고정한다.

Local LLM은 다음과 같이 문맥 판단이 필요한 정보를 담당한다.

- 계약 금액
- 내부 프로젝트 정보
- 비공개 일정
- 영업 기밀
- 문장 전체 맥락을 고려해야 하는 민감정보

Local LLM은 Regex와 개체 탐지 단계에서 이미 탐지한 영역을 참고하여 중복 탐지를 줄인다.

모델 출력은 항상 비신뢰 입력으로 취급한다. LPL 내부 공통 형식으로 사용하기 전에 최소한 다음을 확인한다.

```text
탐지 유형이 허용된 값인지
LLM 후보 text가 실제 원문에 존재하는지
서버가 계산한 start/end 범위와 해당 구간이 실제 원문과 일치하는지
중복되거나 충돌하는 결과가 있는지
구조화 출력 형식이 유효한지
```

NER Backend 또는 LLM Span Resolver가 만든 Detection batch는
`DetectionResultValidator`를 통해 먼저
전체 Span을 fail-closed로 검증한 뒤 중복·겹침 정책을 적용한다. 의미 중복 키는
`(start, end, type)`이며 `regex > ner > llm`, 같은 source에서는 높은 score 순으로
하나를 남긴다. 범위나 type이 다른 겹침은 민감 구간 또는 탐지 근거의 손실을 막기
위해 모두 보존하고 결정적으로 정렬한다. Option B에서 Gateway는 Regex와 신규
NER·LLM Detection을 모두 `/mask`에 전달한다. Masking Pipeline은 Detection 원본을
변경하지 않고 겹치는 span의 비중첩 합집합 component를 별도로 계산하며, 모든
component가 정확히 한 번 치환되고 입력 Detection 밖 원문이 변하지 않는지
fail-closed로 검증한다.

LLM 탐지 출력은 설명이나 Markdown 코드 블록이 없는 순수 JSON 객체여야 한다.
최상위에는 `candidateDecisions`와 `newDetections`만 둔다. 후보 판정은 입력의 모든
candidateId와 `CONFIRMED | REJECTED | UNCERTAIN`만 출력하고, 신규 탐지는 `text`,
`type`, `score`만 출력한다. LLM은 후보 원문·정책·문자 위치를 다시 만들지 않는다.
`LlmDetectionOutputParser`가 판정과 좌표 없는 신규 후보를 검증하고,
`LlmDetectionSpanResolver`는 반복 가능한 식별자·연락처형 타입이면 후보 text의
모든 정확한 원문 출현 위치를 찾고, 문맥형 타입이면 text가 정확히 한 위치에만
나타날 때 허용한다.
서버가 `start`, `end`, `source="llm"`을 설정한다. 입력 후보나 신규 결과가 없으면
각 배열을 `[]`로 출력한다.

Parser는 잘못된 JSON, 중복 키, 비표준 숫자, 필드 누락·추가, 잘못된 자료형과
후보 제약 위반, candidateId 누락·중복·위조를 거부한다. 또한 UTF-8 출력 바이트
수와 후보 판정·신규 탐지 수에 상한을 적용하며, 오류 객체나 메시지에 LLM 출력 원문을
보관하지 않는다. 원문에 없는
후보 문자열, 문맥형 반복 문자열과 좌표 확장 상한 초과는 fail-closed로 거부하고,
계산된 범위와 `text` 일치 여부는 이후 Span Validator가 다시 검증한다.
탐지 LLM은 `temperature=0`, `reasoning_effort="none"`으로 호출해 분류 결과 전에
장문의 Thinking 토큰을 생성하지 않도록 한다.

---

## 11. 주요 설계 원칙

1. **Gateway는 요청 흐름을 관리하고, LPL은 모델 실행을 관리한다.**
2. **파이프라인은 특정 모델이나 Endpoint에 직접 의존하지 않는다.**
3. **모델별 차이는 Adapter가 흡수한다.**
4. **사용 가능한 모델과 Endpoint는 Deployment로 등록하고, NER·LLM 설정은 종류별 파일에 저장하되 병합된 하나의 Snapshot으로 실행한다.**
5. **탐지·마스킹·생성·제목 생성 요청은 역할별 Deployment ID를 직접 선택하고 URL이나 Secret을 받지 않는다.**
6. **Snapshot은 Artifact의 저장·수명, DeploymentResolver는 같은 Snapshot의 실행 리소스를 담은 역할별 Plan 조립, Pipeline은 Plan 소비를 담당한다.**
7. **탐지는 NER·LLM ID, 마스킹·생성·제목 생성은 LLM ID를 필수로 사용하며 조합·할당 정책은 Gateway가 관리한다.**
8. **탐지·정책·마스킹·제목 프롬프트는 `config/prompts.j2`, `config/policy_prompts.json`, `config/mask_prompt.j2`, `config/title_prompt.j2`의 읽기 전용 고정 파일로 배포하며 ID·요청값으로 선택하거나 앱에서 편집하지 않는다.**
9. **모델은 요청마다 로딩하지 않고 미리 준비된 Backend를 사용한다.**
10. **LPL은 가능한 한 Stateless하게 유지하며 대화방, 생성된 제목과 마스킹 entity 매핑을 저장하지 않는다.**
11. **모든 Backend 결과는 공통 Detection, Masking, Generation 또는 제목 출력 계약으로 정규화한다.**
12. **원문을 처리하는 Endpoint는 신뢰 가능한 로컬 환경으로 제한한다.**
13. **새 모델을 추가할 때 기존 파이프라인보다 Adapter와 Registry를 확장한다.**
14. **Deployment 추가·전체 수정·활성 상태 변경·삭제는 해당 종류 파일의 원자 저장과 Snapshot Reload를 함께 완료하며, 실패하면 이전 정상 설정으로 복원한다.**
15. **Deployment 삭제는 비활성 항목만 허용하며 활성 항목은 먼저 PATCH로 비활성화한다.**
16. **온라인 Deployment 변경은 `DeploymentManagementService`만 소유하며, `OfflineRegistryEditor`는 중지 상태의 파일 정비에만 사용한다.**
17. **Adapter 목록 API는 설정 계약과 실제 Provider가 모두 등록된 실행 가능한 `adapterType` 문자열만 공개하며, Endpoint 연결 성공이나 설정·Provider 구현 세부정보를 노출하지 않는다.**
18. **`/ui/`는 같은 origin의 LPL API를 실제 호출하는 내부 기능 테스트 도구이며, 운영 Frontend나 인증·권한 경계를 대신하지 않는다.**

---

## 12. 권장 코드 구조

```text
lpl-service/
├─ app/
│  ├─ main.py
│  ├─ frontend/
│  │  ├─ index.html
│  │  ├─ styles.css
│  │  └─ app.js
│  ├─ api/
│  │  ├─ dependencies.py
│  │  ├─ adapters.py
│  │  ├─ deployments.py
│  │  ├─ detection.py
│  │  ├─ error_handlers.py
│  │  ├─ generation.py
│  │  ├─ masking.py
│  │  └─ title_generation.py
│  ├─ schemas/
│  │  ├─ adapters.py
│  │  ├─ detection.py
│  │  ├─ deployments.py
│  │  ├─ generation.py
│  │  ├─ masking.py
│  │  ├─ registry.py
│  │  └─ title_generation.py
│  ├─ services/
│  │  ├─ adapter_catalog.py
│  │  ├─ deployment_catalog.py
│  │  ├─ deployment_management.py
│  │  ├─ deployment_probe.py
│  │  ├─ detection_pipeline.py
│  │  ├─ generation_pipeline.py
│  │  ├─ llm_detection_output_parser.py
│  │  ├─ llm_detection_span_resolver.py
│  │  ├─ llm_masking_output_parser.py
│  │  ├─ llm_masking_output_validator.py
│  │  ├─ llm_result_validator.py
│  │  ├─ masking_pipeline.py
│  │  ├─ title_generation_pipeline.py
│  │  └─ title_output_validator.py
│  ├─ registry/
│  │  ├─ store.py
│  │  ├─ file_store.py
│  │  ├─ offline_editor.py
│  │  ├─ mutator.py
│  │  ├─ validator.py
│  │  ├─ snapshot.py
│  │  ├─ snapshot_builder.py
│  │  ├─ manager.py
│  │  ├─ execution_plan.py
│  │  └─ deployment_resolver.py
│  ├─ backends/
│  │  ├─ backend_registry.py
│  │  ├─ errors.py
│  │  ├─ provider_registry.py
│  │  ├─ ner/
│  │  │  ├─ base.py
│  │  │  └─ http.py                 고정 표준 NER HTTP Backend
│  │  └─ llm/
│  │     ├─ base.py
│  │     ├─ mock.py
│  │     ├─ openai_compatible.py
│  │     └─ custom_http.py
│  ├─ prompts/
│  │  ├─ __init__.py
│  │  ├─ prompt_artifact.py
│  │  ├─ prompt_errors.py
│  │  ├─ prompt_limits.py
│  │  ├─ prompt_loader.py
│  │  ├─ prompt_renderer.py
│  │  ├─ mask_prompt_artifact.py
│  │  └─ title_prompt_artifact.py
│  ├─ policies/
│  │  ├─ detection_result_validator.py
│  │  ├─ mask_target_resolver.py
│  │  ├─ span_validator.py
│  │  ├─ overlap_resolver.py
│  │  └─ trust_policy.py
│  └─ core/
│     ├─ application_runtime.py
│     ├─ json_codec.py
│     ├─ json_value.py
│     └─ registry_file_coordinator.py
├─ config/
│  ├─ ner_deployments.json
│  ├─ llm_deployments.json
│  ├─ prompts.j2
│  ├─ policy_prompts.json
│  ├─ mask_prompt.j2
│  └─ title_prompt.j2
├─ services/
│  └─ gliner_ner/
│     ├─ Dockerfile
│     ├─ requirements-torch.txt
│     ├─ requirements.txt
│     └─ gliner_ner_server/
├─ docs/
│  ├─ frontend.md
│  ├─ masking.md
│  └─ ner_server.md
└─ tests/
```

이 구조의 핵심은 API, 파이프라인, Registry, 모델 Backend, 프롬프트를 서로 분리하는 것이다. 에이전트는 새 기능을 추가할 때 이 경계를 유지해야 한다.
