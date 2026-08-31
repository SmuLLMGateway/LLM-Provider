# Registry 모듈 구조

## 1. 목적

Registry 모듈은 호출 가능한 NER·LLM Deployment를 검증·저장하고, 고정
탐지·마스킹·제목 Prompt와 함께 불변 `ActiveRegistrySnapshot`으로 관리한다.

탐지나 생성에 사용할 모델 조합은 Registry에 별도 리소스로 저장하지 않는다.
요청이 Deployment ID를 직접 전달하며, `DeploymentResolver`가 요청 시작 시 캡처한
하나의 Snapshot에서 역할에 맞는 실행 계획을 조립한다.

관련 문서:

- [Registry 설정 및 파일 형식](./registry_configuration.md)
- [Deployment 카탈로그 API](./deployments.md)
- [Backend 실행 구조](./backends.md)
- [Prompt 모듈 구조](./prompts.md)
- [Detection Contract](./detection.md)
- [Masking API](./masking.md)
- [Generation API](./generation.md)
- [대화 제목 생성 API](./title_generation.md)

## 2. 코드 구조

```text
app/
├─ api/
│  └─ deployments.py
├─ schemas/
│  ├─ deployments.py
│  └─ registry.py
├─ services/
│  ├─ deployment_catalog.py
│  └─ deployment_management.py
├─ core/
│  └─ registry_file_coordinator.py
├─ backends/
│  ├─ backend_registry.py
│  └─ provider_registry.py
└─ registry/
   ├─ store.py
   ├─ file_store.py
   ├─ validator.py
   ├─ mutator.py
   ├─ offline_editor.py
   ├─ snapshot.py
   ├─ snapshot_builder.py
   ├─ manager.py
   ├─ deployment_resolver.py
   └─ execution_plan.py

config/
├─ ner_deployments.json
├─ llm_deployments.json
├─ prompts.j2
├─ policy_prompts.json
├─ mask_prompt.j2
└─ title_prompt.j2
```

| 구성요소 | 역할 |
|---|---|
| Pydantic Registry 모델 | JSON 필드, 자료형과 개별 Deployment 불변식 검증 |
| `RegistryFileCoordinator` | Registry JSON 작업과 후보 Snapshot 읽기 동기화 |
| `BackendRegistry` | NER 고정 HTTP 및 `(llm, adapterType)`별 설정 계약 |
| `BackendProviderRegistry` | 고정 NER Backend와 LLM Adapter 구현체 등록·선택 |
| `RegistryValidator` | 모든 Deployment의 종류별 실행 설정 계약 검증 |
| `RegistryMutator` | 메모리 `RegistryConfig`에 Deployment 추가·교체 |
| `OfflineRegistryEditor` | 프로세스 중지 상태의 Registry 파일 편집 |
| `RegistryFileStore` | 종류별 Deployment JSON 로드·통합과 변경된 한 파일의 원자 저장 |
| `RegistrySnapshotBuilder` | 첫 build에서 고정 Prompt와 정책 Catalog를 컴파일·캐시하고 Registry 후보 생성 |
| `ActiveRegistrySnapshot` | 요청이 사용할 불변 Deployment 맵과 역할별 Prompt Artifact |
| `RegistryManager` | 마지막 정상 Snapshot과 Deployment 설정 Reload 관리 |
| `DeploymentResolver` | 요청 ID를 검증해 역할별 실행 계획으로 조립 |
| `DeploymentCatalogService` | Snapshot의 내부 설정을 안전한 응답 정보로 투영 |
| `DeploymentManagementService` | Deployment 저장, 후보 Reload와 실패 시 이전 설정 복원 |

## 3. RegistryConfig와 ActiveRegistrySnapshot

두 객체는 목적이 다르다.

```text
RegistryConfig
└─ deployments: dict[str, DeploymentConfig]

ActiveRegistrySnapshot
├─ snapshot_id
├─ deployments: Mapping[str, DeploymentConfig]
├─ detection_prompt: PromptArtifact
├─ mask_prompt: MaskPromptArtifact
└─ title_prompt: TitlePromptArtifact
```

