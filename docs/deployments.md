# Deployment 조회·관리 API

## 1. 목적

Deployment API는 현재 Active Registry Snapshot에 등록된 NER·LLM Deployment의
운영 요약과 Gateway용 운영 상세를 조회하고, 종류별 설정 파일에 Deployment를
추가하거나 전체 교체하고 활성 상태를 변경하거나 비활성 항목을 삭제하는 HTTP
인터페이스다.

```text
GET /deployments/ner
POST /deployments/ner
GET /deployments/ner/{deployment_id}
PUT /deployments/ner/{deployment_id}
PATCH /deployments/ner/{deployment_id}/enabled
DELETE /deployments/ner/{deployment_id}
POST /deployments/ner/{deployment_id}/probe
GET /deployments/llm
POST /deployments/llm
GET /deployments/llm/{deployment_id}
GET /deployments/llm/{deployment_id}/limits
PUT /deployments/llm/{deployment_id}
PATCH /deployments/llm/{deployment_id}/enabled
DELETE /deployments/llm/{deployment_id}
POST /deployments/llm/{deployment_id}/probe
```

조회와 Probe는 파일을 직접 다시 읽지 않고 `RegistryManager.capture()`로 얻은
하나의 Active Snapshot을 사용한다. 추가·전체 수정·활성 상태 변경·삭제는 NER이면
`config/ner_deployments.json`, LLM이면 `config/llm_deployments.json` 한 파일만
원자 교체한 뒤 새 Snapshot을 활성화한다. 활성화가 실패하면 저장 전 설정과
Snapshot으로 복원한다. HTTP `POST`·`PUT`·`PATCH`·`DELETE`는 항상
`DeploymentManagementService`를 통하며 오프라인 파일 편집 도구인
`OfflineRegistryEditor`를 호출하지 않는다.

저장소의 기본 NER·LLM Deployment 파일은 모두 `{}`다. 따라서 최초 시작 시 목록은
빈 배열이며, 이 문서의 예시처럼 `POST /deployments/ner`와
`POST /deployments/llm`으로 실행할 Endpoint를 먼저 등록한다.

## 2. Deployment 추가·수정·활성 상태 변경·삭제

경로가 Deployment의 `kind`를 결정하므로 요청 본문에는 `kind`를 넣지 않는다.
NER와 LLM 파일을 분리해도 Deployment ID는 전체 Registry에서 고유해야 한다.
관리 요청의 계약 필드는 문서에 표시된 camelCase 이름만 허용한다.
`deployment_id`, `adapter_type`, `base_url` 같은 Python snake_case
이름은 알 수 없는 필드로 처리하여 `422 REQUEST_VALIDATION_FAILED`로 거부한다.

### 2.1 추가

```text
POST /deployments/ner
POST /deployments/llm
```

요청 본문에는 새 `deploymentId`와 전체 실행 설정을 넣는다. 다음은 NER 추가
예시다.

```json
{
  "deploymentId": "ner-gliner-local",
  "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
  "timeoutMs": 5000,
  "enabled": true
}
```

정상적으로 추가하면 `201 Created`와 Deployment 상세를 반환한다.
NER는 `adapterType`과 `modelName`을 받지 않는다. `baseUrl`은 path까지 포함한
전체 POST Endpoint이며, LPL은 별도 경로를 붙이지 않는다. 모델별 라벨, 임계값과
출력 변환은 NER 서버가 책임진다.

### 2.2 전체 수정

```text
PUT /deployments/ner/{deployment_id}
PUT /deployments/llm/{deployment_id}
```

수정할 ID는 경로에만 넣고 본문에는 `deploymentId`를 넣지 않는다. `PATCH`가
아니므로 본문은 해당 Deployment의 전체 새 설정이다.

NER 전체 수정 본문:

```json
{
  "baseUrl": "http://ner-server:8008/v1/ner/detect",
  "timeoutMs": 30000,
  "enabled": true
}
```

LLM 전체 수정 본문:

```json
{
  "adapterType": "openai_compatible",
  "baseUrl": "http://127.0.0.1:11434/v1",
  "modelName": "qwen3:8b",
  "timeoutMs": 30000,
  "enabled": true
}
```

