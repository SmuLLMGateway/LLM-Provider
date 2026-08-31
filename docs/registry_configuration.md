# Registry 설정 및 파일 형식

## 1. 목적

Registry는 LPL이 호출할 수 있는 NER·LLM Deployment를 정의한다. 탐지와 생성에
사용할 조합은 Registry에 미리 저장하지 않는다. Gateway가 요청마다 실행할
Deployment ID를 직접 전달하고, LPL은 같은 활성 Snapshot에서 해당 ID를 검증한다.

```text
POST /detect
├─ nerDeploymentId
└─ llmDeploymentId

POST /mask
└─ llmDeploymentId

POST /generate
└─ llmDeploymentId

GET /deployments/ner
└─ NER 운영 요약 목록

GET /deployments/ner/{deployment_id}
└─ 단일 NER Deployment Gateway용 운영 상세

POST /deployments/ner
└─ 새 NER Deployment 추가·저장·활성화

PUT /deployments/ner/{deployment_id}
└─ 기존 NER Deployment 전체 설정 교체·저장·활성화

PATCH /deployments/ner/{deployment_id}/enabled
└─ 기존 NER Deployment 활성 상태만 변경·저장·활성화

DELETE /deployments/ner/{deployment_id}
└─ 비활성 NER Deployment 삭제·저장·활성화

GET /deployments/llm
└─ LLM 운영 요약 목록

GET /deployments/llm/{deployment_id}
└─ 단일 LLM Deployment Gateway용 운영 상세

GET /deployments/llm/{deployment_id}/limits
└─ Registry와 조회 가능한 모델 서버의 컨텍스트 한도

POST /deployments/llm
└─ 새 LLM Deployment 추가·저장·활성화

PUT /deployments/llm/{deployment_id}
└─ 기존 LLM Deployment 전체 설정 교체·저장·활성화

PATCH /deployments/llm/{deployment_id}/enabled
└─ 기존 LLM Deployment 활성 상태만 변경·저장·활성화

DELETE /deployments/llm/{deployment_id}
└─ 비활성 LLM Deployment 삭제·저장·활성화
```

`/detect`, `/mask`, `/generate`, `/titles` 같은 실행 요청에서는 모델 URL,
`adapterType`, 모델 이름이나 Secret을 받지 않고 Registry에 등록된 ID만
전달받는다. 반면 Deployment 관리 API는 Registry 설정을 추가·교체하기 위해
NER에서는 `baseUrl`, `timeoutMs`를 받고 LLM에서는 `adapterType`과 Adapter별 실행
필드를 받는다. 전용 PATCH는 `enabled` boolean 하나만 받고 DELETE는 Request
Body를 받지 않는다. 어느 관리 요청도 Secret은 받지 않는다.

Deployment ID 직접 선택은 LPL을 공개 API로 노출한다는 뜻이 아니다. LPL은 Gateway
뒤의 내부 서비스로 제한하고, 사용자별 Deployment 허용 목록과 조합 정책은
Gateway가 인증 정보에 따라 검사해야 한다.

## 2. 저장 파일과 활성 Snapshot

NER와 LLM Deployment는 종류별 JSON 파일로 분리한다. 같은 설정 루트에는 고정
탐지·마스킹·제목 Prompt 물리 파일도 둔다.

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

이전 단일 파일 `deployments.json`이 남아 있으면 애플리케이션은 이를 조용히
무시하지 않고 시작 또는 Reload를 거부한다. 기존 파일의 `kind=ner` 항목은
`ner_deployments.json`, `kind=llm` 항목은 `llm_deployments.json`으로 옮긴 뒤
새 종류별 파일의 각 항목에서 `kind`를 제거하고 단일 파일을 삭제해야 한다.

이전 버전의 `profiles.json`은 더 이상 읽지 않는다. 기존 배포 디렉터리에서
마이그레이션할 때는 운영자가 이 파일을 삭제해야 하며, 남겨 두더라도 실행
설정에는 반영되지 않는다.