`RegistryConfig`는 파일 로드, 편집과 후보 생성의 입력이다. Pydantic
`frozen=True`만으로 내부 `dict`까지 불변이 되지는 않으므로 실행 중 요청에 그대로
공유하지 않는다.

`ActiveRegistrySnapshot`은 Deployment 맵을 방어 복사하고 `MappingProxyType`으로
감싼다. 고정 Prompt도 검증·컴파일된 역할별 Artifact 객체로 보관한다. 요청은
`RegistryManager.capture()`로 얻은 동일 Snapshot을 끝까지 사용한다.

## 4. app/schemas/registry.py

Registry JSON의 유일한 구조 계약이다.

| 모델 또는 타입 | 역할 |
|---|---|
| `ResourceId` | Registry 리소스 ID 형식 |
| `AdapterType` | Adapter 등록 키 형식 |
| `DeploymentConfig` | 호출 가능한 NER 또는 LLM 인스턴스 설정 |
| `DeploymentFileEntry` | 물리 JSON에 저장하는 `kind` 없는 실행 설정 |
| `DeploymentRegistryFile` | 종류별 파일이 공유하는 최상위 객체 맵 |
| `NerDeploymentRegistryFile` | NER 파일 항목을 내부 `kind=ner` 설정으로 변환 |
| `LlmDeploymentRegistryFile` | LLM 파일 항목을 내부 `kind=llm` 설정으로 변환 |
| `RegistryConfig` | Deployment 맵을 담은 전체 설정 객체 |

알 수 없는 필드는 거부하고 JSON camelCase를 Python snake_case alias로 변환한다.
Adapter별 필수·금지 필드는 중심 Pydantic 모델에 고정하지 않고
`BackendRegistry`가 검증한다.

`ResolvedDeployment`, `DetectionExecutionPlan`, `MaskingExecutionPlan`, `GenerationExecutionPlan`과
`TitleGenerationExecutionPlan`은 JSON 입출력 Schema가 아니다. 검증된 실행 중 객체이므로
`app/registry/execution_plan.py`의 frozen dataclass로 관리한다.

## 5. RegistryFileCoordinator

`RegistryFileCoordinator`는 실행 구성 파일 작업을 하나의 프로세스 내부
`threading.RLock`으로 조율한다.

```python
with coordinator.transaction():
    ...
```

같은 Coordinator를 다음 컴포넌트에 주입한다.

- `RegistryFileStore`
- `RegistrySnapshotBuilder`

Builder가 후보를 만드는 동안 Registry 파일이 바뀌는 프로세스 내부 경쟁을 막는다.
고정 탐지·마스킹·제목 Prompt는 편집 대상이 아닌 읽기 전용 배포 리소스다. Coordinator는 외부
프로세스의 직접 쓰기를 막는 OS 파일 잠금은 아니다.

## 6. RegistryFileStore

`RegistryFileStore`는 종류별 파일 두 개를 읽어 하나의 `RegistryConfig`로
조립한다.

```text
ner_deployments.json
→ load_strict_json()
→ NerDeploymentRegistryFile ─┐
                              ├─ 전역 ID 중복 검사
llm_deployments.json          │
→ load_strict_json()          │
→ LlmDeploymentRegistryFile ──┘
              │
              ▼
       RegistryConfig
              │
              ▼
      RegistryValidator
```

엄격한 JSON Codec이 UTF-8 오류, 중복 키, 비표준 숫자와 잘못된 문법을 먼저
거부한다. 이어 Pydantic 파일 모델이 `kind` 없는 구조를 검증하고 파일명에 따라
각각 `ner` 또는 `llm`을 내부 `DeploymentConfig.kind`로 주입한 뒤 Registry
Validator가 Adapter 계약을 확인한다. 물리 JSON에 명시적인 `kind`가 있으면
거부한다. 두 파일의 ID 집합이 겹치면 병합 과정에서 덮어쓰지 않고 전체 후보를
거부하므로 Deployment ID는 Registry 전체에서 고유하다.

