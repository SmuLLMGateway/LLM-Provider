# Prompt 모듈 구조

## 1. 목적

Prompt 모듈은 역할이 고정된 세 Jinja2 파일과 하나의 정책 Prompt JSON을 안전하게
읽고 검증·컴파일하여 불변 실행 Artifact로 제공한다.

```text
config/
├─ prompts.j2          후보 판정·후속 LLM 추가 탐지 Prompt
├─ policy_prompts.json 14개 정책별 세부 탐지 지침
├─ mask_prompt.j2      동일 대상 그룹화와 마스킹 System Prompt
└─ title_prompt.j2     대화 제목 생성 System Prompt
```

네 파일은 배포된 읽기 전용 리소스다. Prompt ID, Registry 선택값, 요청별 경로,
사용자 정의 본문은 없다. 애플리케이션은 Prompt 생성·수정·삭제 API나 서비스를
제공하지 않는다.

- NER 개체 탐지는 Prompt 없이 먼저 실행한다.
- 후속 LLM은 `prompts.j2`를 사용해 Regex·NER 후보를 판정하고 놓친 정보를 추가 탐지한다.
- 마스킹은 `mask_prompt.j2`를 정적 `system` 메시지로 사용하고 검증된 원문,
  Detection 합집합 component와 요청 namespace를 별도 canonical JSON `user`
  메시지로 전달한다. 모델은 `maskedText`와
  `assignments[{targetId, entityId}]`만 출력한다.
- Generation은 Prompt 모듈을 사용하지 않고 요청 `text`를 그대로 Backend에
  전달한다.
- 제목 생성은 `title_prompt.j2`를 정적 `system` 메시지로 사용하고 첫 사용자
  메시지를 별도 `user` 메시지로 전달한다.

## 2. 코드 구조

```text
app/prompts/
├─ __init__.py
├─ prompt_artifact.py
├─ prompt_errors.py
├─ prompt_limits.py
├─ prompt_loader.py
├─ prompt_renderer.py
├─ policy_prompt_loader.py
├─ policy_prompt_catalog.py
├─ mask_prompt_artifact.py
└─ title_prompt_artifact.py

config/
├─ prompts.j2
├─ policy_prompts.json
├─ mask_prompt.j2
└─ title_prompt.j2
```

| 구성요소 | 역할 |
|---|---|
| `PromptLoader` | 코드에 고정된 각 Prompt 파일을 최초 build에서 한 번 읽기 |
| `PolicyPromptLoader` | 정책 JSON을 strict JSON으로 한 번 로드 |
| `PolicyPromptCatalog` | 정확한 14개 정책 본문 검증, 선택·중복 제거·고정 순서 조립 |
| `PromptArtifact` | 탐지 Prompt의 본문·두 변수 계약 검증, 컴파일과 렌더링 |
| `MaskPromptArtifact` | 마스킹 Prompt의 본문·무변수 계약 검증, 컴파일과 정적 렌더링 |
| `TitlePromptArtifact` | 제목 Prompt의 본문·무변수 계약 검증, 컴파일과 정적 렌더링 |
| `PromptRenderer` | 안전한 Jinja2 환경에서 불변 실행 Handle 생성 |
| `CompiledPromptTemplate` | 컴파일된 Handle과 렌더링 제한 정책 보관 |
| `PromptLimits` | 템플릿·전체 Context·출력 byte와 렌더링 시간 제한 |
| `prompt_errors.py` | 로드·컴파일·렌더링의 안전한 오류 타입 |

## 3. 고정 경로와 소유권

`app/prompts/prompt_loader.py`의 코드 상수가 파일명을 정의한다.

```python
PROMPT_FILENAME = "prompts.j2"
MASK_PROMPT_FILENAME = "mask_prompt.j2"
TITLE_PROMPT_FILENAME = "title_prompt.j2"
POLICY_PROMPTS_FILENAME = "policy_prompts.json"
```

`application_runtime()`은 전달받은 `config_dir`과 네 상수를 사용해
`config_dir/prompts.j2`, `config_dir/mask_prompt.j2`와
`config_dir/title_prompt.j2`, `config_dir/policy_prompts.json`을 자동 선택한다. Runtime
요청, Registry JSON이나 Pipeline 호출자가 경로를 주입하거나 선택하지 않는다.

