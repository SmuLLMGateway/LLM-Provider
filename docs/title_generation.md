# 대화 제목 생성 API

## 1. 목적과 책임 경계

대화 제목 생성 API는 Gateway가 전달한 첫 사용자 메시지를 고정된 제목 생성
System Prompt와 함께 로컬 LLM으로 처리하여 짧은 제목을 반환한다.

```text
Gateway
→ 첫 사용자 메시지와 llmDeploymentId 전달
→ LPL이 제목 생성
→ Gateway가 반환된 title을 대화방 metadata로 저장
```

LPL은 대화방, 메시지 이력이나 생성된 제목을 저장하지 않는다. 어떤 메시지가 첫
메시지인지 판단하거나 제목 생성 시점을 예약하는 일도 담당하지 않는다. Gateway가
대화의 첫 사용자 메시지를 확보한 뒤 이 API를 호출하고 결과를 영구 저장한다.

이 기능은 Option A 본문 생성용 `POST /generate`와 별개다. `/generate`는 사용자
입력을 그대로 Local LLM에 전달하지만, 제목 생성은 코드에 고정된 System Prompt를
항상 사용하고 결과를 제목 전용 계약으로 다시 검증한다.

## 2. Endpoint

```http
POST /titles
Content-Type: application/json
```

정상 응답 상태는 `200 OK`다. 요청은 동기적으로 처리되며 LPL 내부에 백그라운드
작업이나 대화 상태를 남기지 않는다.

## 3. 요청 계약

요청 예시:

```json
{
  "text": "FastAPI에서 여러 로컬 모델 서버를 선택해 호출하고 싶어요.",
  "llmDeploymentId": "llm-local-a"
}
```

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `text` | string | O | 길이 1 이상 | Gateway가 선택한 대화의 첫 사용자 메시지 |
| `llmDeploymentId` | string | O | `^[a-z0-9][a-z0-9._-]*$` | 제목 생성에 사용할 `kind=llm` Deployment ID |

요청 모델은 알 수 없는 필드를 거부한다. 모델 URL, 모델 이름, Prompt 본문·경로,
Prompt ID, 임의 `messages`, 모델 파라미터와 대화 이력은 받을 수 없다.

LPL은 `text`가 실제 첫 메시지인지 확인할 수 없다. 사용자별 Deployment 접근 정책,
첫 메시지 선택과 제목 생성 호출 시점은 Gateway가 관리한다.

## 4. 실행 흐름

```text
GenerateTitleRequest 검증
→ Active Registry Snapshot 한 번 캡처
→ resolve_title_generation(llmDeploymentId, snapshot)
→ kind=llm, enabled와 Provider 확인
→ 같은 Snapshot의 TitlePromptArtifact 렌더링
→ 고정 System Prompt + 원문 user 메시지로 Backend 호출
→ LlmResult 공통 계약 재검증
→ TitleOutputValidator 검증
→ GenerateTitleResponse 반환
```

`TitleGenerationExecutionPlan`은 다음 두 실행 리소스를 함께 보관한다.

```text
TitleGenerationExecutionPlan
├─ llm_deployment
└─ title_prompt
```

Pipeline은 실행 계획을 조립한 뒤 Snapshot이나 파일을 다시 조회하지 않는다.
요청 처리 중 Deployment Reload가 성공해도 진행 중 요청은 처음 캡처한 Deployment와
Prompt를 끝까지 사용한다.

## 5. 고정 제목 생성 Prompt

물리 파일은 다음과 같다.

```text
config/title_prompt.j2
```

이 파일은 외부 변수를 참조하지 않는 정적 Jinja2 템플릿이다.
`TitlePromptArtifact`가 시작 시 본문, UTF-8, 크기, Jinja2 문법과 무변수 계약을
검증하고 컴파일한다. Runtime 요청이나 Registry 설정으로 Prompt를 선택하거나
변경할 수 없다.

Backend에는 다음 공통 메시지를 전달한다.

```python
messages = [
    {
        "role": "system",
        "content": plan.title_prompt.render(),
    },
    {
        "role": "user",
        "content": text,
    },
]
parameters = {
    "max_tokens": 32,
    "temperature": 0,
    "reasoning_effort": "none",
}
output_schema = None
```

제목 생성은 짧고 결정적인 결과가 필요한 작업이므로 sampling을 줄이고 reasoning을
비활성화한다. 특히 reasoning 모델이 `max_tokens=32`를 사고 과정에 모두 사용해
실제 제목 본문을 비워 두는 상황을 막는다. 제목 생성에 사용하는 LLM Adapter는
위 공통 파라미터를 서버의 네이티브 설정으로 변환하거나 그대로 지원해야 한다.

사용자 원문을 Jinja2 템플릿에 삽입하지 않고 별도 `user` 메시지로 전달한다.
고정 Prompt는 입력과 같은 언어, 한 줄 30자 이하, 제목만 출력, 민감한 고유값의
일반화를 요구한다. 주제를 판단할 수 없을 때의 출력은 `새 대화`다.

