# Generation API

## 1. 목적과 범위

Generation API는 사용자가 보낸 입력을 Registry의 요청된 Local LLM Deployment로
전달하여 Option A 생성을 실행하는 HTTP 인터페이스다. FastAPI Endpoint는
요청을 `GenerationPipeline`에 위임하고, 공통 Backend 예외의 HTTP 변환은 전역
exception handler가 담당한다.

```text
POST /generate
→ 필수 사용자 text·llmDeploymentId와 선택적 previousText 수신
→ 요청 시점 Active Registry Snapshot 캡처
→ 전달받은 LLM Deployment 해석
→ LLM Backend 선택
→ previousText 각 항목을 순서대로 assistant 메시지, text는 user 메시지로 전달
→ LlmResult 반환
```

Generation 단계에는 별도 Prompt 리소스나 Jinja2 렌더링이 없다. 사용자가 요청한
LLM Deployment를 선택하며, 선택적 `previousText`와 현재 `text`가 모델에 전달되는
실제 대화 Context다. 이
Endpoint는 스트리밍을 제공하지 않으며 모델 Endpoint, 모델명, API Key와 모델
파라미터를 요청에서 직접 받지 않는다.

대화방 제목 생성은 이 Endpoint의 모드나 옵션이 아니다. Gateway가 첫 사용자
메시지 후 별도 `POST /titles`를 호출하고 반환된 제목을 저장한다. LPL은
대화방이나 제목을 저장하지 않는다. 자세한 계약은
[대화 제목 생성 API](./title_generation.md)를 따른다.

## 2. Endpoint

```http
POST /generate
Content-Type: application/json
```

정상 응답 상태는 `200 OK`다. FastAPI가 생성한 요청·응답 Schema는 `/docs`와
`/openapi.json`에서 확인할 수 있다.

## 3. 요청 계약

요청 예시:

```json
{
  "previousText": [
    {
      "role": "user",
      "content": "회의 내용을 정리해 주세요."
    },
    {
      "role": "assistant",
      "content": "회의에서는 예산과 일정을 검토했습니다."
    }
  ],
  "text": "다음 회의록을 요약해 주세요.",
  "llmDeploymentId": "llm-local-a"
}
```

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `text` | string | O | 길이 1 이상 | Local LLM에 단일 `user` 메시지로 전달할 사용자 입력 |
| `previousText` | array[object] | X | `role`, `content`만 허용 | 이전 사용자 입력과 Local LLM 응답 목록. 생략 또는 `[]` 가능 |
| `llmDeploymentId` | string | O | `^[a-z0-9][a-z0-9._-]*$` | 실행할 `kind=llm` Deployment ID |

요청 모델은 알 수 없는 최상위 필드를 거부한다. `messages`, `promptId`, `context`,
`variables`, 모델 URL과 모델별 파라미터는 전달할 수 없다. `text`와
`llmDeploymentId`는
필수이므로 빈 객체 `{}`, 빈 `text` 또는 누락·`null`인 `llmDeploymentId`는
`REQUEST_VALIDATION_FAILED`로 거부된다. `previousText`는 선택 필드지만 제공한 경우
JSON 배열이어야 하며 각 항목에는 `role`, `content`만 있어야 한다. `role`은
`user` 또는 `assistant`, `content`는 빈 문자열이 아닌 문자열이다. `null`, 단일
문자열, `system` 역할과 추가 필드는 허용하지 않는다. 현재
Schema는 공백만 있는 문자열을 별도로 제거하거나 거부하지 않으며 HTTP 본문과
`text`의 최대 byte 크기도 지정하지 않는다.

### 3.1 Deployment 선택 규칙

- Gateway는 모든 생성 요청에 문자열 `llmDeploymentId`를 반드시 전달한다.
- 전달한 ID가 존재하지 않아도 다른 Deployment로 대체하지 않는다.
- Deployment 조회, 활성화 상태와 `kind=llm` 검증은 요청 시작 시 캡처한 같은
  Snapshot을 사용한다.

따라서 요청 처리 도중 Deployment 설정 Reload가 성공해도 진행 중인 요청의
Deployment가 새 설정과 섞이지 않는다.

## 4. Backend 전달 형식