변경 시에는 현재 Registry를 깊은 복사한 작업본으로 만든다. Updater가 새
`RegistryConfig`를 반환하고 전체 검증을 통과한 경우에만 종류별 맵을 비교한다.
한 번의 Store 갱신에서는 NER 또는 LLM 한 종류만 바꿀 수 있다. 변경한 종류의
임시 파일을 같은 폴더에 완성한 뒤 해당 JSON만 원자 교체하며, 다른 종류의 파일은
다시 쓰지 않는다. 두 종류를 동시에 바꾸려는 Updater는
`RegistryMultiKindUpdateError`로 거부한다.

이전 단일 파일 `deployments.json`이 남아 있으면 조용히 무시하지 않고
`LegacyDeploymentRegistryFileError`를 발생시킨다. 운영 설정은 먼저 두 종류별
파일로 명시적으로 마이그레이션해야 한다.

## 7. RegistryValidator

`RegistryValidator`는 NER를 고정 공통 HTTP 설정 계약으로, LLM을 해당
`(llm, adapterType)` 설정 계약으로 검증한다.

- NER의 `baseUrl`, `timeoutMs`가 존재하고 `modelName`이 없는지
- LLM의 `(llm, adapterType)`이 등록되어 있는지
- LLM Adapter별 필수·금지 필드를 지키는지
- 종류별 추가 검증을 통과하는지

활성 Deployment의 실제 Provider coverage는 Provider-aware
`RegistrySnapshotBuilder`가 확인한다. 요청 역할에 맞는 `kind`, Deployment 존재와
활성화 상태는 요청 시 `DeploymentResolver`가 검증한다.

## 8. RegistryMutator, OfflineRegistryEditor와 DeploymentManagementService

`RegistryMutator`는 파일 I/O 없이 새로운 `RegistryConfig`를 만든다.
온라인과 오프라인 변경은 이 순수 변경 로직과 `RegistryFileStore`만 공유한다.

```text
온라인:
HTTP API
→ DeploymentManagementService
→ Store 트랜잭션에서 RegistryMutator 적용
→ 파일 저장 → Snapshot Reload
→ 활성화 실패 시 파일·Snapshot 복원

오프라인:
중지된 LPL 프로세스
→ OfflineRegistryEditor
→ Store 트랜잭션에서 RegistryMutator 적용
→ 파일만 저장
→ 다음 애플리케이션 시작 시 전체 검증·활성화
```

`OfflineRegistryEditor`와 `add_deployment_offline()`,
`update_deployment_offline()`은 초기 설정, 마이그레이션과 수동 정비용이다.
Active Snapshot Reload나 활성화 실패 Rollback을 수행하지 않으므로 실행 중인
프로세스와 함께 사용하지 않는다.

실행 중 온라인 변경의 유일한 진입점은 `DeploymentManagementService`다. 서비스는
Editor에 위임하지 않고 `RegistryStore`, `RegistryMutator`와 `RegistryManager`를
직접 조합한다. 한 프로세스 안의 변경 작업을 직렬화하고 다음 순서를 하나의 관리
유스케이스로 처리한다.

```text
변경할 kind의 이전 Deployment 맵 캡처
→ Deployment 추가 또는 전체 교체
→ 해당 kind 파일 원자 저장
→ RegistryManager.try_reload()
→ 정상 후보이면 Active Snapshot 교체
→ 후보가 거부되면 변경한 kind만 이전 맵으로 복원
→ 복원한 설정을 다시 Reload하여 마지막 정상 Snapshot 확인
```

따라서 저장 뒤 Snapshot 활성화가 실패하면 이전 설정으로 원복한다. 원복까지
실패한 경우에는 별도의 `DEPLOYMENT_ROLLBACK_FAILED` 오류로 운영자가 개입해야 하는
상태를 알린다. 복원 도중 반대 종류에 별도 변경이 생겨도 그 파일은 덮어쓰지 않는다.

## 9. RegistrySnapshotBuilder

Builder의 후보 생성 흐름:

```text
RegistrySnapshotBuilder 첫 build()
        │ RegistryFileCoordinator Transaction
        ▼
RegistryFileStore.load()
        ├─ Pydantic 구조 검증
        ├─ RegistryValidator
        └─ BackendProviderRegistry coverage 검증
        │
        ▼
config/prompts.j2, config/policy_prompts.json, config/mask_prompt.j2와 config/title_prompt.j2 한 번 로드·검증·컴파일
        │
        ▼
PolicyPromptCatalog와 세 역할별 PromptArtifact 캐시
        │
        ▼
Deployment 설정 + 세 Prompt·정책 Catalog 해시로 snapshot_id 계산
        │
        ▼
ActiveRegistrySnapshot 후보

이후 build()
→ Deployment 설정만 다시 로드·검증
→ 캐시한 동일한 세 Prompt Artifact와 정책 Catalog 재사용
```

탐지·마스킹·제목 생성 Prompt는 선택적 리소스가 아니다. Deployment가 비어
있어도 Builder는 첫 정상 build에서 물리 파일 `config/prompts.j2`,
`config/policy_prompts.json`, `config/mask_prompt.j2`와 `config/title_prompt.j2`를
함께 검증·컴파일하고 Artifact를 캐시해 Snapshot에 담는다. 이후 build에서는 네
파일을 다시 읽거나 컴파일하지 않는다. 파일 경로는 코드
상수이며 Registry나 요청에서 선택하지 않는다. Artifact에는 컴파일 Handle과
content hash만 남고 Prompt 원문이나 metadata는 보관하지 않는다. 최초 시작에서
어느 한 Prompt라도 잘못되면 정상 후보를 만들지 않는다.

`snapshot_id`는 정렬된 Deployment 설정, 세 Artifact와 정책 Catalog의 `content_hash`를
canonical JSON으로 직렬화한 뒤 SHA-256으로 계산한다.

## 10. RegistryManager

`RegistryManager`는 현재 정상 Snapshot의 수명을 관리한다.

```text
initialize()
→ 최초 후보 전체 검증
→ 정상 후보 활성화

try_reload()  Deployment 설정 Reload
→ Lock 밖에서 변경된 ner_deployments.json과 llm_deployments.json 통합 후보 생성
→ 캐시한 동일한 탐지·마스킹·제목 Prompt Artifact 재사용
→ 성공한 후보만 짧은 state lock으로 교체
→ 실패하면 기존 Snapshot 유지

capture()
→ 현재 활성 Snapshot 참조 반환
```

파일 읽기, 최초 Prompt 컴파일과 Backend 호출 중에는 state lock을 잡지 않는다.
포인터 조회·교체만 짧게 보호하므로 진행 중 요청은 캡처한 이전 Snapshot을 계속
사용할 수 있다. Reload는 Deployment 설정만 반영하며 네 Prompt 리소스를 다시
읽거나 본문을 변경하지 않는다.

## 11. DeploymentResolver와 실행 계획

`DeploymentResolver`는 일반 Runtime 실행 계획을 만들며 파일이나 Store에
접근하지 않고 전달받은 Snapshot만 사용한다.

탐지 계획:

```text
resolve_detection(
    ner_deployment_id,
    llm_deployment_id,
    snapshot,
)
├─ NER Deployment 존재·enabled·kind=ner 검증
├─ LLM Deployment 존재·enabled·kind=llm 검증
└─ DetectionExecutionPlan
   ├─ ner_deployment
   ├─ llm_deployment
   └─ detection_prompt
```

마스킹 계획:

```text
resolve_masking(llm_deployment_id, snapshot)
├─ LLM Deployment 존재·enabled·kind=llm 검증
└─ MaskingExecutionPlan
   ├─ llm_deployment
   └─ mask_prompt
```

생성 계획:

```text
resolve_generation(llm_deployment_id, snapshot)
└─ GenerationExecutionPlan
   └─ llm_deployment
```

제목 생성 계획:

```text
resolve_title_generation(llm_deployment_id, snapshot)
├─ LLM Deployment 존재·enabled·kind=llm 검증
└─ TitleGenerationExecutionPlan
   ├─ llm_deployment
   └─ title_prompt
```