기본 설정 디렉터리를 사용하는 물리 경로는 다음과 같다.

```text
config/prompts.j2
config/policy_prompts.json
config/mask_prompt.j2
config/title_prompt.j2
```

경로, 본문이나 변수 계약을 바꾸려면 새 애플리케이션 산출물을 배포하고 프로세스를
재시작해야 한다. `RegistryManager.try_reload()`는 Deployment 설정만 갱신하며 Prompt
파일을 다시 읽지 않는다.

## 4. PromptLoader

`PromptLoader.load()`는 인자를 받지 않는다.

```python
source = loader.load()
```

각 Loader는 생성 시 정해진 고정 파일 하나만 읽으며 다음을 수행한다.

1. 일반 파일인지 확인
2. 파일 크기를 확인한 뒤 제한된 byte만 읽기
3. UTF-8 디코딩
4. 줄바꿈을 LF로 정규화

주요 오류:

| 오류 | 조건 |
|---|---|
| `PromptTemplateNotFoundError` | 파일이 없거나 일반 파일이 아님 |
| `PromptTemplateDecodeError` | UTF-8로 해석할 수 없음 |
| `PromptTemplateReadError` | 파일 시스템 읽기 실패 |
| `PromptTemplateTooLargeError` | 템플릿 byte 제한 초과 |

Loader 자체는 캐시하지 않는다. `RegistrySnapshotBuilder`가 첫 정상 build에서 네
Loader의 `load()`를 호출하고 컴파일된 Artifact와 정책 Catalog를 캐시한다.

## 5. 역할별 Artifact 컴파일

`PromptArtifact.compile()`, `MaskPromptArtifact.compile()`과
`TitlePromptArtifact.compile()`은 별도 Validator 없이 각 고정 Prompt 계약 전체를
직접 검증하고 컴파일한다.

`RegistrySnapshotBuilder`는 각 Loader에서 받은 본문과 공유 Renderer를 역할별
Artifact의 `compile()`에 전달한다. 파일 경로는 오류 식별용으로 내부에서만
사용하며 외부 입력이나 선택값으로 노출하지 않는다.

검증 순서:

1. 본문이 문자열이고 공백만으로 이루어지지 않았는지 확인
2. UTF-8로 표현 가능한지 확인
3. 템플릿 UTF-8 byte 제한 확인
4. Sandboxed Jinja2 문법 분석과 컴파일
5. 외부 참조 변수 계약 확인
6. 본문 SHA-256 해시 계산

탐지용 `PromptArtifact`의 외부 변수는 다음 두 개와 정확히 일치해야 한다.

```text
text
existing_detections
```

하나가 누락되거나 다른 변수가 추가돼도 `PromptVariableContractError`가 발생한다.
이름만 맞추는 것이 아니라 실제 템플릿에서 두 변수를 모두 참조해야 한다.

마스킹용 `MaskPromptArtifact`와 제목용 `TitlePromptArtifact`는 외부 변수가 없어야
한다.

```text
MASK_PROMPT_VARIABLES = frozenset()
TITLE_PROMPT_VARIABLES = frozenset()
```

`mask_prompt.j2` 또는 `title_prompt.j2`가 변수를 하나라도 참조하면
`PromptVariableContractError`가 발생한다. 첫 사용자 메시지는 Jinja2 Context에
삽입하지 않고 Backend의 별도 `user` 메시지로 전달한다. 마스킹 원문과 component
evidence도 Jinja2 Context에 삽입하지 않고 canonical JSON `user` 메시지로 전달한다.

## 6. Prompt Artifact

세 Artifact는 각각 다음 두 필드만 보관한다.

```text
PromptArtifact
├─ compiled_template    CompiledPromptTemplate
└─ content_hash         본문 UTF-8 SHA-256

MaskPromptArtifact
├─ compiled_template    CompiledPromptTemplate
└─ content_hash         본문 UTF-8 SHA-256

TitlePromptArtifact
├─ compiled_template    CompiledPromptTemplate
└─ content_hash         본문 UTF-8 SHA-256
```