역할별 고정 Prompt는 Registry 리소스가 아니며 Prompt ID metadata로 등록하지 않는다.
`policy_prompts.json`도 요청에서 선택하는 Registry가 아니라 코드에 고정된 14개
Policy ID의 읽기 전용 본문 Catalog다.
물리 파일 `config/prompts.j2`, `config/policy_prompts.json`,
`config/mask_prompt.j2`와 `config/title_prompt.j2`는 첫 정상 Snapshot을 만들 때
한 번씩 읽고 검증·컴파일한다. Builder는 결과 Artifact와 정책 Catalog를 프로세스
수명 동안 캐시한다.

`policy_settings.json`도 Deployment Registry에는 병합하지 않는다. 별도
`PolicySettingsManager`가 원자 저장과 활성 불변 설정 참조를 관리하며 자세한 계약은
[활성 정책 설정](./policies.md)을 따른다.

```text
ner_deployments.json ────────────┐
llm_deployments.json ────────────┤
config/prompts.j2 ───────────────┼─ RegistrySnapshotBuilder 첫 build()
config/policy_prompts.json ──────┤
config/mask_prompt.j2 ───────────┤
config/title_prompt.j2 ──────────┘
                                         │
                                         ▼
                         역할별 PromptArtifact + PolicyPromptCatalog 캐시
                         + 통합 ActiveRegistrySnapshot
                                         │
                    이후 Reload는 종류별 Deployment JSON만 갱신
```

`RegistryConfig`는 두 파일의 Deployment를 하나의 맵으로 조립한 객체이고,
`ActiveRegistrySnapshot`은 요청이 실제로 사용하는 불변 실행 상태다.

## 3. 빈 파일 초기화

```python
from pathlib import Path

from app.registry import RegistryFileStore

RegistryFileStore(Path("config")).initialize()
```

초기화는 다음 파일 두 개를 각각 빈 객체로 생성한다.

```json
{}
```

```text
config/ner_deployments.json
config/llm_deployments.json
```

두 파일 중 하나라도 기존에 있으면 아무 파일도 덮어쓰지 않고
`FileExistsError`를 발생시킨다. 생성 도중 실패하면 이번 초기화에서 먼저 만든
파일도 제거한다.

저장소에서 제공하는 기본 파일도 각각 `{}`다. 빈 Registry는 유효하며 애플리케이션은
정상적으로 시작하고 종류별 목록 API에서 빈 배열을 반환한다. Detect, Generate와
Title 실행 전에는 관리 API 또는 오프라인 정비 도구로 필요한 Deployment를 먼저
등록해야 한다. 다음 절의 JSON은 초기값이 아니라 등록 형식 예시다.

## 4. 종류별 Deployment JSON

두 파일 모두 최상위 값은 별도 래퍼가 없는 `Deployment ID → 설정` 객체 맵이다.
각 설정 객체에는 `kind`를 저장하지 않는다. 파일명이 종류를 결정하며
`RegistryFileStore`가 로드할 때 각각 `ner` 또는 `llm`을 내부
`DeploymentConfig.kind`로 주입한다. 명시적인 `kind` 필드는 파일명과 값이
일치하더라도 거부한다.

`config/ner_deployments.json` 예시:

```json
{
  "ner-http-a": {
    "baseUrl": "http://192.168.0.20:9000/v1/ner/detect",
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
    "baseUrl": "http://127.0.0.1:11434/v1",
    "modelName": "qwen3:8b",
    "timeoutMs": 30000,
    "contextWindowTokens": 32768,
    "enabled": true
  }
}
```

어느 파일에도 `deployments` 래퍼를 다시 넣지 않는다. 두 파일에 같은
Deployment ID를 등록할 수도 없다. `RegistryFileStore`는 교집합을 명시적으로
검사하여 ID가 겹치면 덮어쓰지 않고
`DuplicateDeploymentIdAcrossFilesError`로 전체 구성을 거부한다.

