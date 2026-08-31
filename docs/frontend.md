# 기능 테스트용 Frontend

## 1. 목적과 범위

`app/frontend`는 LPL FastAPI 기능을 브라우저에서 수동으로 확인하기 위한 내부
테스트 화면이다. FastAPI와 같은 origin의 `/ui/`에서 정적 파일로 제공되며 별도
Frontend 서버, Node.js, npm 패키지나 빌드 과정이 없다.

이 화면은 다음 기능을 실제 LPL API에 요청한다.

- NER·LLM Deployment 목록 조회
- NER·LLM Deployment 추가
- NER·LLM Deployment 전체 설정 수정
- NER·LLM Deployment Probe
- 개인정보·기밀정보 탐지를 위한 `POST /detect`
- 전체 Detection 기반 Local LLM 마스킹을 위한 `POST /mask`
- Local LLM 생성을 위한 `POST /generate`
- 대화 제목 생성을 위한 `POST /titles`
- 성공·실패 상태, 성공 응답 JSON과 HTTP 오류 JSON 표시

운영 사용자용 UI, Gateway Frontend 또는 관리 콘솔이 아니다. 인증, 권한, 사용자별
Deployment 접근 정책, Secret 관리와 운영 수준의 입력 보호 기능을 제공하지 않는다.

## 2. 파일 구성

```text
app/frontend/
├─ index.html
├─ styles.css
└─ app.js
```

| 파일 | 역할 |
|---|---|
| `index.html` | Deployment 관리, Probe와 Runtime API 호출을 위한 화면 구조와 입력 요소를 정의한다. |
| `styles.css` | 테스트 화면의 배치, 상태 표시, 입력 폼과 JSON 출력 영역의 모양을 정의한다. |
| `app.js` | 사용자 이벤트 처리, 같은 origin API 호출, Deployment 목록 갱신과 JSON·오류 출력을 담당한다. |

세 파일은 FastAPI가 그대로 제공한다. 브라우저에서 실행할 번들을 만들거나 소스
변환을 수행하지 않는다.

## 3. 실행과 접속

프로젝트 루트의 PowerShell에서 가상환경을 활성화한 뒤 Uvicorn을 실행한다.

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

가상환경을 활성화하지 않고 직접 실행하려면 다음 명령을 사용할 수 있다.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

서버가 시작되면 다음 주소를 연다.

```text
http://127.0.0.1:8000/ui/
```

화면과 API가 같은 `http://127.0.0.1:8000` origin을 사용하므로 JavaScript는
`/deployments/ner`, `/detect` 같은 상대 경로로 요청한다. 별도의 CORS 설정이나
Frontend 개발 서버가 필요하지 않다. 서버 종료는 Uvicorn을 실행한 터미널에서
`Ctrl+C`를 누른다.

FastAPI의 자동 API 문서는 다음 주소에서 함께 확인할 수 있다.

```text
http://127.0.0.1:8000/docs
```

## 4. Deployment 기능

초기 Registry는 비어 있으므로 처음 접속하면 NER·LLM 목록이 모두 빈 상태로
표시된다. Detect, Mask, Generate와 Title 테스트 전에 이 화면에서 필요한 Deployment를
종류별로 추가한다.

NER와 LLM은 종류별 Endpoint를 사용한다.

| 기능 | NER | LLM |
|---|---|---|
| 목록 | `GET /deployments/ner` | `GET /deployments/llm` |
| 추가 | `POST /deployments/ner` | `POST /deployments/llm` |
| 전체 수정 | `PUT /deployments/ner/{deployment_id}` | `PUT /deployments/llm/{deployment_id}` |
| 연결 검사 | `POST /deployments/ner/{deployment_id}/probe` | `POST /deployments/llm/{deployment_id}/probe` |

추가 요청은 본문의 `deploymentId`를 사용한다. 수정 요청은 경로에서 ID를 받고 기존
설정 전체를 교체하므로 일부 필드만 보내는 Patch로 사용하면 안 된다. 화면에서
추가·수정을 실행하면 테스트용 임시 상태가 아니라 실제 종류별 Registry JSON이
저장되고 Active Snapshot Reload가 수행된다.

