# Backend 실행 구조

## 1. 목적

Backend 모듈은 NER 표준 HTTP 서버와 여러 LLM 구현체를 공통 인터페이스 뒤에 둔다.
NER는 사용자 선택 없이 단일 표준 HTTP Backend를 사용하고, LLM만 Deployment의
`adapterType`에 맞는 실제 구현체를 선택한다.

Registry 설정과 런타임 구현체는 서로 다른 두 Registry가 관리한다.

```text
BackendRegistry
└─ Deployment 설정 계약
   ├─ NER 고정 HTTP 설정
   ├─ 등록된 LLM adapterType
   ├─ 필수·금지 필드
   └─ 추가 설정 검증

BackendProviderRegistry
└─ 실제 실행 구현체
   ├─ NerBackend
   └─ LlmBackend
```

새 LLM Adapter를 실행하려면 같은 `(llm, adapterType)`으로 다음 두 항목을 모두 등록해야 한다.

1. `BackendRegistry`에 Deployment 설정 계약 등록
2. `BackendProviderRegistry`에 실제 Backend 인스턴스 등록

설정 계약만 등록하면 JSON을 검증할 수 있지만 모델을 실행할 수 없고, Provider만 등록하면 해당 구현체가 어떤 Deployment 설정을 받는지 보장할 수 없다.

Registry 설정과 Snapshot 흐름은 [Registry 모듈 구조](./registry.md), JSON 필드는
[Registry 설정 및 파일 형식](./registry_configuration.md)에서 설명한다.

## 2. 코드 구조

```text
app/
├─ backends/
│  ├─ __init__.py
│  ├─ backend_registry.py
│  ├─ errors.py
│  ├─ provider_registry.py
│  ├─ validation.py
│  ├─ ner/
│  │  ├─ __init__.py
│  │  ├─ base.py
│  │  ├─ http.py
│  └─ llm/
│     ├─ __init__.py
│     ├─ base.py
│     ├─ input_validation.py
│     ├─ mock.py
│     └─ openai_compatible.py
├─ services/
│  ├─ __init__.py
│  ├─ detection_pipeline.py
│  ├─ generation_pipeline.py
│  ├─ llm_masking_output_parser.py
│  ├─ llm_masking_output_validator.py
│  ├─ masking_pipeline.py
│  └─ title_generation_pipeline.py
└─ schemas/
   ├─ detection.py
   ├─ generation.py
   ├─ masking.py
   └─ registry.py
```

현재 기본 Runtime에는 고정 공통 HTTP NER와 개발용 Mock·OpenAI Chat Completions
호환 LLM 구현체가 포함되어 있다. 같은 LLM 인터페이스를 Detection, Masking,
Generation과 Title Pipeline이 사용한다. Ollama Native LLM은 아직 기본 구현체로
포함되어 있지 않으며, 아래 예시는 LLM 인터페이스에 맞춰 추가하는 방법을 보여준다.

## 3. 공통 Backend 인터페이스

### 3.1 NER Backend

`app/backends/ner/base.py`의 `NerBackend`는 runtime-checkable Protocol이다.

```python
class NerBackend(Protocol):
    async def detect(
        self,
        text: str,
        deployment: DeploymentConfig,
    ) -> list[Detection]:
        ...
```

구현체는 모델 고유 출력을 `Detection` 목록으로 정규화해야 한다. 반환 타입 선언을
신뢰 경계로 간주하지 않으며, Detection Pipeline은 각 Backend batch를
`DetectionResultValidator`에 전달해 필드, 원문 span, source와 개수 제한을 다시
검증한 뒤 중복·겹침 정책을 적용한다.

```text
Detection
├─ start
├─ end
├─ text
├─ type
├─ source       "ner" 또는 "llm"
└─ score        0.0~1.0
```

NER 구현체는 최소한 다음을 보장해야 한다.

- `start < end`이며 구간이 원문 범위 안에 있음
- `text == original_text[start:end]`
- 모델 label을 LPL이 사용할 개체 유형으로 변환
- score를 0.0~1.0 범위로 정규화
- 모델 SDK 또는 서버 고유 객체를 Pipeline 밖으로 노출하지 않음

NER 관리 API와 물리 Registry에는 `adapterType`이 없다. 파일을 읽을 때만 내부
호환 키 `http_ner`가 주입되며, 사용자는 Mock이나 모델별 NER Adapter를 선택할 수
없다. 테스트 대역은 Provider 또는 Pipeline 의존성 주입으로 사용한다.

### 3.2 LLM Backend

`app/backends/llm/base.py`의 `LlmBackend`도 runtime-checkable Protocol이다.

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

현재 메시지, 파라미터와 출력 Schema는 열린 `dict` 계약이다. Adapter 구현체가 공통 입력을 서버 고유 요청 형식으로 변환하고 결과를 `LlmResult`로 정규화한다.

`input_validation.py`는 모든 LLM Backend가 공유하는 입력 컨테이너 구조를 검증하고,
`normalize_json_value()`를 통해 Backend가 사용할 깊은 복사본을 만든다. JSON 문법과
직렬화 규칙은 `app/core/json_codec.py`가 통일하며, Adapter 전용 예약 필드와 HTTP
요청·응답의 도메인 구조 해석은 각 구현체가 담당한다. `validation.py`는 Backend를
직접 호출할 때 중복되던 Deployment의 `kind`, 내부 Backend 키와 `enabled` 검증을
NER·LLM 구현체에 제공한다. NER의 내부 키는 외부 계약이 아니다.