정상 수정은 `200 OK`와 변경된 상세를 반환한다. LLM ID를 NER 경로로 수정하는
식의 종류 변경은 `404 DEPLOYMENT_NOT_FOUND`로 거부한다.

### 2.3 활성화·비활성화

```text
PATCH /deployments/ner/{deployment_id}/enabled
PATCH /deployments/llm/{deployment_id}/enabled
```

기존 실행 설정을 다시 보낼 필요 없이 `enabled`만 변경한다. Request Body는 정확히
다음 boolean 필드 하나만 허용한다.

```json
{
  "enabled": true
}
```

`adapterType`, `baseUrl`, `modelName`, `timeoutMs`, `deploymentId` 또는 알 수 없는
추가 필드를 보내면 `422 REQUEST_VALIDATION_FAILED`로 거부한다. `enabled` 누락이나
문자열 `"true"`도 허용하지 않는다.

정상 변경은 `200 OK`와 기존 상세 조회·전체 수정 API와 같은 Deployment 상세를
반환한다. 예를 들어 LLM 활성화 응답은 다음과 같다.

```json
{
  "deploymentId": "llm-local-a",
  "enabled": true,
  "adapterType": "openai_compatible",
  "baseUrl": "http://127.0.0.1:11434/v1",
  "modelName": "qwen3:8b",
  "timeoutMs": 30000
}
```

같은 `enabled` 값을 반복해서 보내도 `200 OK`를 반환하는 멱등 연산이다. 이 경우
파일 내용과 Snapshot이 바뀌지 않을 수 있으며 Reload의 `NO_CHANGE`도 정상 성공으로
처리한다. 존재하지 않거나 경로 종류와 다른 Deployment ID는
`404 DEPLOYMENT_NOT_FOUND`로 반환한다.

새 Deployment는 다음 순서로 등록하면 실제 연결을 확인하기 전에 일반 실행 경로에
노출되는 것을 막을 수 있다.

```text
enabled=false로 POST 등록
→ POST /deployments/{kind}/{deployment_id}/probe로 실제 연결 확인
→ Probe 성공 시 PATCH /deployments/{kind}/{deployment_id}/enabled
   body: {"enabled": true}
→ 일반 /detect, /mask, /generate 또는 /titles 요청에서 사용
```

### 2.4 삭제

```text
DELETE /deployments/ner/{deployment_id}
DELETE /deployments/llm/{deployment_id}
```

Request Body 없이 비활성 상태인 Deployment를 Registry에서 제거한다. 성공하면
본문 없는 `204 No Content`를 반환한다. 존재하지 않거나 경로 종류와 실제
Deployment 종류가 다르면 `404 DEPLOYMENT_NOT_FOUND`로 거부한다.

`enabled=true`인 Deployment는 삭제하지 않는다. 삭제 API가 자동으로 비활성화하지
않으며 `409 DEPLOYMENT_MUST_BE_DISABLED`를 반환하므로 다음 순서를 사용한다.

```text
PATCH /deployments/{kind}/{deployment_id}/enabled
body: {"enabled": false}
→ 비활성 상태의 원자 저장과 Snapshot Reload 성공 확인
→ DELETE /deployments/{kind}/{deployment_id}
→ 성공 시 204 No Content
```

추가·전체 수정·활성 상태 변경·삭제는 다음 순서로 처리한다.

```text
요청 검증
→ 종류별 Backend 설정 계약 검증
→ 해당 종류 JSON 파일만 원자 교체
→ RegistryManager.try_reload()
→ 성공 시 새 Active Snapshot 사용
→ 실패 시 이전 파일과 Snapshot 복원
```

## 3. 종류별 Deployment 요약 목록

### `GET /deployments/ner`

NER Deployment만 Deployment ID 오름차순으로 반환한다. Request Body는 없다.

```json
{
  "deployments": [
    {
      "deploymentId": "ner-gliner-local",
      "enabled": true
    },
    {
      "deploymentId": "ner-maintenance",
      "enabled": false
    }
  ]
}
```

### `GET /deployments/llm`

LLM Deployment만 Deployment ID 오름차순으로 반환한다. Request Body는 없다.

```json
{
  "deployments": [
    {
      "deploymentId": "llm-local-a",
      "enabled": true
    }
  ]
}
```

목록 항목은 다음 두 필드만 반환한다.