`ResolvedDeployment`는 ID와 검증된 `DeploymentConfig`를 함께 보관한다. 실행 계획은
Snapshot의 실제 객체를 참조하므로 Pipeline이 URL이나 Prompt를 다시 조회할 필요가
없다.

Resolver 오류:

| HTTP 상태 | 코드 | 조건 |
|---:|---|---|
| 404 | `DEPLOYMENT_NOT_FOUND` | 요청한 ID가 없음 |
| 409 | `DEPLOYMENT_DISABLED` | 요청한 Deployment가 비활성화됨 |
| 422 | `DEPLOYMENT_KIND_MISMATCH` | 요청 역할과 Deployment `kind`가 다름 |

이 오류 표와 활성 상태 검증은 Detection·Masking·Generation·Title의 일반 Runtime 실행
계획에 적용된다. 관리용 Deployment Probe는 활성화 전 연결 상태도 확인해야 하므로
별도 조회 정책으로 존재 여부와 요청 경로의 종류를 검증하고, `enabled=false`인
Deployment에도 실제 최소 요청을 보낸다. Probe는 설정과 Active Snapshot을
변경하거나 Deployment를 자동 활성화하지 않으며, Provider가 등록되지 않았다면
`503 BACKEND_PROVIDER_NOT_REGISTERED`를 반환한다.

## 12. 요청 단위 일관성

Detection:

```text
snapshot = manager.capture()
plan = resolver.resolve_detection(
    ner_deployment_id,
    llm_deployment_id,
    snapshot=snapshot,
)
→ plan.ner_deployment로 NER 실행
→ plan.detection_prompt 렌더링
→ plan.llm_deployment로 후속 LLM 실행
```

Masking:

```text
snapshot = manager.capture()
plan = resolver.resolve_masking(
    llm_deployment_id,
    snapshot=snapshot,
)
→ plan.mask_prompt를 정적 system 메시지로 렌더링
→ 검증한 원문·합집합 component·요청 namespace를 canonical JSON user 메시지로 전달
→ plan.llm_deployment로 동일 대상 그룹 생성
→ LPL이 replacement와 maskedText를 파생·검증
```

Generation:

```text
snapshot = manager.capture()
plan = resolver.resolve_generation(
    llm_deployment_id,
    snapshot=snapshot,
)
→ plan.llm_deployment로 생성 실행
```

제목 생성:

```text
snapshot = manager.capture()
plan = resolver.resolve_title_generation(
    llm_deployment_id,
    snapshot=snapshot,
)
→ plan.title_prompt를 정적 system 메시지로 렌더링
→ 첫 사용자 메시지를 별도 user 메시지로 전달
→ plan.llm_deployment로 제목 생성
```

Pipeline은 Plan을 받은 뒤 Snapshot을 다시 캡처하거나 파일을 읽지 않는다. 요청 처리
중 Deployment 설정 Reload가 성공해도 기존 요청의 Deployment는 바뀌지 않는다.
Prompt는 모든 요청과 Reload 후보에서 첫 build가 캐시한 역할별 동일 Artifact를
사용한다.

## 13. 책임 경계

| 컴포넌트 | 담당 | 담당하지 않음 |
|---|---|---|
| Registry Pydantic 모델 | JSON 구조·자료형 | 파일 I/O, Prompt 컴파일 |
| `BackendRegistry` | Adapter별 설정 계약 | 실제 네트워크 호출 |
| `RegistryValidator` | Deployment 설정 의미 검증 | Prompt 파일 검증 |
| `RegistryMutator` | 순수 설정 변경 | 저장, 활성화 |
| `OfflineRegistryEditor` | 프로세스 중지 상태의 Registry 파일 편집 | HTTP 요청 처리, Snapshot Reload·Rollback |
| `RegistryFileStore` | 종류별 Deployment JSON 통합 로드와 한 종류 파일의 원자 저장 | Prompt 파일 관리 |
| `RegistrySnapshotBuilder` | 전체 후보 검증·컴파일 | 활성 포인터 교체 |
| `ActiveRegistrySnapshot` | Deployment와 역할별 Prompt Artifact 저장·수명 보장 | Backend 실행 |
| `RegistryManager` | 활성 Snapshot 수명주기 | Deployment 해석 |
| `DeploymentManagementService` | Deployment 추가·교체·활성 상태 변경·삭제의 저장, Reload와 실패 시 원복 | 자동 파일 감시 |
| `DeploymentResolver` | 역할별 실행 계획 조립 | 파일 접근, Backend 호출 |
| Pipeline | Plan 소비, Backend 호출, 결과 검증 | Registry 파일 편집 |