`app/schemas/generation.py`의 공통 결과:

```text
LlmResult
├─ text
├─ modelName       선택
├─ finishReason    선택
└─ usage           선택
   ├─ inputTokens
   ├─ outputTokens
   └─ totalTokens
```

`LlmResult`와 `LlmTokenUsage`는 알 수 없는 필드를 거부하고, frozen·strict Pydantic 모델로 동작한다. 토큰 수는 0 이상의 정수다.

`app/services/llm_result_validator.py`는 Backend가 반환한 객체를 그대로 신뢰하지
않는다. 정확한 `LlmResult` 타입인지 확인한 뒤 `text`, 선택적 모델 정보와 중첩
Usage를 Plain Data로 다시 조립해 Pydantic 계약과 UTF-8을 재검증한다. 따라서
`model_construct()`로 필드 검증을 우회한 객체도 거부한다. 이 경계는 Detection,
Masking, Generation Pipeline과 LLM Deployment Probe가 함께 사용한다.

`app/backends/llm/mock.py`의 `MockLlmBackend`는 모델 서버 없이 Pipeline과 API를 조립해 테스트하기 위한 인메모리 구현체다. 기본적으로 `text="[]"`, `modelName="mock-llm"`, `finishReason="stop"`인 `LlmResult`를 반환하며, 생성자에 별도의 frozen `LlmResult`를 주입해 원하는 응답을 고정할 수 있다. 호출 메시지, 파라미터, 출력 Schema와 Deployment는 기록하지 않는다.

기본 `text="[]"`는 Detection LLM의 빈 출력에는 유효하지만 `/mask`의
`{"maskedText": ..., "assignments": [...]}` 계약에는 유효하지 않다. Masking
Pipeline 테스트에는 해당 요청의 target과 namespace에 맞춘 `LlmResult`를 명시적으로
주입해야 한다.

`llm/mock` Deployment에는 `baseUrl`, `modelName`, `timeoutMs`를 설정하지 않는다. 이 필드들은 기본 `BackendRegistry` 계약에서 모두 금지되며, Mock은 실제 추론이나 개인정보·기밀정보 탐지 용도로 사용하면 안 된다.

## 4. BackendProviderRegistry

`app/backends/provider_registry.py`는 프로세스 수명 동안 재사용할 실제 Backend 인스턴스를 불변 Mapping으로 보관한다.

```python
registration = BackendProviderRegistration(
    kind="llm",
    adapter_type="ollama_native",
    provider=ollama_backend,
)

providers = BackendProviderRegistry([registration])
```

등록 키는 다음 조합이다.

```text
(kind, adapterType)
```

동일한 `adapterType`을 NER와 LLM 양쪽에 등록할 수 있지만, 동일한 `(kind, adapterType)`은 한 번만 등록할 수 있다.

`BackendProviderRegistration`은 등록 시 최소 실행 인터페이스를 확인한다.

| kind | 필요한 callable |
|---|---|
| `ner` | `detect` |
| `llm` | `generate` |

등록 단계에서는 메서드 존재 여부만 확인한다. 정확한 인자·반환 타입은 Protocol 기반 정적 검사와 구현체 테스트로 보장해야 하며, 결과는 `Detection` 또는 `LlmResult`로 검증해야 한다.

### 4.1 조회

Pipeline은 요청별 실행 계획의 Deployment를 사용해 종류별 메서드로 구현체를 조회한다.

```python
ner_backend = providers.require_ner(
    deployment_id=plan.ner_deployment.id,
    deployment=plan.ner_deployment.config,
)

llm_backend = providers.require_llm(
    deployment_id=plan.llm_deployment.id,
    deployment=plan.llm_deployment.config,
)
```

`require_ner()`와 `require_llm()`은 등록할 때 전달한 동일한 Provider 인스턴스를 반환한다. 요청마다 Backend나 모델을 새로 생성하지 않는다.

### 4.2 설정 계약 연결

Provider Registry에 등록된 모든 키는 대응하는 `BackendRegistry` 설정 계약을 가져야 한다.

```python
providers.validate_contracts(backend_registry)
```

`RegistrySnapshotBuilder`에 `backend_providers`를 전달하면 생성자에서 이 검사를 수행한다.

```python
builder = RegistrySnapshotBuilder(
    store,
    backend_providers=providers,
)
```

### 4.3 Runtime Provider coverage와 Probe

Provider-aware Builder는 후보 Snapshot을 만들 때 활성화된 모든 Deployment에 대응하는 Provider가 있는지 검사한다.

```text
RegistryConfig 로드
→ BackendRegistry 설정 계약 검증
→ BackendProviderRegistry 활성 Deployment coverage 검증
→ Prompt 검증·컴파일
→ 후보 Snapshot 생성
```