NER를 선택하면 Adapter와 Model Name 입력은 숨겨지고 전체 POST Endpoint인
`baseUrl`, `timeoutMs`, `enabled`만 전송한다. LPL은 `baseUrl`에 경로를 붙이지
않는다. LLM을 선택한 경우에만
`GET /adapters/llm` 목록으로 Adapter를 선택하고 필요하면 `modelName`을 입력한다.

Probe는 Request Body 없이 실제 Backend 최소 요청을 보낸다. `enabled=false`인
Deployment도 점검할 수 있지만 Probe 성공이 설정 수정이나 자동 활성화를 뜻하지
않는다. LLM Probe는 모델 로드와 Cold Start를 유발할 수 있다.

LPL API는 비활성 Deployment 삭제를 지원하지만 현재 기능 테스트 화면에는 삭제
버튼을 제공하지 않는다. 삭제 API는 Gateway나 별도 관리 도구에서 먼저
`PATCH .../enabled`로 비활성화한 뒤 `DELETE /deployments/{kind}/{deployment_id}`로
호출한다. 활성 Deployment를 바로 삭제하면 `409 DEPLOYMENT_MUST_BE_DISABLED`,
정상적으로 삭제하면 본문 없는 `204 No Content`를 반환한다.

## 5. Runtime API 기능

### 5.1 탐지

`POST /detect`는 원문과 NER·LLM Deployment ID를 받아 NER 실행 후 LLM 후보 판정과
추가 탐지를 수행한다. 미확정 Regex 후보가 없다면 `regexCandidates`에 빈 배열을
사용한다. 응답의 `candidateDecisions`에서 REJECTED와 UNCERTAIN을 확인해야 한다.

```json
{
  "text": "홍길동은 서울에 살고 있습니다.",
  "nerDeploymentId": "ner-local-a",
  "llmDeploymentId": "llm-local-a",
  "organizationProfile": {
    "organization": {"name": "ABC 주식회사", "aliases": ["ABC"], "type": "PRIVATE"},
    "publicContext": {"domains": ["abc.com"], "entities": []},
    "privacy": {"personNameScope": true, "persons": []},
    "securityContext": {"internalIpRanges": [], "internalDomains": [], "internalSystems": [], "cloudAssets": [], "securityAssets": [], "protectTestLogs": true},
    "confidentialTechnologyContext": {"assets": []},
    "thirdPartyContext": {"entities": []}
  },
  "sourceType": "CHAT_TEXT",
  "regexCandidates": []
}
```

테스트 화면은 선택적인 조직 프로필 JSON 입력란과 `CHAT_TEXT | OCR_TEXT` 선택을
제공한다. 조직 입력란을 비우면 `organizationProfile: null`로 전송한다. 실제
운영에서는 Gateway가 제공 가능한 관리자 설정과 텍스트 출처를 조립해 전달한다.

### 5.2 마스킹

`POST /mask`는 Gateway가 병합할 전체 Regex·NER·LLM Detection과 원문을 선택한
Local LLM Deployment로 전달한다. 이 화면은 Detect 응답을 자동으로 복사하거나
CONFIRMED Regex와 병합하지 않으므로 테스트하는 사람이 최종 `detections` 배열
전체를 직접 입력해야 한다.

```json
{
  "text": "홍길동은 서울에 살고 있습니다.",
  "llmDeploymentId": "llm-local-a",
  "detections": [
    {
      "start": 0,
      "end": 3,
      "text": "홍길동",
      "type": "PERSONAL_IDENTITY",
      "policyId": "P01",
      "source": "ner",
      "score": 0.98
    }
  ]
}
```

빈 `detections` 배열은 유효하다. 선택한 LLM Deployment의 존재·활성 상태·종류는
같이 검증하지만 Backend를 호출하지 않고 원문과 빈 replacement 배열을 반환한다.
성공 응답은 `maskedText`와 원문 좌표의 `start`, `end`, 요청 범위
`entityId`, `placeholder`, `types`, `sources`를 가진 `replacements`를 표시한다.
자세한 fail-closed 계약은 [Masking API](./masking.md)를 따른다.

### 5.3 생성