| 필드 | 필수 | 설명 |
|---|---:|---|
| `deploymentId` | O | NER는 `/detect`, LLM은 `/detect`, `/mask`, `/generate` 또는 `/titles`에서 역할에 맞게 선택할 Registry ID |
| `enabled` | O | 현재 실행 요청에서 사용할 수 있는 상태인지 표시 |

해당 종류의 Deployment가 없으면 다음처럼 빈 배열을 반환한다.

```json
{
  "deployments": []
}
```

NER와 LLM을 합친 `GET /deployments` Endpoint와 `kind` Query Parameter는 제공하지
않는다. 호출자는 필요한 종류의 경로를 직접 선택한다.

NER·LLM 종류는 요청 경로로 이미 표현되므로 목록 응답에는 `kind`를 중복해서
포함하지 않는다. 내부 Registry의 `DeploymentConfig.kind`와 경로 종류 검증은
그대로 유지한다.

## 4. Deployment 상세 조회

### `GET /deployments/ner/{deployment_id}`

현재 Snapshot에서 ID와 `kind=ner`가 모두 일치하는 Deployment의 Gateway용 운영
상세 정보를 반환한다. Request Body는 없다.

```json
{
  "deploymentId": "ner-gliner-local",
  "enabled": true,
  "baseUrl": "http://127.0.0.1:8008/v1/ner/detect",
  "timeoutMs": 5000
}
```

### `GET /deployments/llm/{deployment_id}`

현재 Snapshot에서 ID와 `kind=llm`이 모두 일치하는 Deployment의 Gateway용 운영
상세 정보를 반환한다. Request Body는 없다.

```json
{
  "deploymentId": "llm-local-a",
  "enabled": true,
  "adapterType": "openai_compatible",
  "baseUrl": "http://127.0.0.1:11434/v1",
  "modelName": "qwen3:8b",
  "timeoutMs": 30000
}
```

NER 상세 응답은 목록의 `deploymentId`, `enabled`에 필수 `baseUrl`, `timeoutMs`를
추가한다. LLM 상세는 필수 `adapterType`과 설정된 경우 `baseUrl`, `modelName`,
`timeoutMs`를 추가한다. 선택적 LLM 필드가 없으면 `null` 대신 생략한다. 요청
경로에서 종류를 알 수 있으므로 상세 응답에도 `kind`를 포함하지 않는다.

| 필드 | 필수 | 설명 |
|---|---:|---|
| `adapterType` | LLM만 O | 현재 LLM Deployment에 연결된 Backend Adapter 계약 |
| `baseUrl` | NER는 O, LLM은 Adapter별 | NER 전체 POST Endpoint 또는 LLM 서버 URL |
| `modelName` | LLM Adapter별 | LLM 서버용 모델 이름 또는 alias |
| `timeoutMs` | NER는 O, LLM은 Adapter별 | Registry에 설정된 호출 제한 시간 |

IP나 호스트를 별도의 `ip` 또는 `host` 필드로 중복해서 반환하지 않는다. Gateway는
`baseUrl`을 URL로 해석해 host, port와 path를 확인한다.

존재하지 않는 ID뿐 아니라 요청 경로와 Deployment 종류가 다른 경우에도 다음과
같이 반환한다. 예를 들어 `kind=llm`인 ID를
`GET /deployments/ner/{deployment_id}`로 조회하면 같은 404 응답을 사용한다.

```http
HTTP 404
```

```json
{
  "detail": {
    "code": "DEPLOYMENT_NOT_FOUND",
    "message": "요청한 Deployment를 찾을 수 없습니다"
  }
}
```

## 4.1 LLM 컨텍스트 한도 조회

### `GET /deployments/llm/{deployment_id}/limits`

현재 Snapshot의 LLM 설정과 조회 가능한 Ollama Native API 정보를 조합해 실제 사용
가능한 컨텍스트 한도를 반환한다. Request Body는 없다. 비활성 Deployment도 운영
정보 조회 대상으로 사용할 수 있다.

```json
{
  "deploymentId": "llm-local-a",
  "modelContextWindowTokens": 131072,
  "runtimeContextWindowTokens": 16384,
  "effectiveContextWindowTokens": 16384,
  "source": "ollama_runtime"
}
```