Prompt 원문, 경로 metadata, Task metadata나 별도 변수 목록은 보관하지 않는다.
`compiled_template`은 원시 Jinja 객체를 외부에 노출하지 않는 불변 Handle이다.

Artifact는 별도 Prompt Service를 거치지 않고 직접 렌더링한다.

```python
detection_prompt = detection_artifact.render(
    text=text,
    existing_detections=serialized_detections,
)

mask_system_prompt = mask_artifact.render()
title_system_prompt = title_artifact.render()
```

탐지 Artifact의 두 인자는 정확한 문자열이어야 한다. 마스킹과 제목 Artifact는
외부 인자를 받지 않으며 항상 역할별 같은 정적 System Prompt를 렌더링한다.

## 7. 렌더링 안전성과 제한

`PromptRenderer`는 `ImmutableSandboxedEnvironment`와 `StrictUndefined`를 사용하고
Jinja global을 제거한다.

탐지 Prompt의 렌더링 Context는 다음 고정 객체로 구성한다.

```python
{
    "text": text,
    "existing_detections": existing_detections,
}
```

`existing_detections`는 Python 객체 표현이 아니라 공통 JSON Codec으로 직렬화한
compact JSON 객체 문자열이다. 객체에는 `enabledPolicies`, 원본
`organizationProfile`, `sourceType`, `regexCandidates`, 새 `nerCandidates`가
들어간다. Regex 후보는 candidateId, span,
text, policyId, detailType과 score를 보존한다. NER 후보는 검증·중복 제거 후 LPL이
원문 순서대로 생성한 candidateId와 span, text, policyId, entityType, score를
포함한다. NER 후보의 정책·개체 조합은 `P01/PERSON`, `P04/LOCATION`으로 고정한다.
조직 프로필은 공개 도메인·공개 명칭과 활성 정책별 비공개 기준정보를 제공한다.
`sourceType`은 직접 입력한 `CHAT_TEXT`와 Gateway가 OCR로 추출한 `OCR_TEXT`를
구분한다. 조직 프로필은 선택 사항이며 제공되지 않으면 Context에
`organizationProfile: null`을 넣는다. 모델은 조직별 공개·내부 여부를 임의로
확정하지 않고 필요한 경우 UNCERTAIN으로 판정한다. Prompt 모듈이나 LPL 저장소에는
조직 정보를 보관하지 않는다.
탐지 LLM은 각 candidateId에 대해 `CONFIRMED`, `REJECTED`, `UNCERTAIN` 중 하나를
정확히 한 번 반환하고, 누락 항목은 별도 `newDetections`에 좌표 없이 출력한다.
Pipeline은 candidateId 집합을 재검증하며 후보 원문·좌표·정책은 모델 출력에서 받지
않고 이 Context의 검증된 값으로 복원한다.

정책별 지침은 후보 유무와 관계없이 전역 활성 정책 전체를 선택한다.

```text
GET/PUT /policies/enabled의 현재 enabledPolicies 전체
```

활성 정책은 P01~P08, S01~S03, B01~B03의 고정 순서로 정렬하고 중복을 제거한다.
`PromptArtifact.render()`는 기본 `prompts.j2` 렌더링 결과 뒤에 선택된 정책만
`<policy_instructions>` 블록으로 조립한다. 각 활성 정책은 후보 목록과 관계없이
한 번만 포함한다. Context의 `enabledPolicies`와 LLM `output_schema`의
허용 type도 전체 활성 정책과 동일하게 제한한다. Regex·NER 후보가 비어 있어도
활성 정책의 누락 탐지를 위해 LLM을 실행한다.

마스킹과 제목 Prompt는 외부 변수가 없으므로 빈 Context로 렌더링한다.

```python
{}
```

첫 사용자 메시지는 렌더링 Context가 아니라 LLM Backend의 별도 `user` 메시지로
전달되므로 사용자 본문을 Jinja2 코드로 해석하지 않는다. Masking Pipeline도 검증된
원문, 합집합 component와 서버 생성 namespace를 공통 JSON Codec의 canonical JSON
문자열로 직렬화해 별도 `user` 메시지로 전달한다.