- `enabled=true`: 일반 Runtime 실행을 위해 대응하는 Provider가 반드시 필요
- `enabled=false`: Provider 없이 설정을 미리 저장할 수 있음
- `/detect`, `/mask`, `/generate`, `/titles`에서 비활성 Deployment를 선택하면
  `DeploymentResolver`가 `DEPLOYMENT_DISABLED`로 실행 전에 거부
- 관리용 Probe는 비활성 Deployment도 허용하며 Provider가 등록되어 있으면 실제
  최소 요청을 실행
- 비활성 Probe 대상의 Provider가 없으면 `503 BACKEND_PROVIDER_NOT_REGISTERED`
  반환

활성 Deployment에 필요한 Provider가 없으면 초기화는 실패한다. Deployment 설정
Reload에서는 후보가 거부되고 현재 last-known-good Snapshot과 generation을
유지한다. 비활성 Deployment의 Provider 미등록은 Snapshot 생성을 막지 않으며,
해당 Deployment를 Probe할 때 503으로 드러난다.

Probe 성공은 Backend의 현재 요청·응답 계약을 통과했다는 뜻일 뿐이다. Probe는
Deployment 설정, `enabled` 값이나 Active Snapshot을 변경하지 않고 자동
활성화하지 않는다.

현재 `backend_providers` 생성자 인자는 선택 사항이다. 전달하지 않으면 이전 설정 전용 흐름과의 호환을 위해 Provider coverage 검사를 건너뛴다. 실제 모델을 실행하는 애플리케이션 조립에서는 반드시 Provider Registry를 주입해야 한다.

Provider Registry 자체는 Active Snapshot 안에 들어가지 않는다. Snapshot은 실행할 Deployment를 고정하고, Pipeline은 애플리케이션이 소유한 동일 Provider Registry에서 구현체를 조회한다.

### 4.4 LLM Adapter 목록 API

현재 LPL 프로세스에서 실행 가능한 LLM Adapter 종류는 다음 읽기 전용 Endpoint로
조회한다.

```text
GET /adapters/llm
```

이 Endpoint는 요청 본문이나 `kind` Query Parameter를 받지 않는다. 애플리케이션이
함께 조립한 `BackendRegistry.registrations`와
`BackendProviderRegistry.registrations`에서 정확히 같은
`(llm, adapterType)` 키가 양쪽에 모두 존재하는 교집합만 반환한다. 설정 계약만
있거나 실제 Provider만 있는 항목은 실행 가능한 Adapter 목록에 포함하지 않는다.
Deployment Registry나 Active Snapshot에서 현재 사용 중인 Adapter를 역산하는
API는 아니다.

응답 예시는 다음과 같다.

```json
{
  "adapters": [
    "mock",
    "openai_compatible"
  ]
}
```

응답은 `adapters` 필드 하나만 가지며 값은 `adapterType` 문자열 배열이다.
종류는 `/llm` 경로로 이미 표현되므로 응답에 `kind`를 중복하지 않는다.
문자열은 `adapterType` 오름차순으로 정렬한다.

목록에 포함된 Adapter는 설정 계약과 호출 가능한 구현체가 현재 프로세스에 모두
등록됐다는 뜻일 뿐 모델 서버가 실행 중인지, 등록된 URL에 연결할 수 있는지,
모델이 로드되는지 또는 응답 계약을 통과하는지는 보장하지 않는다. 실제 Endpoint
연결과 요청·응답 검증은 Deployment를 등록한 뒤
`POST /deployments/llm/{deployment_id}/probe`로 확인한다. NER는 선택 가능한 Adapter가
없으므로 목록 API를 제공하지 않으며, 연결은 등록된 NER Deployment의 Probe로
직접 확인한다.

응답은 문자열 목록 외에 Deployment URL, API Key·Token·Secret, 설정값,
Provider 객체·클래스명, 모듈 경로나 callable을 포함하지 않는다. Adapter별 설정은
Deployment POST·PUT에서 서버의 `BackendRegistration` 계약으로 검증한다.

## 5. 오류

### 5.1 등록 오류

| 오류 | 조건 |
|---|---|
| `DuplicateBackendProviderRegistrationError` | 같은 `(kind, adapterType)`을 중복 등록 |
| `InvalidBackendProviderError` | NER에 callable `detect`, LLM에 callable `generate`가 없음 |
| `BackendProviderContractError` | Provider에 대응하는 `BackendRegistry` 설정 계약이 없음 |
| `ValueError` | 잘못된 `kind` 또는 `adapterType` 형식 |

### 5.2 조회 오류

`BackendProviderLookupError`는 다음 코드를 제공한다.

| 코드 | 조건 |
|---|---|
| `BACKEND_PROVIDER_NOT_REGISTERED` | 해당 `adapterType` Provider가 어느 kind에도 없음 |
| `BACKEND_PROVIDER_KIND_MISMATCH` | Provider가 다른 kind에만 등록됐거나 종류별 조회 메서드와 Deployment kind가 맞지 않음 |

오류 객체에는 `adapter_type`, 요청한 `kind`, 선택적 `deployment_id`와 사용 가능한 `available_kinds`가 포함된다.

Provider Registry는 실제 Backend 실행 중 발생한 HTTP, 모델 또는 출력 변환 오류를
임의로 감싸지 않는다. 각 Adapter가 오류를 다음 공통 Backend 예외 중 하나로
변환해 전파한다.