| 필드 | 설명 |
|---|---|
| `modelContextWindowTokens` | Ollama `/api/show`에서 확인한 모델 구조상 최대 컨텍스트 |
| `runtimeContextWindowTokens` | Ollama `/api/ps`에서 확인한 현재 로드 모델의 실행 컨텍스트 |
| `effectiveContextWindowTokens` | 모델·실행·Registry 한도 중 가장 작은 값 |
| `source` | 유효 한도를 결정한 `ollama_runtime`, `ollama_model`, `registry`, `unknown` |

Ollama 자동 조회는 OpenAI 호환 `baseUrl`이 `/v1`이고 기본 Ollama port `11434`를
사용하거나 host 이름에 `ollama`가 포함된 경우에만 시도한다. `/api/show`와
`/api/ps` 조회가 실패해도 실행 오류로 바꾸지 않고 Registry 설정값으로 fallback한다.
모든 값을 알 수 없으면 숫자 필드를 `null`, `source="unknown"`으로 반환한다.

존재하지 않거나 NER 종류인 ID는 `404 DEPLOYMENT_NOT_FOUND`를 반환한다.

## 5. Deployment 실제 연결 Probe

Probe API는 Request Body를 받지 않는다. 현재 Active Snapshot에서 요청 경로의
종류와 ID가 일치하는 Deployment를 활성 상태와 관계없이 선택하고, 해당 Backend의
실제 실행 계약을 최소 입력으로 한 번 호출한다.

단순 TCP 연결이나 별도의 Health Endpoint만 확인하는 기능이 아니다. Backend의
요청 직렬화, 실제 모델 서버 호출, 응답 정규화와 공통 결과 검증까지 모두
통과해야 `available`을 반환한다.

Probe는 활성화 전 연결 상태도 확인하는 관리 진단 작업이므로 `enabled=false`인
Deployment에도 실제 요청을 보낸다. 성공해도 Deployment 설정이나 `enabled` 값,
설정 파일과 Active Snapshot을 변경하지 않으며 자동 활성화하지 않는다. 선택한
Deployment의 Provider가 등록되지 않았다면 실제 호출 없이
`503 BACKEND_PROVIDER_NOT_REGISTERED`를 반환한다.

### 5.1 NER Probe

#### `POST /deployments/ner/{deployment_id}/probe`

현재 Active Snapshot에서 `kind=ner`인 Deployment를 활성 상태와 관계없이 찾고,
고정 NER HTTP `detect()` 계약으로 최소 입력 `"A"`를 한 번 전송한다.

```http
POST /deployments/ner/ner-gliner-local/probe
```

고정 요청 직렬화, 응답 JSON 검증, 공통 `Detection` 변환과 원문 Span
검증까지 모두 성공해야 다음 응답을 반환한다.

```json
{
  "deploymentId": "ner-gliner-local",
  "status": "available",
  "latencyMs": 12.345
}
```

| 필드 | 필수 | 설명 |
|---|---:|---|
| `deploymentId` | O | 실제 Probe를 실행한 Deployment ID |
| `status` | O | 전체 요청·응답 계약 통과 시 `available` |
| `latencyMs` | O | Backend 호출과 결과 검증에 걸린 밀리초 |

`latencyMs`는 Snapshot 조회와 Provider 선택 시간을 제외하고, `detect()` 호출
직전부터 공통 결과 검증 완료 시점까지 측정한다. 결과가 빈 배열이어도 정상적인
NER 응답 계약을 지켰다면 성공이다. Probe 입력과 탐지 결과는 응답에 포함하거나
영구 저장하지 않는다.

실제 외부 요청은 등록된 `baseUrl` 전체 Endpoint에 `POST`로
`{"text":"A"}`를 전송한다. LPL은 경로를 덧붙이지 않으며 응답은 정확한
`detections` 배열 계약이어야 한다.

연결 실패, timeout, 잘못된 HTTP 상태나 응답 형식은 `available` 응답으로 바꾸지
않고 기존 공통 Backend HTTP 오류로 반환한다. `/detect`와 같은 의미의 오류
계약을 유지하기 위한 정책이다. NER가 아닌 ID는 존재하지 않는 것처럼 404로
처리한다. `enabled=false`인 NER도 Probe에서는 실제 요청을 보내며, 성공하더라도
해당 Deployment를 자동 활성화하지 않는다.

### 5.2 LLM Probe