현재 제한:

| 제한 | 기본값 | 설명 |
|---|---:|---|
| `max_template_bytes` | 262,144 | 고정 템플릿 UTF-8 크기 |
| `max_context_bytes` | 1,048,576 | 역할별 전체 Context JSON 크기 |
| `max_output_bytes` | 1,048,576 | 렌더링 결과 UTF-8 크기 |
| `render_timeout_ms` | 1,000 | 협력적 렌더링 제한 시간 |

탐지 입력 구조는 두 문자열로 고정되며 마스킹·제목 Prompt의 Context는 비어 있다.
렌더링 출력은 세 역할 모두 청크 단위로 생성하면서 누적 byte와 deadline을
확인한다. 마스킹 canonical user JSON은 Jinja Context가 아니므로 Masking 입력
Validator가 별도의 UTF-8 byte·Detection 개수 제한을 적용한다.

## 8. Snapshot 수명주기

```text
RegistrySnapshotBuilder 첫 정상 build()
→ config/prompts.j2, config/policy_prompts.json, config/mask_prompt.j2와 config/title_prompt.j2를 각각 한 번 로드
→ PolicyPromptCatalog 검증, PromptArtifact, MaskPromptArtifact와 TitlePromptArtifact 컴파일
→ 세 Artifact 캐시
→ ActiveRegistrySnapshot.detection_prompt
→ ActiveRegistrySnapshot.mask_prompt
→ ActiveRegistrySnapshot.title_prompt

이후 build() / RegistryManager.try_reload()
→ ner_deployments.json과 llm_deployments.json만 다시 로드·검증
→ 캐시한 동일한 세 Prompt Artifact와 PolicyPromptCatalog 재사용
```

최초 build에서 어느 한 Prompt라도 로드·검증·컴파일에 실패하면 애플리케이션
초기화도 실패한다. 첫 정상 build 이후에는 네 파일을 다시 읽거나 컴파일하지
않는다. Snapshot ID에는 세 Artifact와 정책 Catalog의 content hash가 모두 반영된다.

Prompt 변경 적용 절차:

```text
새 config/prompts.j2, config/policy_prompts.json, config/mask_prompt.j2 또는 config/title_prompt.j2를 포함한 산출물 배포
→ 프로세스 재시작
→ 첫 build에서 네 Prompt 리소스를 함께 검증·컴파일
```

운영 중 파일 교체, Prompt 전용 Reload, 자동 감시 기능은 제공하지 않는다.

## 9. 운영 안전 규칙

- `config/prompts.j2`, `config/policy_prompts.json`, `config/mask_prompt.j2`와 `config/title_prompt.j2`를 읽기 전용 배포 리소스로
  마운트한다.
- 기존 Docker named volume `lpl-config`가 번들 `config`를 가리는 배포에서는 이미지
  업그레이드 전에 볼륨에도 `policy_prompts.json`과 `mask_prompt.j2`가 있는지 확인한다. 파일이 없으면 첫
  Snapshot build를 실패시키며 불완전한 Prompt 집합으로 축소 시작하지 않는다.
- Runtime 요청에서 Prompt 경로, ID나 본문을 받지 않는다.
- Prompt 생성·수정·삭제 API 또는 내부 편집 서비스를 추가하지 않는다.
- Prompt 원문과 렌더링 결과 전체를 로그에 남기지 않는다.
- 마스킹 canonical user JSON, LLM 원출력, maskedText와 entity 매핑을 로그에 남기거나
  저장하지 않는다.
- 제목 생성 입력과 결과도 Prompt 모듈이 저장하지 않는다. Gateway가 첫 사용자
  메시지 후 제목 생성 API를 호출하고 검증된 제목을 대화방 metadata로 저장한다.
- 고정 파일의 버전, 승인 상태와 변경 이력은 빌드·배포 시스템에서 관리한다.
- 협력적 deadline은 장시간 반환하지 않는 단일 표현식을 선점 중단하지 못한다.