| 필드 | 필수 | 설명 |
|---|---:|---|
| `adapterType` | LLM만 O | LLM Backend 설정 계약과 Provider를 선택하는 키 |
| `enabled` | O | 요청에서 선택 가능한 상태인지 표시 |
| `baseUrl` | NER는 O, LLM은 Adapter별 | NER 전체 POST Endpoint 또는 LLM 기본 URL |
| `modelName` | LLM Adapter별 | 모델 서버가 사용하는 모델 이름 또는 alias |
| `timeoutMs` | NER는 O, LLM은 Adapter별 | 1~300,000ms 호출 제한 시간 |
| `contextWindowTokens` | LLM만 X | 관리자가 지정한 전체 컨텍스트 토큰 한도 |

Deployment ID와 LLM `adapterType`은 소문자 영문자로 시작하고 소문자, 숫자, `.`,
`_`, `-`만 사용할 수 있다. 알 수 없는 필드는 오류로 처리한다. `baseUrl`은 유효한
HTTP(S) URL이어야 한다. 모든 `baseUrl`에는 query, fragment 또는
사용자명·비밀번호 형태의 userinfo를 포함할 수 없다. 인증정보는 URL이나 Registry
평문 대신 별도의 Secret 전달 계약으로 관리해야 한다.

NER는 단일 표준 HTTP 계약을 사용하고 LLM 필수·금지 필드는 Adapter별로 다르다.
기본 설정 계약은 다음과 같다.

| 종류 또는 `(kind, adapterType)` | 필수 필드 | 금지 필드 |
|---|---|---|
| `ner` 표준 HTTP | `baseUrl`, `timeoutMs` | `adapterType`, `modelName`, `contextWindowTokens` |
| `(llm, mock)` | 없음 | `baseUrl`, `modelName`, `timeoutMs` |
| `(llm, openai_compatible)` | `baseUrl`, `modelName`, `timeoutMs` | 없음 |

제거된 필드를 포함해 지원하지 않는 추가 필드는 API 요청에서 422로, Registry
로드에서 설정 오류로 거부한다.