#### `POST /deployments/llm/{deployment_id}/probe`

선택한 LLM Backend의 실제 `generate()` 계약으로 다음 공통 입력을 한 번
전달한다.

```json
{
  "messages": [
    {
      "role": "user",
      "content": "A"
    }
  ],
  "parameters": {
    "max_tokens": 1
  },
  "output_schema": null
}
```

현재 `openai_compatible` Adapter가 만드는 실제 요청은 다음과 같다. `model`은
Deployment의 `modelName`을 사용하고 Adapter가 비스트리밍을 강제한다.

```json
{
  "model": "qwen3:8b",
  "messages": [
    {
      "role": "user",
      "content": "A"
    }
  ],
  "max_tokens": 1,
  "stream": false
}
```

```http
POST /deployments/llm/llm-local-a/probe
```

```json
{
  "deploymentId": "llm-local-a",
  "status": "available",
  "latencyMs": 184.321
}
```

출력을 1토큰으로 제한해 Probe 비용을 줄이지만, 실제 모델 생성 요청이므로
서버 설정에 따라 모델을 메모리나 GPU에 로드하고 Cold Start가 발생할 수 있다.
모델 출력, 모델 이름이나 Token Usage는 Probe 응답에 포함하거나 영구 저장하지
않는다. 반환값은 공통 `LlmResult` 계약으로 다시 검증한다.

`max_tokens`는 현재 LPL LLM Probe가 사용하는 공통 제한 파라미터다. 향후 Native
Adapter를 추가하면 해당 Adapter가 이 값을 자신의 출력 토큰 제한 필드로
변환해야 한다. `MockLlmBackend`는 네트워크 호출 없이 공통 `generate()` 계약만
검사한다.

LLM이 아닌 ID는 404로 처리한다. `enabled=false`인 LLM도 Probe에서는 실제 요청을
보내며, 성공하더라도 해당 Deployment를 자동 활성화하지 않는다. 연결 실패,
timeout과 잘못된 모델 응답은 NER와 마찬가지로 기존 공통 Backend HTTP 오류 계약을
사용한다.

## 6. 비활성 Deployment 정책

이 API는 현재 운영 Registry를 관찰하는 카탈로그이므로 `enabled=false`인
Deployment도 목록과 상세 조회에서 제외하지 않는다.

```json
{
  "deploymentId": "ner-maintenance",
  "enabled": false
}
```

`enabled=false`는 조회 금지가 아니라 일반 Runtime 처리 트래픽 금지를 의미한다.
해당 ID를 `POST /detect`, `POST /mask`, `POST /generate` 또는 `POST /titles`에서 선택하면
`409 DEPLOYMENT_DISABLED`가 발생한다. 운영 조회와 관리용 Probe는 허용되며,
Probe는 실제 Backend 요청을 보내지만 설정이나 Active Snapshot을 변경하거나
Deployment를 자동 활성화하지 않는다.

LPL 카탈로그는 사용자별 권한이나 조합 정책을 적용하지 않는다. Gateway가 다음
조건을 기준으로 Frontend에 보여 줄 실제 선택 목록을 필터링해야 한다.

- `enabled=true`
- 인증된 사용자가 접근 가능한 Deployment
- 탐지에는 허용된 NER·LLM 조합
- 생성에는 허용된 LLM

## 7. 운영 설정 노출 경계

Registry의 `DeploymentConfig`를 HTTP 응답으로 그대로 직렬화하지 않는다. 다음
운영 필드는 목록 응답에는 반환하지 않는다.

- `adapterType` (LLM 전용)
- `baseUrl`
- `modelName`
- `timeoutMs`

Gateway가 실제 라우팅 상태를 확인해야 하므로 NER 상세에는 `baseUrl`, `timeoutMs`를
필수로 반환하고 LLM 상세에는 `adapterType`을 필수로 반환하며 `baseUrl`,
`modelName`, `timeoutMs`는 설정된 경우에만 반환한다. API Key, Token, Secret
참조를 포함한 모든 Secret은 상세
응답에서도 항상 제외한다. Secret은 Registry에 평문으로 저장하지 않으며, 추후 Secret
참조 필드가 추가되더라도 카탈로그 응답에는 포함하지 않는다.