마스킹에서 LPL은 Local LLM 호출, 동일 대상 그룹의 구조화 출력 검증, 입력 Detection
합집합 전체 coverage와 비탐지 원문 불변 검증을 담당한다. Gateway는 Regex와 신규
NER·LLM Detection 전체 병합, `/mask` 호출과 성공한 `maskedText`의 외부 Provider
전달을 담당한다. LPL은 `entityId ↔ placeholder` 매핑을 Snapshot이나 Registry에
저장하지 않으며 매핑은 요청 범위에서만 유효하다.

제목 생성에서 LPL은 실행 계획 해석, Local LLM 호출과 출력 검증까지만 담당한다.
대화 상태와 첫 메시지 판단, 제목 생성 호출 시점 및 반환된 제목의 영구 저장은
Gateway가 담당한다.

## 14. Deployment 카탈로그와 운영 상세 경계

`DeploymentCatalogService`는 `RegistryManager.capture()`로 얻은 현재 Snapshot을
읽고 내부 `DeploymentConfig`를 목록용 운영 요약과 Gateway용 운영 상세 응답
모델로 변환한다.

```text
GET /deployments/ner
→ NER DeploymentSummary 목록

GET /deployments/ner/{deployment_id}
→ NER DeploymentDetail

GET /deployments/llm
→ LLM DeploymentSummary 목록

GET /deployments/llm/{deployment_id}
→ LLM DeploymentDetail
```

각 목록은 경로와 `kind`가 일치하는 항목의 ID 순서이며 `deploymentId`, `enabled`만
반환한다. 통합 목록과 `kind` Query Parameter는 제공하지 않는다. NER 상세는 필수
`baseUrl`, `timeoutMs`를 추가하고, LLM 상세는 필수 `adapterType`과 설정된 경우
`baseUrl`, `modelName`, `timeoutMs`를 추가한다. 상세
경로와 실제 Deployment의 `kind`가 다르거나 ID가 존재하지 않으면 모두
`404 DEPLOYMENT_NOT_FOUND`를 반환한다.

NER·LLM 종류는 요청 경로로 이미 표현되므로 `DeploymentSummary`와
`DeploymentDetail`에는 `kind`를 포함하지 않는다. 다만 내부
`DeploymentConfig.kind`와 Registry 검증, 경로와 실제 종류의 일치 검증 및 실행
요청의 역할 검증은 계속 유지한다.

비활성 Deployment도 운영 상태 확인을 위해 `enabled=false`로 조회된다. 이 API는
사용자별 선택 가능 모델 API가 아니므로 Gateway가 접근 정책과 활성 상태를 적용해
Frontend에 전달할 목록을 필터링한다.

일반 Runtime 실행에서는 비활성 Deployment를 거부하지만 관리용 Probe에서는 실제
연결 점검을 허용한다. Probe 결과는 Registry 설정이나 활성 상태를 변경하지 않는다.

운영 필드인 `baseUrl`, `timeoutMs`와 LLM 전용 `adapterType`, `modelName`은 목록에
포함하지 않으며 Gateway용 상세에서만 반환한다. URL의 IP 또는 호스트는 별도 필드로
중복하지 않고 `baseUrl`에서 확인한다. Secret은 상세에서도 반환하지 않는다.
LPL API는 Gateway 뒤의 신뢰 가능한 내부망으로 제한하고,
Gateway는 상세 운영 필드를 Frontend로 전달하지 않는다. 자세한 계약은
[Deployment 카탈로그 API](./deployments.md)를 따른다.