NER `baseUrl`은 호출 path까지 포함한 전체 POST Endpoint URL이다. LPL은 경로를
붙이지 않고 `POST {baseUrl}`에 `{"text":"..."}`를 보내고 정확한
`{"detections":[...]}` 응답을 받는다. NER `baseUrl`에는 path를 허용하지만 query,
fragment와 URL userinfo는 금지한다. 외부 NER 서버의 공통 요청·응답 형식은
[Backend 실행 구조](./backends.md#9-표준-http-ner-backend)를 따른다.

이전 `gliner_http`, `hf_inference_token_classification`, NER `mock` 형식은 더 이상
지원하는 Registry 계약이 아니다. 기존 서버는 표준 API를 제공하도록 수정하거나
앞단 Wrapper에서 표준 요청·응답으로 변환해야 한다. 새 Adapter 등록 확장은 LLM에만
적용하며 같은 `(llm, adapterType)`으로 설정 계약과 실제 Provider를 함께 등록한다.

## 5. 검증 단계

최초 후보 Snapshot은 다음 순서로 검증한다.

```text
ner_deployments.json → load_strict_json()
→ NerDeploymentRegistryFile Pydantic 검증 → 내부 kind=ner 주입
llm_deployments.json → load_strict_json()
→ LlmDeploymentRegistryFile Pydantic 검증 → 내부 kind=llm 주입
→ 두 파일 간 Deployment ID 중복 검사
→ RegistryConfig 조립
→ RegistryValidator의 Adapter 설정 계약 검증
→ 활성 Deployment의 Provider coverage 검증
→ config/prompts.j2, config/policy_prompts.json, config/mask_prompt.j2와 config/title_prompt.j2 로드·검증·컴파일
→ Snapshot ID 계산
→ ActiveRegistrySnapshot 후보 반환
```

`enabled=false`인 Deployment도 파일에 저장할 수 있다. `/detect`, `/mask`,
`/generate`, `/titles` 같은 일반 Runtime 요청에서는 실행할 수 없지만 관리용 Probe에서는
활성화 전 연결 상태를 확인하기 위해 실제 최소 요청의 대상으로 사용할 수 있다.
활성 Deployment에는 실제 Provider가 등록되어 있어야 한다. JSON, Adapter 계약,
Provider coverage 또는 고정 Prompt 중 하나라도 잘못되면 최초 후보 전체를
거부한다. 이후 Deployment 설정 Reload에서는 JSON과 Deployment 계약만 다시
검증하고 첫 build가 캐시한 Prompt Artifact를 재사용한다.

비활성 Deployment는 Provider 없이도 저장할 수 있으므로 해당 Provider가 등록되지
않은 상태에서 Probe하면 `503 BACKEND_PROVIDER_NOT_REGISTERED`를 반환한다. Probe는
설정 파일, `enabled` 값이나 Active Snapshot을 변경하지 않고 자동 활성화하지
않는다.

`enabled=false`인 항목도 Deployment 운영 카탈로그 조회에는 포함되며
`enabled: false`로 표시된다. 카탈로그 조회와 실행 가능 여부는 별개다.

## 6. 고정 Prompt 계약

후속 LLM 탐지는 다음 고정 파일을 사용한다.

```text
물리 파일: config/prompts.j2
코드 상수 파일명: prompts.j2
외부 변수: text, existing_detections

정책 세부 지침: config/policy_prompts.json
정책 키: P01~P08, S01~S03, B01~B03 정확히 14개
```

`application_runtime()`이 `config_dir/prompts.j2`를 자동 사용한다. Registry나
요청에서 경로를 주입하거나 고르지 않으며 Prompt ID, Prompt Registry, 요청별
Prompt 선택 필드도 없다. Deployment가 하나도 없어도 Builder는 이 단일 물리
파일과 정책 Prompt JSON을 첫 build에서 검증·컴파일해 Snapshot의
`detection_prompt`에 보관하고 이후 동일 Artifact를 재사용한다.
`PromptArtifact.compile()`은 외부 변수가 정확히
문자열 `text`와 `existing_detections` 두 개뿐인지 직접 확인한다. Generation은
이 Artifact를 사용하지 않고 요청
`text`를 그대로 LLM에 전달한다. 애플리케이션은 Prompt 생성·수정·삭제 API나
서비스를 제공하지 않으며, 파일은 읽기 전용 배포 리소스로 취급한다. 변경 적용은
새 파일 배포와 프로세스 재시작으로만 가능하다.

마스킹은 다음 고정 파일을 사용한다.

```text
물리 파일: config/mask_prompt.j2
코드 상수 파일명: mask_prompt.j2
외부 변수: 없음
```

`MaskPromptArtifact`는 외부 Jinja 변수를 허용하지 않는 정적 System Prompt로
컴파일되어 Snapshot의 `mask_prompt`에 보관된다. 검증된 원문, 겹친 Detection span의
합집합 component, `types`·`sources` 근거와 서버 생성 placeholder namespace는
Jinja Context가 아닌 별도 canonical JSON `user` 메시지로 전달한다. 이 JSON에는
Prompt 렌더링 Context 상한과 독립된 UTF-8 byte·항목 수 제한을 적용한다.

Masking LLM은 `maskedText`와 component `targetId`별 같은 대상의 요청 범위
`entityId` assignment를 출력하지만 원문 span·type·source나 replacement
placeholder 필드는 작성하지 않는다. LPL이 응답의
`start`, `end`, `types`, `sources`, request-scoped `entityId`와 placeholder를
검증된 입력에서 파생한다. 자세한 계약은 [Masking API](./masking.md)를 따른다.

## 7. 오프라인 Registry 편집

`OfflineRegistryEditor`는 LPL 프로세스가 완전히 중지된 상태의 초기 설정,
마이그레이션과 수동 정비에만 사용한다. 다음 작업은 Registry 파일만 변경하며
Active Snapshot Reload나 활성화 실패 Rollback을 수행하지 않는다.

```text
OfflineRegistryEditor 메서드:
  add_deployment()
  update_deployment()

모듈 편의 함수:
  add_deployment_offline()
  update_deployment_offline()
```

예시:

```python
from app.registry import OfflineRegistryEditor, RegistryFileStore

editor = OfflineRegistryEditor(RegistryFileStore("config"))
editor.add_deployment(
    "llm-local-a",
    {
        "kind": "llm",
        "adapterType": "openai_compatible",
        "baseUrl": "http://127.0.0.1:11434/v1",
        "modelName": "qwen3:8b",
        "timeoutMs": 30000,
        "contextWindowTokens": 32768,
        "enabled": True,
    },
)
```

이 예시는 물리 파일이나 HTTP 요청이 아니라 오프라인 Editor를 직접 호출하므로
완성된 `DeploymentConfig`를 만들기 위한 `kind`가 필요하다. 파일 로드와 관리 HTTP
API에서는 각각 파일명과 요청 경로가 내부 `kind`를 주입한다.

Offline Editor는 입력 ID와 Pydantic 구조를 확인한 뒤 `RegistryMutator`로 새
`RegistryConfig`를 만들고 Store 트랜잭션에서 저장한다. 저장은 같은 디렉터리의
임시 파일을 완성한 뒤 변경한 종류의 JSON만 원자 교체한다. 한 Store 갱신에서
NER와 LLM을 동시에 변경하는 작업은 `RegistryMultiKindUpdateError`로 거부한다.
따라서 한 관리 요청은 한 종류의 파일만 바꾸며, 다른 종류 파일은 다시 쓰지 않는다.

파일 저장 성공이 활성 Snapshot 변경을 뜻하지는 않는다. Offline Editor를 실행
중인 LPL과 함께 사용하면 파일과 Active Snapshot이 서로 달라질 수 있으므로 반드시
서비스를 먼저 중지한다. 저장한 설정은 다음 애플리케이션 시작 시 전체 검증을
통과해야 활성화된다.

실제 HTTP 관리 API는 Offline Editor를 호출하지 않는다.
`DeploymentManagementService`가 Store 트랜잭션에서 `RegistryMutator`를 적용하고
저장·Reload·활성화 실패 복원을 모두 소유한다.

## 8. Snapshot과 Deployment 설정 Reload

`RegistrySnapshotBuilder`는 Deployment 설정과 캐시한 Prompt Artifact의
`content_hash`를 canonical JSON으로 직렬화하고 SHA-256 `snapshot_id`를 계산한다.

```text
RegistryManager.try_reload()
→ Lock 밖에서 종류별 JSON 두 개를 읽어 통합 후보 Snapshot 생성
→ 첫 build가 캐시한 동일한 탐지·마스킹·제목 PromptArtifact 재사용
→ 후보 전체 검증 성공
→ 짧은 state lock으로 활성 Snapshot 포인터 교체
```

Reload는 Deployment 설정 변경만 반영하며 Prompt 파일을 읽거나 컴파일하지 않는다.
실패하면 마지막 정상 Snapshot을 유지한다. 최초 `initialize()`가 실패하면 대체할
정상 Snapshot이 없으므로 애플리케이션 시작도 실패한다.

요청은 시작 시 `RegistryManager.capture()`를 한 번 호출한다. 이후 같은 Snapshot의
Deployment와 Prompt Artifact로 실행 계획을 조립하므로 처리 중 Deployment 설정
Reload가 성공해도 기존 요청에 새 설정이 섞이지 않는다. Prompt Artifact는 배포된
고정 리소스에서 첫 build 시 만들어진 동일 객체를 재사용한다.

## 9. Deployment 해석

`DeploymentResolver`는 일반 Runtime 실행 계획을 만들며 Store나 파일에 접근하지
않고 전달받은 Snapshot만 사용한다.

탐지:

```text
resolve_detection(ner_deployment_id, llm_deployment_id, snapshot)
├─ NER ID 존재·enabled·kind=ner 검증
├─ LLM ID 존재·enabled·kind=llm 검증
└─ 두 ResolvedDeployment + detection_prompt
   → DetectionExecutionPlan
```

생성:

```text
resolve_generation(llm_deployment_id, snapshot)
└─ LLM ID 존재·enabled·kind=llm 검증
   → GenerationExecutionPlan
```

마스킹:

```text
resolve_masking(llm_deployment_id, snapshot)
├─ LLM ID 존재·enabled·kind=llm 검증
└─ ResolvedDeployment + mask_prompt
   → MaskingExecutionPlan
```

Resolver 오류의 HTTP 매핑은 다음과 같다.

| HTTP 상태 | 코드 | 조건 |
|---:|---|---|
| 404 | `DEPLOYMENT_NOT_FOUND` | 요청한 ID가 Snapshot에 없음 |
| 409 | `DEPLOYMENT_DISABLED` | 요청한 Deployment가 비활성화됨 |
| 422 | `DEPLOYMENT_KIND_MISMATCH` | 요청 역할과 Deployment `kind`가 다름 |

오류가 발생하면 다른 Deployment로 대체하거나 Backend를 실행하지 않는다.

위 활성 상태 검증과 오류 매핑은 `/detect`, `/mask`, `/generate`, `/titles`에 적용된다.
관리용 Probe는 별도 조회 정책으로 `enabled=false`인 Deployment도 실제 호출하지만
설정이나 Snapshot을 변경하거나 자동 활성화하지 않는다. Provider가 등록되지 않은
경우에는 `503 BACKEND_PROVIDER_NOT_REGISTERED`를 반환한다.

## 10. Lock과 파일 일관성

`RegistryFileStore`와 `RegistrySnapshotBuilder`는 같은
`RegistryFileCoordinator`의 `RLock`을 사용한다.

Lock을 잡는 범위:

- `ner_deployments.json`과 `llm_deployments.json`을 함께 읽어 통합 설정 조립
- 변경한 한 종류의 Deployment JSON 원자 교체
- 첫 후보를 만드는 동안 고정 Prompt 로드·검증·컴파일

활성 Snapshot의 조회와 교체는 별도의 짧은 state lock을 사용한다. 파일 I/O, Prompt
컴파일 또는 Backend 호출 중에는 state lock을 잡지 않는다.

이 Lock은 같은 Python 프로세스 내부의 작업만 조율한다. 외부 편집기나 다른
프로세스의 직접 쓰기까지 막는 OS 파일 잠금은 아니다.

## 11. Deployment 카탈로그와 운영 상세

현재 Active Snapshot의 운영 요약과 Gateway용 운영 상세는 다음 읽기 전용 API에서
조회한다.

```text
GET /deployments/ner
GET /deployments/ner/{deployment_id}
GET /deployments/llm
GET /deployments/llm/{deployment_id}
```

각 목록은 경로와 `kind`가 일치하는 Deployment를 ID 순으로 반환하고 비활성
항목도 포함한다. 통합 목록 Endpoint와 `kind` Query Parameter는 제공하지 않는다.
목록에는 `deploymentId`, `enabled`만 포함한다. NER 상세에는 필수 `baseUrl`,
`timeoutMs`를 추가하고 LLM 상세에는 필수 `adapterType`과 설정된 경우 `baseUrl`,
`modelName`, `timeoutMs`를 추가한다. 선택적 LLM 값이 없으면 `null` 대신 필드를
생략한다. Secret은 상세에서도 반환하지 않는다.

상세 경로와 실제 Deployment의 `kind`가 다르거나 ID가 존재하지 않으면 모두
`404 DEPLOYMENT_NOT_FOUND`를 반환한다.

NER·LLM 종류는 요청 경로로 이미 구분되므로 목록·상세 응답에 `kind`를
중복해서 반환하지 않는다. 내부 Registry의 `DeploymentConfig.kind`와 이를
사용하는 설정 검증, 상세 경로 종류 검증 및 실행 요청 역할 검증은 유지한다.

이 카탈로그는 LPL 운영 Registry 전체를 보여 주며 사용자별 접근 제어를 하지 않는다.
Gateway는 `enabled` 상태와 사용자별 허용 정책을 적용해 실제 선택 가능한 NER·LLM
목록만 Frontend에 제공한다. URL의 IP나 호스트는 별도 필드로 두지 않고
`baseUrl`에서 확인한다. LPL API는 Gateway 뒤의 신뢰 가능한 내부망으로 제한하고,
Gateway는 상세 응답의 `baseUrl`, `timeoutMs`와 LLM의 `adapterType`, `modelName`을
Frontend로 전달하지 않는다. 자세한 HTTP 계약은
[Deployment 카탈로그 API](./deployments.md)를 따른다.

## 12. Deployment 추가·수정·활성 상태 변경·삭제 API

관리 API는 경로로 Deployment 종류를 고정한다.

```text
POST /deployments/ner
POST /deployments/llm
→ 본문의 deploymentId로 새 Deployment 추가
→ 파일 저장과 Active Snapshot Reload 성공 시 201

PUT /deployments/ner/{deployment_id}
PUT /deployments/llm/{deployment_id}
→ 기존 Deployment 설정 전체 교체
→ 파일 저장과 Active Snapshot Reload 성공 시 200

PATCH /deployments/ner/{deployment_id}/enabled
PATCH /deployments/llm/{deployment_id}/enabled
→ 기존 Deployment의 enabled만 변경
→ 파일 저장과 Active Snapshot Reload 성공 시 200

DELETE /deployments/ner/{deployment_id}
DELETE /deployments/llm/{deployment_id}
→ 비활성 Deployment 삭제
→ 파일 저장과 Active Snapshot Reload 성공 시 본문 없는 204
```

NER POST 본문에는 `deploymentId`, `baseUrl`, `timeoutMs`, `enabled`를 넣고 PUT은
`deploymentId`를 제외한 나머지 세 필드를 넣는다. NER 요청에는 `adapterType`과
`modelName`을 허용하지 않는다. LLM POST 본문에는 `deploymentId`, `adapterType`,
`enabled`와 Adapter가 요구하는 선택 필드를 넣고 PUT은 ID를 제외한 같은 전체
설정을 받는다. 두 요청 모두 `kind`는 받지 않고 경로의 `ner` 또는 `llm`을 내부
설정에 결합한다.
HTTP JSON 필드명은 camelCase만 허용하며 `deployment_id`, `adapter_type`,
`base_url` 같은 Python snake_case 이름은 422로 거부한다.

PUT은 PATCH가 아니라 전체 교체다. 기존에 설정된 선택 필드라도 새 본문에서
생략하면 제거되므로 유지하려면 다시 전달해야 한다. 기존 Deployment를 다른
종류로 이동하는 용도로 사용할 수 없으며, 대상이 없거나 경로 종류와 다르면
`404 DEPLOYMENT_NOT_FOUND`를 반환한다. POST ID도 두 종류별 파일을 합친 전체
Registry에서 고유해야 한다.

활성 상태 전용 PATCH는 다음 Request Body만 허용한다.

```json
{
  "enabled": true
}
```

`enabled`는 필수 boolean이며 문자열 `"true"`, 누락, `deploymentId`나 실행 설정을
포함한 추가 필드는 `422 REQUEST_VALIDATION_FAILED`로 거부한다. PATCH는 기존 종류별
실행 설정을 그대로 보존하고 성공 시
`200 OK`와 변경된 `DeploymentDetail`을 반환한다. 존재하지 않거나 경로 종류와
다른 ID는 `404 DEPLOYMENT_NOT_FOUND`다. 같은 값을 반복 요청해도 성공하는 멱등
연산이며, 저장 내용이 동일해 Reload가 `NO_CHANGE`를 반환해도 정상으로 처리한다.

DELETE는 Request Body 없이 `enabled=false`인 Deployment만 제거한다. 활성 항목은
자동으로 비활성화하지 않고 `409 DEPLOYMENT_MUST_BE_DISABLED`로 거부한다. 대상이
없거나 실제 종류와 경로 종류가 다르면 `404 DEPLOYMENT_NOT_FOUND`다. 성공하면
응답 본문 없이 `204 No Content`를 반환한다.

`DeploymentManagementService`의 처리 순서는 다음과 같다.

```text
현재 통합 Registry 캡처
→ DeploymentManagementService가 RegistryMutator로
   추가, 전체 교체, enabled 변경 또는 비활성 항목 삭제
→ 요청 kind의 JSON만 원자 저장
→ RegistryManager.try_reload()
→ 성공한 통합 후보만 Active Snapshot으로 교체
```

후보 활성화가 실패하면 저장 전에 캡처한 같은 종류의 Deployment 맵만 복원하고
반대 종류 파일은 덮어쓰지 않은 채 다시 Reload한다. 정상 원복까지 끝나면 API는
`503 DEPLOYMENT_ACTIVATION_FAILED`를 반환하고 기존 Active Snapshot을 계속
사용한다. 파일 또는 Snapshot 원복까지 실패하면
`500 DEPLOYMENT_ROLLBACK_FAILED`를 반환하므로 운영자 확인이 필요하다.

관리 API는 Registry 설정만 변경한다. 모델 다운로드, 모델 서버 시작·중지와 실제
연결 확인은 수행하지 않으며, 연결 확인은 별도 Probe API를 사용한다.
Probe는 활성화 전 설정도 점검할 수 있도록 `enabled=false`인 Deployment를
허용하지만, 결과와 관계없이 설정을 수정하거나 자동 활성화하지 않는다.

따라서 새 Deployment는 다음 순서로 안전하게 등록한다.

```text
enabled=false로 POST 등록
→ POST /deployments/{kind}/{deployment_id}/probe
→ Probe 성공 시 PATCH /deployments/{kind}/{deployment_id}/enabled
   body: {"enabled": true}
```

Deployment를 삭제할 때는 다음 순서로 활성 요청의 실수나 경합을 방지한다.

```text
PATCH /deployments/{kind}/{deployment_id}/enabled
body: {"enabled": false}
→ 비활성 상태의 원자 저장과 Snapshot Reload 성공 확인
→ DELETE /deployments/{kind}/{deployment_id}
→ 성공 시 204 No Content
```

PATCH와 DELETE도 POST·PUT과 같은 종류별 원자 저장, Snapshot Reload와 복원 정책을
사용한다.
후보 활성화 실패 후 정상 복원되면 `503 DEPLOYMENT_ACTIVATION_FAILED`, Registry가
준비되지 않았으면 `503 REGISTRY_NOT_INITIALIZED`, 저장 실패는
`500 DEPLOYMENT_STORAGE_FAILED`, 복원까지 실패하면
`500 DEPLOYMENT_ROLLBACK_FAILED`를 반환한다.

## 13. 현재 제한사항

- 설정 저장소는 JSON 파일 기반이다.
- 프로세스 간 파일 Lock과 Snapshot 동기화는 제공하지 않으므로 관리 API를 사용할
  때는 Uvicorn worker를 하나만 실행해야 한다.
- 컨테이너를 교체해도 변경을 유지하려면 `config`를 쓰기 가능한 영속 볼륨으로
  마운트해야 하며, 기본 컨테이너 사용자 UID/GID `10001:10001`에 쓰기 권한을
  부여해야 한다.
- 기존 Docker named volume `lpl-config`는 새 이미지에 포함된 번들 `config`를
  가린다. `/mask` 지원 이미지로 업그레이드할 때 볼륨에 `mask_prompt.j2`가 없으면
  첫 Snapshot build와 애플리케이션 시작이 fail-closed로 실패한다. 기존 Deployment
  JSON을 보존한 뒤 새 파일을 볼륨에 추가하거나 볼륨을 안전하게 재구성해야 한다.
- Deployment 삭제는 비활성 항목만 허용하며 삭제 API가 자동으로 비활성화하지
  않는다.
- 파일 자동 감시는 없다. `OfflineRegistryEditor`나 외부 도구로 종류별 JSON을
  직접 바꿔도 실행 중 Snapshot은 자동 Reload되지 않는다.
- Prompt 생성·수정·삭제 API나 서비스는 제공하지 않는다.
- 요청별 모델 파라미터와 구조화 출력 설정은 없다.
- Secret 참조와 신뢰 구역 정책은 MVP Registry에 아직 포함되지 않았다.
- Provider 등록 구성을 바꾸려면 애플리케이션 재조립 또는 재시작이 필요하다.