Prompt는 읽기 전용 배포 리소스다. 변경하려면 새 산출물을 배포하고 프로세스를
재시작해야 하며, Deployment 설정 Reload로는 변경되지 않는다.

## 6. 성공 응답

```json
{
  "title": "로컬 모델 서버 선택"
}
```

| 필드 | JSON 타입 | 필수 | 제약 | 설명 |
|---|---|---:|---|---|
| `title` | string | O | 한 줄, 1~30자, UTF-8 120 bytes 이하 | 검증을 마친 대화 제목 |

응답에는 모델 이름, Usage, Prompt 내용이나 Deployment 설정을 포함하지 않는다.
Gateway는 `title`만 대화방 metadata에 저장한다.

## 7. 모델 출력 검증

LLM 출력은 비신뢰 데이터로 처리한다. `TitleOutputValidator`는 앞뒤 공백을 제거한
뒤 다음 조건을 검증한다.

- 빈 문자열이 아님
- 30자 이하
- UTF-8 120 bytes 이하
- 줄바꿈과 Unicode 줄 구분자가 없는 한 줄
- 제어 문자가 없음
- `title:` 또는 `제목:` 접두사가 없음
- 따옴표, 백틱, Markdown heading, JSON 객체·배열 모양이 아님

검증에 실패한 모델 출력 원문은 오류 응답에 포함하지 않는다. 잘못된 제목을
사용자 입력 일부로 대체하거나 자동 저장하지도 않는다. 호출 실패 시 대체 제목을
사용할지는 제목 저장을 소유한 Gateway가 결정한다.

## 8. 오류 응답

오류 응답은 다른 LPL API와 같은 형식이다.

```json
{
  "detail": {
    "code": "TITLE_OUTPUT_INVALID",
    "message": "모델 서버 호출 또는 응답 처리에 실패했습니다"
  }
}
```

| HTTP 상태 | 코드 | 조건 |
|---:|---|---|
| 422 | `REQUEST_VALIDATION_FAILED` | 요청 필드 누락, 잘못된 타입·ID 또는 알 수 없는 필드 |
| 404 | `DEPLOYMENT_NOT_FOUND` | 요청한 LLM Deployment가 없음 |
| 409 | `DEPLOYMENT_DISABLED` | 요청한 LLM Deployment가 비활성화됨 |
| 422 | `DEPLOYMENT_KIND_MISMATCH` | NER Deployment를 제목 생성용으로 요청함 |
| 503 | `REGISTRY_NOT_INITIALIZED` | Registry가 초기화되지 않음 |
| 503 | `APPLICATION_RUNTIME_UNAVAILABLE` | FastAPI Runtime을 사용할 수 없음 |
| 503 | `BACKEND_PROVIDER_NOT_REGISTERED` | 필요한 LLM Provider가 등록되지 않음 |
| 503 | `BACKEND_PROVIDER_KIND_MISMATCH` | Deployment와 Provider 종류가 다름 |
| 500 | `TITLE_PROMPT_RENDER_FAILED` | 고정 제목 Prompt 렌더링 실패 |
| 500 | `TITLE_BACKEND_RESULT_INVALID` | Backend가 공통 `LlmResult` 계약을 위반함 |
| 502 | `TITLE_OUTPUT_INVALID` | LLM 출력이 제목 계약을 위반함 |

Backend Adapter의 설정·timeout·전송·응답 오류는 기존 공통 전역 exception
handler가 각각 500, 504 또는 502로 변환한다. API 오류에는 사용자 `text`, 모델
출력, Prompt 본문과 내부 예외 상세를 복사하지 않는다.

## 9. Gateway 연동 원칙

- Gateway는 첫 사용자 메시지를 받은 뒤 제목 생성 API를 호출한다.
- Gateway는 사용자별로 허용된 `llmDeploymentId`만 선택한다.
- LPL은 제목 생성 결과를 저장하거나 대화 ID와 연결하지 않는다.
- Gateway가 성공 응답의 `title`을 대화방 metadata로 저장한다.
- 재시도, 비동기 작업 큐, 대체 제목과 제목 재생성 정책은 Gateway가 결정한다.
- 제목에는 민감정보가 노출될 수 있으므로 Gateway도 Frontend 표시와 로그를
  일반 대화 내용과 같은 민감도로 취급한다.

## 10. 현재 제한사항

- 첫 사용자 메시지 하나만 입력으로 받으며 Assistant 응답이나 전체 대화 이력은
  사용하지 않는다.
- 제목 생성은 스트리밍하지 않는다.
- 요청별 Prompt, 출력 길이와 모델 파라미터를 지정할 수 없다.
- LPL은 제목 목록, 제목 수정·삭제와 대화방 관리 API를 제공하지 않는다.
- API 자체의 사용자 인증·인가와 사용자별 Deployment 정책은 Gateway 책임이다.