### 5.3 실행 오류

`app/backends/errors.py`는 Adapter 구현 방식과 무관한 다섯 가지 실행 오류 분류를
정의한다.

| 공통 오류 | 의미 | HTTP 상태 |
|---|---|---:|
| `BackendConfigurationError` | Deployment, Adapter 파라미터 또는 실행 설정이 잘못됨 | 500 |
| `BackendTimeoutError` | 설정된 제한 시간 안에 Backend 실행이 완료되지 않음 | 504 |
| `BackendTransportError` | 연결 실패 등으로 Backend에 요청을 전달하지 못함 | 502 |
| `BackendResponseError` | 비정상 HTTP 상태, 크기 초과, 잘못된 응답 구조 등으로 결과를 해석할 수 없음 | 502 |
| `BackendInputTooLargeError` | 모델이 원문 전체를 자르지 않고 처리할 수 없음 | 413 |

공통 상위 타입은 HTTP 상태와 안전한 메시지를 선택하는 기준이다. 오류의
`code`는 `OPENAI_COMPATIBLE_TIMEOUT` 같이 Adapter가 정한 세부 코드를 그대로
유지한다. 이로써 클라이언트는 공통 HTTP 의미와 Adapter 고유 원인을
모두 확인할 수 있다.

`app/api/error_handlers.py`의 전역 exception handler가 이 공통 상위 타입을
HTTP 응답으로 변환한다. `/detect`, `/mask`와 `/generate` Endpoint는 Adapter 구현체나
Adapter 전용 예외를 import하지 않는다. 새 Adapter는 다섯 공통 타입 중 의미에
맞는 타입을 상속하고 안전한 고유 `code`를 정의하면 되며, API Route나 HTTP
매핑을 수정할 필요가 없다.

## 6. 수명주기

`BackendProviderRegistry`는 Provider 객체의 생성이나 종료를 담당하지 않는다. 등록된 객체 참조를 보관하고 선택하는 역할만 한다.

권장 소유 구조:

```text
FastAPI lifespan / Application Container
├─ 공유 httpx.AsyncClient 생성
├─ Hugging Face 모델 사전 로드
├─ Backend 구현체 생성
├─ BackendProviderRegistry 조립
├─ RegistryManager와 Pipeline 조립
└─ 종료 시 Client와 모델 리소스 정리
```

운영 원칙:

- HTTP Backend는 요청마다 `httpx.AsyncClient`를 만들지 않고 연결 풀을 공유
- Hugging Face 모델은 요청마다 다운로드하거나 로드하지 않고 시작 시 준비
- Provider Registry는 애플리케이션 수명 동안 동일한 인스턴스를 재사용
- Registry Mapping이 불변이라는 사실은 Provider 객체 내부가 thread-safe하다는 뜻이 아님
- 상태를 가진 Provider는 동시 요청 안전성, GPU 접근 직렬화와 종료 방식을 구현체가 책임짐
- Deployment JSON Reload는 Provider 코드나 캐시된 고정 Prompt를 교체하지 않음
- Provider 등록 구성을 바꾸려면 현재는 애플리케이션을 다시 조립하거나 재시작해야 함

현재 `app/main.py`는 FastAPI lifespan에서 `application_runtime()`을 열어 공유
`httpx.AsyncClient`, 기본 Backend Provider Registry, Registry Manager,
Detection, Masking과 Generation Pipeline을 조립한다. 종료 시 AsyncClient
Context를 닫아 연결 풀을 정리한다. HTTP 탐지·마스킹·생성 계약은 각각
[Detection Contract](./detection.md), [Masking API](./masking.md)와
[Generation API](./generation.md)에서
설명한다.

## 7. Mock LLM Backend

`MockLlmBackend`는 기본 애플리케이션 런타임에 `(llm, mock)` Provider로 등록된다.
따라서 테스트 요청은 외부 모델 서버 없이 다음 Deployment ID를 직접 선택할 수 있다.

```json
{
  "llm-mock-a": {
    "adapterType": "mock",
    "enabled": true
  }
}
```

고정 응답이 필요한 단위 테스트에서는 Backend를 직접 구성한다.

```python
from app.backends import LlmResult, MockLlmBackend


backend = MockLlmBackend(
    LlmResult(
        text="테스트 응답",
        model_name="mock-llm",
        finish_reason="stop",
    )
)
```

공유 테스트에서 Backend가 이전 요청을 보관해 원문이 남는 일을 막기 위해 호출 이력 기능은 제공하지 않는다. 호출 인자를 검증해야 하는 테스트에는 별도의 테스트 전용 `LlmBackend` 구현체를 해당 테스트 안에서 사용한다.

## 8. OpenAI-compatible LLM Backend

`app/backends/llm/openai_compatible.py`의 `OpenAICompatibleLlmBackend`는 OpenAI Chat Completions 형식으로 동작하는 로컬 모델 서버를 공통 `LlmBackend` 계약에 연결한다. vLLM, LM Studio와 OpenAI 호환 API를 활성화한 Ollama처럼 동일한 요청·응답 형식을 제공하는 서버에 사용할 수 있다.