`GenerationPipeline`은 요청의 `previousText`와 `text`를 수정하거나 템플릿에
삽입하지 않고 다음 공통 LLM Backend 입력으로 구성한다.

```python
messages = [
    {
        "role": previous_text[0].role,
        "content": previous_text[0].content,
    },
    {
        "role": previous_text[1].role,
        "content": previous_text[1].content,
    },
    {
        "role": "user",
        "content": text,
    }
]
```

`previousText`가 생략되거나 빈 배열이면 `assistant` 메시지를 만들지 않고 현재와
동일하게 `text` 하나만 `user` 메시지로 전달한다. 배열의 각 항목은 지정한 `user`
또는 `assistant` 역할과 순서를 그대로 유지한다. 현재 `text`는 항상 마지막 `user`
메시지로 추가한다.

현재 API 요청 계약에는 모델 파라미터와 구조화 출력 Schema가 없으므로
요청자가 파라미터를 직접 지정할 수 없다. Pipeline은 빈 `parameters`와
`output_schema=None`을 전달한다. 모델 서버별 HTTP 요청 모양으로 바꾸는 책임은
선택된 LLM Backend Adapter에 있다. 모델 컨텍스트 조회 계약은
[Deployment 조회·관리 API](./deployments.md#41-llm-컨텍스트-한도-조회)를 따른다.

## 5. 성공 응답

응답은 Backend별 결과를 공통 `LlmResult` 형식으로 정규화한다.

```json
{
  "text": "요약된 결과입니다.",
  "modelName": "local-model-a",
  "finishReason": "stop",
  "usage": {
    "inputTokens": 42,
    "outputTokens": 18,
    "totalTokens": 60
  }
}
```

| 필드 | JSON 타입 | 필수 | 설명 |
|---|---|---:|---|
| `text` | string | O | 생성된 최종 텍스트 |
| `modelName` | string | X | 모델 서버가 반환한 모델 이름 |
| `finishReason` | string | X | 모델 서버가 반환한 종료 이유 |
| `usage` | object | X | 정규화된 토큰 사용량 |
| `usage.inputTokens` | integer | O | 0 이상의 입력 토큰 수 |
| `usage.outputTokens` | integer | O | 0 이상의 출력 토큰 수 |
| `usage.totalTokens` | integer | O | 0 이상의 전체 토큰 수 |

`modelName`, `finishReason`, `usage`가 `None`이면 HTTP 응답에서 해당 필드를
`null`로 내보내지 않고 생략한다.

## 6. 오류 응답

실행 중 예외를 변환한 오류는 다음 공통 모양을 사용한다.

```json
{
  "detail": {
    "code": "DEPLOYMENT_NOT_FOUND",
    "message": "요청한 Deployment를 찾을 수 없습니다"
  }
}
```

오류 코드와 상태 코드는 `app/api/error_handlers.py`의 전역 handler,
Runtime 의존성 및 공통 요청 검증 handler의 매핑을 기준으로 한다.

| HTTP 상태 | 코드 | `message` | 조건 |
|---:|---|---|---|
| 422 | `REQUEST_VALIDATION_FAILED` | 잘못된 필드와 안전한 사유를 포함한 검증 메시지 | `text` 또는 `llmDeploymentId` 누락, 빈 문자열, `null`, 배열이 아니거나 항목이 잘못된 `previousText`, JSON·타입·ID 또는 알 수 없는 필드 |
| 404 | `DEPLOYMENT_NOT_FOUND` | `요청한 Deployment를 찾을 수 없습니다` | 전달한 Deployment가 없음 |
| 409 | `DEPLOYMENT_DISABLED` | `요청한 Deployment가 비활성화되어 있습니다` | 전달한 Deployment가 비활성화됨 |
| 422 | `DEPLOYMENT_KIND_MISMATCH` | `요청한 Deployment를 해당 역할에 사용할 수 없습니다` | NER Deployment를 생성 LLM으로 요청함 |
| 503 | `REGISTRY_NOT_INITIALIZED` | `Registry가 아직 준비되지 않았습니다` | Registry Manager가 초기화되지 않음 |
| 503 | `APPLICATION_RUNTIME_UNAVAILABLE` | `애플리케이션 실행 구성이 준비되지 않았습니다` | FastAPI lifespan Runtime을 조회할 수 없음 |
| 503 | `BACKEND_PROVIDER_NOT_REGISTERED` | `실행할 Backend Provider가 준비되지 않았습니다` | Deployment의 Backend Provider가 등록되지 않음 |
| 503 | `BACKEND_PROVIDER_KIND_MISMATCH` | `실행할 Backend Provider가 준비되지 않았습니다` | Deployment와 Provider kind가 맞지 않음 |
| 504 | `OPENAI_COMPATIBLE_TIMEOUT` | `모델 서버 응답 제한 시간을 초과했습니다` | 모델 서버 응답이 Deployment timeout을 초과함 |
| 500 | `OPENAI_COMPATIBLE_CONFIG_INVALID` | `모델 서버 실행 설정이 올바르지 않습니다` | 모델 서버 실행 설정이 잘못됨 |
| 502 | `OPENAI_COMPATIBLE_REQUEST_FAILED` | `모델 서버 호출 또는 응답 처리에 실패했습니다` | 모델 서버로 요청을 전송하지 못함 |
| 502 | `OPENAI_COMPATIBLE_HTTP_ERROR` | `모델 서버 호출 또는 응답 처리에 실패했습니다` | 모델 서버가 성공이 아닌 HTTP 상태를 반환함 |
| 502 | `OPENAI_COMPATIBLE_RESPONSE_INVALID` | `모델 서버 호출 또는 응답 처리에 실패했습니다` | 모델 서버 응답을 공통 결과로 변환할 수 없음 |
| 502 | `OPENAI_COMPATIBLE_RESPONSE_TOO_LARGE` | `모델 서버 호출 또는 응답 처리에 실패했습니다` | 모델 서버 응답이 허용 크기를 초과함 |
| 500 | `GENERATION_BACKEND_RESULT_INVALID` | `Backend 결과가 생성 응답 계약과 다릅니다` | Backend 반환값이 `LlmResult` 계약과 다름 |

OpenAI 호환 코드는 Adapter 고유 세부 원인을 보존한다. HTTP 상태와
안전한 메시지는 각 예외의 공통 상위 타입인 `BackendConfigurationError`
(500), `BackendTimeoutError`(504), `BackendTransportError`(502),
`BackendResponseError`(502)를 기준으로 결정한다. `/generate` Route는
OpenAI 전용 예외를 import하지 않으며, 새 Adapter가 이 공통 계약을 따르면
Route나 HTTP 매핑을 수정할 필요가 없다.

Pydantic/FastAPI 요청 검증 오류도 다른 오류와 같은 고정된 `detail` 객체로
정규화한다. FastAPI 기본 검증 오류 배열은 잘못된 입력값을 포함할 수 있으므로
반환하지 않는다. 따라서 잘못된 사용자 `text`를 오류 응답에 복사하지 않는다.

## 7. FastAPI lifespan과 Runtime

`app/main.py`의 `create_app()`은 주입 가능한 Runtime factory와 lifespan을
사용한다. 기본 `application_runtime()`은 시작할 때 다음 순서로 실행 객체를
조립한다.

```text
RegistryFileCoordinator, RegistryValidator, PromptRenderer 생성
→ trust_env=False인 공유 httpx.AsyncClient 생성
→ Mock·공통 HTTP·Hugging Face Inference Token Classification·GLiNER HTTP NER와
  Mock·OpenAI-compatible LLM Provider 등록
→ RegistryFileStore와 RegistrySnapshotBuilder 조립
→ ner_deployments.json과 llm_deployments.json 검증·병합
→ 고정 탐지·마스킹·제목 생성 Prompt를 한 번 검증·컴파일하고 Artifact 캐시
→ RegistryManager.initialize()
→ DetectionPipeline, MaskingPipeline, GenerationPipeline과 TitleGenerationPipeline 조립
→ app.state.runtime에 Runtime 게시
```

Prompt 구성요소는 고정 물리 파일 `config/prompts.j2`, `config/policy_prompts.json`,
`config/mask_prompt.j2`와 `config/title_prompt.j2`를 활성 Snapshot으로 준비한다.
탐지 Pipeline은 기본 Prompt와 정책 Catalog를 조립하고, MaskingPipeline과
TitleGenerationPipeline은 각 정적 System Prompt를 사용한다. 네 리소스 모두 ID나
요청값으로 선택하는 Registry 리소스가 아니며 GenerationPipeline에는 Prompt
구성요소가 주입되지 않는다. 이후 Deployment 설정 Reload는 캐시된 동일한 세
Prompt Artifact와 정책 Catalog를 재사용하며 파일을
다시 읽지 않는다.

설정 JSON, Prompt 또는 활성 Deployment의 Provider coverage가
잘못되면 최초 Snapshot을 활성화하지 않고 애플리케이션 시작 자체가 실패한다.
마지막 정상 Snapshot이 없는 최초 시작에서 잘못된 설정으로 요청을 받지 않기
위한 동작이다.

모든 요청은 같은 `httpx.AsyncClient`, Backend Provider Registry와 Registry
Manager를 재사용한다. 종료 시 공유 AsyncClient Context를 종료하여 연결 풀을
닫는다. `GenerationPipeline`은 이 수명주기를 소유하지 않는다.

## 8. 보안과 자원 제한

### 8.1 등록된 Deployment ID만 지정한다

`/generate`는 사용자 `text`, 선택적 `previousText`와 필수 `llmDeploymentId`만
받는다. 다음 값은 HTTP
요청으로 받지 않는다.

- 임의 모델 URL과 Endpoint path
- 모델 이름과 `adapterType`
- API Key 또는 Secret
- Jinja 템플릿, Prompt ID 또는 Prompt Context
- 임의 역할을 가진 메시지 배열
- 모델별 `parameters`와 `output_schema`

요청자는 등록된 ID를 선택할 수 있지만 Endpoint나 Adapter 설정을 직접 주입할 수
없다. 실행 대상은 검증된 Registry Snapshot의 Deployment로만 선택한다.
사용자 `text`는 선택된 로컬 LLM에 전달되는 데이터이므로 해당 Deployment가 실제
신뢰 가능한 로컬 영역인지 배포·운영 정책에서 보장해야 한다. 현재 코드에는
`trustZone` 강제 검증이 없다.

### 8.2 사용자 입력과 모델 서버 오류를 노출하지 않는다

- Pipeline은 `previousText`와 `text`를 Jinja2 코드로 해석하지 않고 일반 문자열
  content로 전달한다.
- 공유 `httpx.AsyncClient`는 `trust_env=False`로 생성하여 환경변수의 Proxy
  설정을 모델 서버 호출에 자동 적용하지 않는다.
- OpenAI-compatible Backend의 디코딩 응답은 기본 1,048,576 bytes로 제한한다.
- 모델 서버 응답 본문, 사용자 `text`, 인증 정보와 내부 예외 상세를 API 오류
  메시지에 복사하지 않는다.
- 이 API 자체에는 인증·인가가 구현되어 있지 않다. Gateway 뒤의 내부
  네트워크에 배치하고 네트워크 접근 제어 또는 상위 Gateway 인증을 적용해야
  한다.

현재 FastAPI 코드에는 HTTP 요청 본문이나 `text`에 대한 별도 byte 상한이 없다.
운영 환경의 Reverse Proxy나 ASGI Server에서도 요청 크기 제한을 설정해야 한다.

## 9. 현재 제한사항

- 생성 응답 스트리밍은 지원하지 않는다.
- 선택적 `previousText`는 `user`, `assistant` 역할의 이전 대화를 순서대로
  전달하고 현재 입력은 마지막 `user` 메시지로 추가한다. `system` 역할은 요청할
  수 없다.
- 요청별 모델 파라미터와 구조화 출력 Schema는 아직 없다. Pipeline은 빈
  `parameters`와 `output_schema=None`을 사용한다.
- 기본 Runtime의 생성 Provider는 `llm/openai_compatible` 하나다.
- Deployment Registry Reload 관리 Endpoint와 자동 파일 감시는 없다.
- `/health`는 모델 서버를 실제 호출하는 readiness 검사가 아니라 HTTP 프로세스
  상태만 반환한다.
- Detection Pipeline은 Runtime에 조립되어 `POST /detect`로 실행할 수 있다.
- MaskingPipeline은 Runtime에 별도로 조립되며 `POST /mask`로 실행한다. 요청·응답과
  fail-closed 계약은 [Masking API](./masking.md)를 따른다.
- TitleGenerationPipeline은 Runtime에 별도로 조립되며
  `POST /titles`로 실행한다.