`POST /generate`는 선택적인 이전 LLM 응답과 현재 사용자 입력을 선택한 Local LLM
Deployment로 전달한다.

```json
{
  "previousText": [
    {
      "role": "user",
      "content": "이전 사용자 입력입니다."
    },
    {
      "role": "assistant",
      "content": "이전 Local LLM 응답입니다."
    }
  ],
  "text": "이 문장을 요약해 주세요.",
  "llmDeploymentId": "llm-local-a"
}
```

화면의 이전 대화 입력은 선택 사항이며 `role`, `content`를 가진 JSON 객체 배열로
입력한다. `role`은 `user` 또는 `assistant`만 허용한다. 비어 있으면
`previousText: []`로 요청한다.

### 5.4 제목 생성

`POST /titles`는 첫 사용자 메시지와 선택한 Local LLM Deployment로 한 줄 제목을
생성한다.

```json
{
  "text": "로컬 모델 서버를 등록하는 방법을 알려 주세요.",
  "llmDeploymentId": "llm-local-a"
}
```

화면은 어떤 메시지가 첫 메시지인지 판단하거나 생성된 제목을 저장하지 않는다.
제목 생성 API의 입출력만 확인한다.

## 6. 응답과 오류 표시

각 호출 결과 영역에는 성공·실패 상태와 API 응답을 표시한다. JSON 응답은 읽기 쉬운
들여쓰기로 출력하고, 오류일 때는 HTTP 상태 코드와 응답 원형을 같은 영역에 표시한다.

대표적인 오류는 다음과 같다.

- 요청 본문이나 ID가 계약과 다른 `REQUEST_VALIDATION_FAILED`
- 존재하지 않는 Deployment의 `DEPLOYMENT_NOT_FOUND`
- 일반 Runtime 요청에서 비활성 Deployment를 선택한 `DEPLOYMENT_DISABLED`
- Backend Provider, 연결, timeout 또는 모델 응답 오류

화면은 오류를 성공으로 변환하거나 재시도하지 않는다. 최종 판단 기준은 표시된
HTTP 상태와 API의 `detail.code`, `detail.message`다.

## 7. 데이터 전송과 보안 주의사항

이 UI는 미리 만들어 둔 가짜 화면이 아니다. 사용자가 입력한 다음 데이터가
브라우저에서 같은 origin의 FastAPI로 실제 전송된다.

- `/detect`, `/mask`, `/generate`, `/titles`에 입력한 원문
- Deployment 추가·수정에 입력한 URL과 제한시간, LLM의 Adapter 종류와 모델명
- 선택한 Deployment ID, 기존 탐지 결과와 `/mask`의 전체 Detection

브라우저 개발자 도구의 Network 화면, 로컬 프록시 또는 서버 접근 로그에서 요청을
확인할 수 있다. `http://`로 원격 접속하면 전송 구간이 암호화되지 않는다. 따라서
다음 원칙을 지킨다.

- 로컬 개발 환경이나 접근이 통제된 신뢰 내부망에서만 사용한다.
- 인터넷에 직접 공개하지 않는다.
- 실제 Secret, API Key나 Token을 입력하지 않는다.
- 가능한 한 합성 테스트 원문과 테스트 전용 모델 설정을 사용한다.
- 운영 환경에서 사용해야 한다면 별도 인증·권한, TLS와 네트워크 접근 통제를 먼저
  적용한다.

같은 origin이라는 사실은 CORS 구성을 단순하게 할 뿐 인증이나 보안 경계를
제공하지 않는다.

## 8. 지원하지 않는 기능

- 운영 사용자용 화면과 사용자별 상태 저장
- 로그인, 인증, 권한과 사용자별 Deployment 필터링
- Deployment 삭제 UI와 부분 수정 UI
- Prompt 생성·수정·삭제
- 모델 다운로드 또는 모델 서버 시작·중지
- Gateway의 Regex 탐지, 전체 Detection 자동 병합과 외부 LLM Provider 호출
- 요청 및 결과의 영구 저장

화면의 목적은 LPL이 이미 제공하는 API 계약과 Backend 연결을 빠르게 수동 검증하는
것으로 제한한다.
