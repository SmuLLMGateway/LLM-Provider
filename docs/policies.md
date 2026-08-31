# 활성 정책 설정

LPL은 `/detect` 요청마다 활성 정책 목록을 받지 않는다. 단일 LPL 인스턴스의 전역
설정은 `config/policy_settings.json`에 저장하고 별도 API로 조회·전체 교체한다.

```text
GET /policies/enabled
PUT /policies/enabled
```

## 조회

```http
GET /policies/enabled
```

```json
{
  "enabledPolicies": ["P01", "P03", "S01", "B01"]
}
```

## 전체 교체

```http
PUT /policies/enabled
Content-Type: application/json
```

```json
{
  "enabledPolicies": ["P01", "P03", "S01", "B01"]
}
```

`enabledPolicies`는 필수이며 최소 1개, 최대 14개다. 중복, 미등록 ID, 추가 필드와
snake_case 이름은 `422 REQUEST_VALIDATION_FAILED`로 거부한다. 요청 순서는 다음
서버 고정 순서로 정규화한다.

```text
P01, P02, P03, P04, P05, P06, P07, P08,
S01, S02, S03, B01, B02, B03
```

## 저장과 실행 반영

설정 파일이 없는 기존 배포 볼륨은 최초 시작 시 14개 전체 활성 기본값으로
`policy_settings.json`을 생성한다. PUT은 완성된 임시 JSON 파일을 만든 뒤 공개
경로로 원자 교체하고, 저장 성공 후에만 활성 불변 설정 참조를 교체한다. 같은 값을
다시 PUT하면 파일과 generation을 변경하지 않는 멱등 성공이다.

Detection Pipeline은 요청 시작 시 현재 `PolicySettings`를 한 번 캡처하므로 처리 중
PUT이 성공해도 진행 중 요청의 정책 집합은 바뀌지 않는다. 다음 요청부터 새 설정을
사용한다.

- 비활성 정책을 참조하는 RegexCandidate: `422 DETECTION_POLICY_DISABLED`
- 비활성 정책의 NER 결과: 제외
- LLM Prompt Context와 JSON Schema: 후보 유무와 관계없이 활성 정책 전체 포함
- Schema를 무시한 비활성 LLM 출력: `502 LLM_DETECTION_OUTPUT_POLICY_DISABLED`

P01~P08, S01~S03, B01~B03 중 활성화된 모든 정책을
`config/policy_prompts.json` 조립 대상으로 사용한다. Regex·NER 후보가 없어도 P02
고유식별정보와 P03 연락처를 포함한 전체 활성 타입을 LLM이 추가 탐지할 수 있다.

`/detect`의 `organizationProfile`은 선택 사항이다. 프로필을 제공한 경우에만 활성
정책에 따라 다음 섹션을 요구한다.

| 정책 | 조건부 필수 Context |
|---|---|
| `P01` | `privacy` |
| `S02` | `securityContext` |
| `S03` | `securityContext.protectTestLogs` |
| `B02` | `thirdPartyContext` |
| `B03` | `confidentialTechnologyContext` |

프로필 전체를 생략하거나 `null`로 보내는 것은 허용한다. 객체를 제공하면서 이
조건을 충족하지 못하면 `422 ORGANIZATION_PROFILE_INCOMPLETE`로 거부한다. LPL은
조직 프로필을 저장하지 않으며 Gateway가 제공 가능한 경우에만 현재 요청에 넣어
전달한다.

## 오류 응답

| HTTP 상태 | 코드 | 조건 |
|---:|---|---|
| 422 | `REQUEST_VALIDATION_FAILED` | 빈 배열, 중복·미등록 ID, 잘못된 타입 또는 추가 필드 |
| 500 | `POLICY_SETTINGS_STORAGE_FAILED` | 설정 파일 원자 저장 실패 |
| 503 | `POLICY_SETTINGS_NOT_INITIALIZED` | Runtime의 활성 정책 설정이 준비되지 않음 |