모든 `baseUrl`은 Active Snapshot 활성화 전에 공통 검증하며 query, fragment와 URL
userinfo를 거부한다. NER `baseUrl`은 호출 path까지 포함한 전체 POST Endpoint를
허용하며 LPL은 경로를 추가하지 않는다. URL 안에 Token이나 사용자명·비밀번호를
포함한 Deployment는 상세 API에 도달하기 전에 Registry 후보 단계에서 거부된다.

## 15. Deployment 추가·수정·활성 상태 변경·삭제 관리 API

NER와 LLM은 경로로 종류를 고정한 별도 관리 API를 제공한다.

```text
POST /deployments/ner
POST /deployments/llm
→ 요청 본문의 deploymentId로 새 Deployment 추가
→ 성공 시 201과 생성된 운영 상세 반환

PUT /deployments/ner/{deployment_id}
PUT /deployments/llm/{deployment_id}
→ 기존 Deployment의 설정 전체 교체
→ 부분 수정이 아니므로 유지할 선택 필드도 본문에 다시 포함

PATCH /deployments/ner/{deployment_id}/enabled
PATCH /deployments/llm/{deployment_id}/enabled
→ 기존 Deployment의 enabled만 변경

DELETE /deployments/ner/{deployment_id}
DELETE /deployments/llm/{deployment_id}
→ 비활성 Deployment만 삭제
→ 성공 시 본문 없는 204 No Content
```

요청 본문에는 `kind`를 받지 않는다. 경로의 NER·LLM 종류를 내부
`DeploymentConfig.kind`로 결합하므로 다른 종류로 변경할 수 없다. 같은 ID는 두
파일을 합친 전체 Registry에서 중복될 수 없으며, PUT 대상이 없거나 실제 종류가
경로와 다르면 `DEPLOYMENT_NOT_FOUND`를 반환한다.

DELETE는 Request Body를 받지 않으며 `enabled=false`인 Deployment만 제거한다.
활성 Deployment를 바로 삭제하면 `409 DEPLOYMENT_MUST_BE_DISABLED`로 거부한다.
운영 삭제는 먼저 `PATCH .../enabled`에 `{"enabled": false}`를 보내 비활성 상태의
저장과 Snapshot Reload를 완료한 뒤 DELETE를 호출한다. 삭제 대상이 없거나 경로
종류와 다르면 `404 DEPLOYMENT_NOT_FOUND`다.

관리 API들은 파일 저장에서 끝나지 않고 `DeploymentManagementService`가 즉시 후보
Snapshot을 Reload한다. 활성화에 실패하고 이전 파일·Snapshot 복원이 성공하면
`503 DEPLOYMENT_ACTIVATION_FAILED`, 복원까지 실패하면
`500 DEPLOYMENT_ROLLBACK_FAILED`를 반환한다. 삭제도 요청 종류의 파일만 원자
저장하며 같은 Reload·복원 정책을 따른다. API는 Registry 설정을 관리할 뿐 모델
서버 자체를 시작·중지하거나 다운로드하지 않는다.

## 16. 현재 제한사항

- JSON 파일 기반 저장소다.
- Deployment 삭제는 비활성 항목만 허용하며 삭제 API가 자동 비활성화를 수행하지
  않는다.
- 파일 변경 자동 감시는 없다. 관리 API를 거치지 않은 외부 파일 변경은 자동으로
  Reload되지 않는다.
- `OfflineRegistryEditor` 또는 수동 파일 변경은 실행 중 Snapshot에 반영되지
  않는다. 서비스를 중지한 상태에서만 사용하고 다음 시작 시 전체 검증을 거친다.
- Prompt 생성·수정·삭제 API나 서비스는 제공하지 않는다.
- 프로세스 간 파일 잠금은 없다.
- 요청별 모델 파라미터와 구조화 출력 설정은 없다.
- Secret과 신뢰 구역 정책은 아직 Registry 계약에 포함되지 않았다.