이 클래스는 전달받은 `DeploymentConfig`를 호출 직전에도 확인한다.

- `kind=llm`
- `adapterType=openai_compatible`
- `enabled=true`
- `baseUrl`, `modelName`, `timeoutMs` 존재
- 선택적 `contextWindowTokens`는 양의 정수
- `baseUrl`에 query와 fragment가 없음

### 8.1 요청 URL

`baseUrl`의 경로 끝에 `/chat/completions`를 붙인다. 이미 해당 경로로 끝나면 중복해서 붙이지 않는다.

```text
http://localhost:8000/v1
→ http://localhost:8000/v1/chat/completions

http://localhost:8000/v1/chat/completions
→ http://localhost:8000/v1/chat/completions
```

따라서 일반적인 OpenAI 호환 서버는 `baseUrl`을 `/v1`까지 지정하면 된다.

### 8.2 요청 Payload

기본 요청은 비스트리밍 Chat Completions 요청이다.

```json
{
  "model": "qwen3",
  "messages": [
    {
      "role": "user",
      "content": "요청 내용"
    }
  ],
  "stream": false,
  "temperature": 0.1
}
```

호출자가 전달한 `parameters`를 먼저 복사한 뒤 `model`, `messages`, `stream=false`를 Adapter가 설정한다. 다음 필드는 Adapter가 소유하므로 `parameters`에서 사용할 수 없다.

```text
model
messages
stream
n
response_format
```

`n`은 여러 결과를 요청하지 못하게 막는다. 이 Backend는 항상 첫 번째 `choices[0]`만 공통 결과로 변환한다.

`output_schema`가 있으면 다음 OpenAI Structured Outputs 형식으로 변환한다.

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "lpl_output",
      "strict": true,
      "schema": {}
    }
  }
}
```

### 8.3 응답 정규화

응답은 OpenAI Chat Completions 객체로 검증하고 첫 번째 `choices[0]`의 본문,
종료 사유와 token usage를 공통 `LlmResult`로 변환한다. 응답은 최대 1 MiB까지만
Streaming으로 읽으며 유효한 UTF-8과 엄격한 JSON이어야 한다.

### 8.4 오류

- 잘못된 Deployment나 호출 파라미터는 `OPENAI_COMPATIBLE_CONFIG_INVALID`
- 연결 실패는 `OPENAI_COMPATIBLE_REQUEST_FAILED`
- 제한시간 초과는 `OPENAI_COMPATIBLE_TIMEOUT`
- 성공이 아닌 HTTP 상태는 `OPENAI_COMPATIBLE_HTTP_ERROR`
- 잘못되거나 과도하게 큰 응답은 `OPENAI_COMPATIBLE_RESPONSE_INVALID` 또는
  `OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE`

Route는 이 전용 오류를 직접 알지 않고 공통 Backend 오류 처리기를 통해 안전한
HTTP 응답으로 변환한다.

### 8.5 Provider 등록과 Client 수명주기

FastAPI Runtime은 공유 `httpx.AsyncClient`로 Backend 인스턴스를 한 번 만들고
`(llm, openai_compatible)` 키에 등록한다. 요청마다 Client나 Backend를 다시 만들지
않으며 애플리케이션 종료 시 Client를 닫는다.

## 9. 표준 HTTP NER Backend

NER 서버 종류별 차이는 LPL Adapter가 아니라 NER 서버 또는 그 앞단 Wrapper가
책임진다. 사용자는 Adapter를 선택하지 않고 서버 주소와 제한시간만 등록한다.
LPL은 모든 NER Deployment에 내부 키 `http_ner`를 주입하고 하나의
`HttpNerBackend`로 호출한다.

### 9.1 Deployment 계약

NER 관리 API와 `config/ner_deployments.json`은 다음 실행 필드만 받는다.

```json
{
  "baseUrl": "http://ner-server:8008/v1/ner/detect",
  "timeoutMs": 30000,
  "enabled": true
}
```

`baseUrl`은 호출 경로까지 포함한 전체 POST Endpoint URL이다. path는 허용하지만
query, fragment와 사용자 인증정보는 허용하지 않는다. `adapterType`과 `modelName`도
외부 계약에 포함되지 않는다. Endpoint 경로는 서버 구현에 따라 달라도 된다.

### 9.2 고정 요청 계약

LPL은 `baseUrl`을 변경하거나 고정 경로를 붙이지 않고 그대로 호출한다.

```text
POST {baseUrl}
Content-Type: application/json
```

```json
{
  "text": "연락처는 010-1234-5678입니다."
}
```

NER 서버는 최상위 `detections` 하나만 반환하며 각 항목도 아래 다섯 필드만
포함해야 한다.

```json
{
  "detections": [
    {
      "start": 5,
      "end": 18,
      "text": "010-1234-5678",
      "type": "CONTACT",
      "score": 0.98
    }
  ]
}
```

외부 서버는 `source`를 반환하지 않는다. LPL이 검증 후 항상 `source="ner"`를
설정한다. 중복 키, 추가 필드, 잘못된 자료형과 유효하지 않은 span은 fail-closed로
거부한다. `type`은 공통 14개 정책 코드 중 하나여야 하며 목록 밖 값도 응답 계약
위반으로 전체 거부한다. HTTP redirect는 따라가지 않으며 응답은 최대 1 MiB까지만
읽는다.

### 9.3 장문 입력 오류 계약

NER 서버가 원문 전체를 처리할 수 없으면 `413 Payload Too Large`와 다음의 제한된
JSON 중 하나를 반환해야 한다.

```json
{
  "code": "NER_INPUT_TOO_LONG",
  "detail": "입력이 모델의 최대 길이를 초과했습니다."
}
```

```json
{
  "code": "NER_INPUT_TOO_LONG",
  "detail": "입력이 모델의 최대 길이를 초과했습니다.",
  "maxTokens": 512
}
```

LPL은 이 정확한 계약만 공통 `NER_INPUT_TOO_LONG` 오류로 승격한다. 다른 413 본문은
일반 `HTTP_NER_HTTP_ERROR`로 처리한다.

### 9.4 기존 NER 서버 연결

기존 서버가 Hugging Face 배열, GLiNER 전용 객체 등 다른 요청·응답 형식을
사용한다면 서버 자체를 수정하거나 앞단 Wrapper를 두어 표준 계약으로 변환해야
한다. 이전 `gliner_http`, `hf_inference_token_classification` 설정은 더 이상
Registry에서 허용하지 않는다. 저장소의 `services/gliner_ner`는 라벨과 임계값,
GLiNER 출력 변환을 서버 안에서 처리하고 이 표준 Endpoint를 직접 제공한다.

## 10. 계약과 Provider 등록

LLM 구현체를 사용하려면 설정 계약과 인스턴스를 같은 키로 등록한다. NER는 고정
`http_ner` 내부 계약과 `HttpNerBackend` 하나를 애플리케이션이 조립하며, 외부
설정이나 Adapter 목록으로 선택하지 않는다.

```python
from app.backends import (
    BackendRegistration,
    BackendProviderRegistration,
    BackendProviderRegistry,
    create_default_backend_registry,
)
from app.registry import (
    RegistryFileStore,
    RegistrySnapshotBuilder,
    RegistryValidator,
)