Active Snapshot으로 승인되는 모든 `baseUrl`은 query, fragment와 URL userinfo를
포함할 수 없다. 따라서 상세 API는 토큰이나 URL 내 인증정보가 제거된 값을
사후 가공하는 것이 아니라, Registry 검증을 통과한 안전한 `baseUrl`만 반환한다.

이 API는 운영 Endpoint를 노출하므로 인터넷이나 Frontend에 직접 공개하지 않는다.
LPL API는 Gateway 뒤의 신뢰 가능한 내부망으로 제한하고, Gateway만 상세 응답을
사용해 LPL의 현재 설정을 확인한다. Gateway는 사용자에게 보여 줄 목록에는
`deploymentId`와 `enabled`만 사용하며 `adapterType`, `baseUrl`, `modelName`,
`timeoutMs`를 Frontend로 전달하지 않는다.

## 8. 오류 응답

| HTTP | 코드 | 발생 조건 |
|---:|---|---|
| 404 | `DEPLOYMENT_NOT_FOUND` | 상세·수정·활성 상태 변경·삭제·Probe ID가 없거나 경로 종류와 실제 `kind`가 다름 |
| 409 | `DEPLOYMENT_ALREADY_EXISTS` | 추가하려는 ID가 NER 또는 LLM Registry에 이미 존재함 |
| 409 | `DEPLOYMENT_MUST_BE_DISABLED` | 활성 Deployment를 비활성화하지 않고 삭제함 |
| 422 | `REQUEST_VALIDATION_FAILED` | 잘못된 ID, 추가·수정 요청 본문 또는 `enabled` 외 필드·누락·잘못된 타입이 있는 PATCH 본문 |
| 422 | Adapter별 설정 검증 코드 | 추가·수정·활성 상태 변경 후보가 선택한 Adapter 계약과 다름 |
| 500 | Adapter별 설정 오류 코드 | Backend 실행 설정 오류 |
| 500 | `DEPLOYMENT_STORAGE_FAILED` | 추가·수정·활성 상태 변경·삭제 중 종류별 설정 파일 저장소 작업 실패 |
| 500 | `DEPLOYMENT_ROLLBACK_FAILED` | 후보 Snapshot 활성화 실패 후 이전 설정 복원도 실패 |
| 500 | `LLM_PROBE_RESULT_INVALID` | LLM Provider 구현체가 공통 결과 계약을 위반함 |
| 502 | Adapter별 전송·응답 오류 코드 | 모델 서버 연결·HTTP·응답 검증 실패 |
| 502 | `NER_PROBE_RESULT_INVALID` | Adapter 결과가 공통 Detection Span 계약과 다름 |
| 503 | `DEPLOYMENT_ACTIVATION_FAILED` | 추가·수정·활성 상태 변경·삭제의 저장 후보 활성화 실패 후 이전 설정으로 복원됨 |
| 503 | `BACKEND_PROVIDER_NOT_REGISTERED` | Probe 대상의 Adapter 구현체가 준비되지 않음. 비활성 Deployment도 같은 조건 적용 |
| 503 | `REGISTRY_NOT_INITIALIZED` | Active Registry Snapshot이 아직 없음 |
| 503 | `APPLICATION_RUNTIME_UNAVAILABLE` | FastAPI Runtime을 사용할 수 없음 |
| 504 | Adapter별 timeout 코드 | 모델 서버 응답 제한시간 초과 |

요청 검증 오류는 기존 공통 `detail.code`·`detail.message` 형식을 유지하면서 잘못된
필드와 안전한 사유를 `message`에 표시한다. 예를 들어 Ollama 모델 이름 문법을
Deployment ID에 사용하면 다음과 같이 응답한다.

```json
{
  "detail": {
    "code": "REQUEST_VALIDATION_FAILED",
    "message": "요청 검증에 실패했습니다: body.deploymentId: 소문자 또는 숫자로 시작하고 소문자, 숫자, '.', '_', '-'만 사용할 수 있습니다"
  }
}
```

오류 응답은 사용자가 보낸 실제 필드 값, 알 수 없는 추가 필드 이름, 내부 URL,
모델명, 설정값 또는 예외 문자열을 포함하지 않는다. 여러 필드가 잘못된 경우에는
최대 다섯 개까지 `필드: 사유` 형식으로 표시하고 나머지 개수만 안내한다.