backend_registry = create_default_backend_registry(
    [
        BackendRegistration(
            kind="llm",
            adapter_type="ollama_native",
            required_fields=frozenset(
                {"base_url", "model_name", "timeout_ms"}
            ),
        )
    ]
)

backend_providers = BackendProviderRegistry(
    [
        BackendProviderRegistration(
            kind="llm",
            adapter_type="ollama_native",
            provider=OllamaNativeLlmBackend(http_client),
        ),
        BackendProviderRegistration(
            kind="ner",
            adapter_type="http_ner",
            provider=HttpNerBackend(http_client),
        )
    ]
)

validator = RegistryValidator(backend_registry)
store = RegistryFileStore("config", validator=validator)
builder = RegistrySnapshotBuilder(
    store,
    backend_providers=backend_providers,
)
```

`RegistrySnapshotBuilder` 생성 시 Provider에 대응하는 계약이 모두 있는지 확인하고, `build()` 시 활성 Deployment에 필요한 Provider가 모두 있는지 확인한다.
기본 FastAPI Runtime은 공유 `httpx.AsyncClient`로 `HttpNerBackend`를 한 번 생성해
내부 `(ner, http_ner)` Provider로 등록하며 애플리케이션 종료 시 Client를 닫는다.

## 11. Deployment 예시

`config/llm_deployments.json`:

```json
{
  "llm-ollama-qwen": {
    "adapterType": "ollama_native",
    "baseUrl": "http://localhost:11434",
    "modelName": "qwen3:8b",
    "timeoutMs": 30000,
    "enabled": true
  }
}
```

`config/ner_deployments.json`:

```json
{
  "ner-http-a": {
    "baseUrl": "http://ner-server:9000/v1/ner/detect",
    "timeoutMs": 5000,
    "enabled": true
  }
}
```

물리 JSON에는 `kind`를 저장하지 않는다. 파일명이 종류를 결정하며 로드할 때
내부 `DeploymentConfig.kind`로 주입된다. Backend 계약 표와 Provider 등록 코드의
`kind`는 이 내부 런타임 값을 의미한다.

현재 공통 설정에는 인증 Secret 필드가 없다. NER `baseUrl`에는 호출 경로까지
포함한 전체 POST Endpoint를 등록한다. 인증이 필요한 구현체는
서버 또는 네트워크 경계에서 관리하거나 이후
`secretRef` 같은 명시적 계약을 추가해야 한다.

## 12. 런타임 선택 흐름

```text
RegistryManager.capture()
→ DeploymentResolver.resolve_detection(...), resolve_masking(...) 또는 resolve_generation(...)
→ 같은 Snapshot의 ResolvedDeployment와 PromptArtifact를 담은 역할별 실행 계획
→ Detection: NER Backend 실행 → Regex + NER로 Prompt 렌더링 → LLM Backend 실행
→ Masking: 전체 Detection 합집합 component → 정적 Prompt + canonical JSON → LLM Backend 실행
→ Generation: 사용자 text로 LLM Backend 실행
→ Detection 목록, MaskResponse 또는 LlmResult
→ Pipeline 검증·병합·응답
```

Prompt ID나 Prompt 선택 설정은 없다. NER 개체 탐지는 Prompt 없이 실행하고, 후속
LLM 추가 탐지는 물리 파일 `config/prompts.j2`에서 컴파일한
단일 Artifact를 사용한다. 마스킹은 외부 변수가 없는
`config/mask_prompt.j2`의 별도 Artifact를 정적 System Prompt로 사용한다. 파일명은 코드 상수이며 요청이나 Registry가 경로를
선택하지 않는다. Active Snapshot은 Artifact를 저장하고 그 수명을 보장한다. `DeploymentResolver`는
Provider를 선택하거나 호출하지 않고, 같은 Snapshot의 Deployment와 Artifact 객체
자체를 불변 실행 계획으로 조립한다. Pipeline은 Plan만 소비하여 선택된
Deployment를 Provider Registry에 전달한다. NER 모델별 요청·응답 차이는 표준 서버
또는 Wrapper가 흡수하고, LLM 모델별 차이는 Backend 구현체가 흡수한다. Pipeline에는
`adapterType` 조건문을 누적하지 않는다.

### 12.1 MaskingPipeline

`app/services/masking_pipeline.py`의 `MaskingPipeline`은 FastAPI에 의존하지 않는
Option B 마스킹 오케스트레이션 서비스다.

```text
MaskingPipeline.mask(
    text=...,
    llm_deployment_id="llm-local-a",
    detections=(...),
)
→ RegistryManager.capture() (요청당 한 번)
→ plan = DeploymentResolver.resolve_masking(llm_deployment_id, snapshot)
→ 입력 Detection의 span·text·source 검증
→ 서로 겹치는 span만 최대 합집합 component로 병합
→ 빈 detections이면 Plan 검증 후 Backend·namespace 생성 없이 원문과 빈 replacements 반환
→ plan.mask_prompt를 정적 system 메시지로 렌더링
→ component와 서버 생성 namespace를 canonical JSON user 메시지로 전달
→ LlmBackend.generate()
→ targetId/entityId assignment 구조화 출력 Parser
→ LPL이 start/end/types/sources/entityId/placeholder 파생
→ 전체 coverage·비탐지 원문·maskedText 재구성 fail-closed 검증
→ MaskResponse
```

모델은 `maskedText`와 검증된 `targetId`의 요청 범위 `entityId` 그룹을 출력하지만
원문 span, type, source와 replacement placeholder 필드는 직접 만들지 않는다.
LPL은 모든 component가 정확히 한 번 포함되는지, 같은 entity ID가 동일한 요청 전용
placeholder를 사용하는지와 Detection 밖 원문이 byte-for-byte 보존되는지 검사한다.
잘못된 항목만 제거하거나 원문·부분 마스킹 결과로 fallback하지 않는다.
Backend 호출에는 `parameters={"max_tokens": 16384}`와 현재 target ID·assignment
수를 제한한 요청별 strict JSON Schema를 `output_schema`로 전달한다. Adapter가
구조화 출력을 지원하더라도 Parser와 Validator의 서버 측 검증은 그대로 수행한다.

`MaskingPipeline`은 entity 매핑을 요청 이후 저장하지 않는다. 자세한 HTTP 계약과
Gateway 책임은 [Masking API](./masking.md)를 따른다.

### 12.2 GenerationPipeline

`app/services/generation_pipeline.py`의 `GenerationPipeline`은 FastAPI에 의존하지 않는 Option A 생성 오케스트레이션 서비스다.

```text
GenerationPipeline.generate(
    text=...,
    previous_text=...,
    llm_deployment_id="llm-local-a",
)
→ RegistryManager.capture() (요청당 한 번)
→ plan = DeploymentResolver.resolve_generation(llm_deployment_id, snapshot)
→ plan.llm_deployment로 require_llm()
→ previous_text의 user·assistant 역할을 보존하고 text는 마지막 user 메시지로 구성
→ LlmBackend.generate()
→ LlmResult
```

Generation Pipeline은 Prompt 구성요소를 사용하지 않는다. 선택적 `previous_text`
배열의 각 `user`·`assistant` 역할과 순서를 보존하고 필수 `text`를 마지막 `user`
메시지로 구성하여 선택된 Backend에 전달한다. content는 수정하거나 렌더링하지 않는다. 현재 요청
계약에는 모델 파라미터와 출력 Schema가 없으므로 요청자가 이를 직접 주입할 수
없다. Backend에는 빈 `parameters`와 `output_schema=None`을 전달한다.

기본 Runtime은 `LlmLimitsService`를 같은 공유 `httpx.AsyncClient`와 Registry로
조립한다. Ollama 기본 `/v1` 주소는 Native `/api/show`, `/api/ps`를 최대 1 MiB의
엄격한 JSON으로 조회하고, 실패하면 Registry의 `contextWindowTokens`로 fallback한다.
`LlmLimitsService`는 모델·실행·Registry 컨텍스트 한도를 조회만 하며
`GenerationPipeline` 입력을 근사 계산하거나 사전 차단하지 않는다.

Pipeline은 요청 시작 시 Active Snapshot을 한 번만 캡처하고 Resolver가 그
Snapshot에서 완전한 `GenerationExecutionPlan`을 조립하도록 한다. Pipeline은 Plan을
받은 뒤 Snapshot의 리소스를 다시 조회하지 않는다. 따라서 요청 처리 도중
Deployment 설정 Reload가 성공해도 진행 중인 요청에는 새 실행 설정이 섞이지
않는다. 고정 Prompt는 첫 정상 build에서 캐시되어 Reload 대상이 아니며 Generation
자체도 이 Artifact를 사용하지 않는다.

`text`와 `llm_deployment_id`는 필수 인자이고 `previous_text`는 선택 인자다.
Gateway는 모든 생성 요청에 선택한
Deployment ID를 전달하며, 존재하지 않거나 비활성화됐거나 역할과 kind가 맞지 않는
ID는 다른 Deployment로 대체하지 않는다. Deployment 해석은 캡처한 같은 Snapshot에서
이뤄지며 Generation은 Prompt Artifact를 사용하지 않는다.

Pipeline은 `RegistryManager` 초기화와 Reload, Backend의 생명주기, HTTP 상태
코드와 오류 응답 변환을 담당하지 않는다. Deployment, Provider와 Backend에서
발생한 구체적인 오류는 FastAPI 전달 계층으로 그대로 전파한다.
Backend 실행 오류는 각 Route가 아니라 `app/api/error_handlers.py`의 전역 handler가
공통 상위 타입을 기준으로 안전한 HTTP 오류로 변환한다. 요청·응답과
상태 코드 계약은 [Generation API](./generation.md)에서 설명한다.

### 12.3 DetectionPipeline

`app/services/detection_pipeline.py`의 `DetectionPipeline`은 NER 개체 탐지와
후속 LLM 후보 판정·누락·문맥형 정보 추가 탐지를 한 요청 안에서 순서대로 실행한다.

```text
DetectionPipeline.detect(
    text=...,
    ner_deployment_id="ner-local-a",
    llm_deployment_id="llm-local-a",
)
→ Snapshot capture와 DetectionExecutionPlan 조립
→ 기존 Regex 결과 재검증
→ Plan의 NER Entity Detection 필수 실행
→ Regex + NER 결과를 evidence로 plan.detection_prompt 렌더링
→ Plan의 Detection LLM이 Regex·NER 후보 판정과 누락·문맥형 정보 추가 탐지
→ LLM 후보 text의 원문 좌표를 서버에서 계산
→ 각 Backend batch Parser·Span·source 검증
→ 중복·겹침 정책 적용
→ 후보 판정과 CONFIRMED NER·신규 LLM Detection 반환
```

Entity Deployment는 반드시 `kind=ner`이며 Prompt 없이 `require_ner()`와
`detect()`를 호출한다. 이후 항상 Plan의 `detection_prompt`와
`llm_deployment`를 사용한다. 후속 LLM은 Regex와 NER 결과를 근거로
모든 Regex·NER 후보를 `CONFIRMED`, `REJECTED`, `UNCERTAIN`으로 판정하고 누락된
민감정보도 추가한다.

후속 LLM 호출은 `temperature=0`, `reasoning_effort="none"`과 strict
`output_schema`를 사용한다. Schema에는
정확한 후보 수·candidateId를 제한한 `candidateDecisions`와 `text`, `type`, `score`
세 필드의 `newDetections`, 활성 `type`, 값 범위와 `additionalProperties=false`를
명시한다. OpenAI-compatible
Adapter는 이를 `response_format.type=json_schema`로 변환한다. 새 LLM Adapter도
탐지에 사용하려면 이 공통 Schema를 자신의 네이티브 구조화 출력 기능으로 변환해야
한다. Backend의 Schema 지원 여부와 관계없이 모델 출력은 Prompt 계약과
`LlmDetectionOutputParser`로 재검증한다. `LlmDetectionSpanResolver`는 식별자·연락처
등 반복 가능한 7개 타입을 모든 정확한 원문 위치로 확장하고, 문맥형 7개 타입은
정확히 한 위치에만 나타날 때 허용한다. 원문에 없는 문자열, 문맥형 반복 문자열이나
확장 결과 상한 초과는 좌표를 추측하지 않고 거부한다.

기본 탐지 Prompt에는 `config/policy_prompts.json`의 선택된 정책 지침을 조립한다.
후보 유무와 관계없이 현재 활성화된 14개 정책을 고정 순서로 한 번씩 포함하며,
같은 전체 활성 집합으로 Prompt Context와 output schema의 허용 type을 제한한다.

Pipeline은 기존 Regex, NER와 LLM 결과를 모두 비신뢰 batch로 취급한다. 기존 결과에는
`expected_source="regex"`, NER에는 `ner`, Parser 결과에는 `llm`을 적용한다. 기존
Regex는 문맥 증거와 중복 억제에 사용하고 판정은 `candidateDecisions`로 반환하지만
최종 `detections`에서는 제외한다.
LLM Provider가 반환한 객체는 공통 `llm_result_validator`에서 전체 `LlmResult`와
중첩 Usage를 다시 확인하므로 `model_construct()`로 검증을 우회한 결과도 실행
계약 오류로 거부한다.
