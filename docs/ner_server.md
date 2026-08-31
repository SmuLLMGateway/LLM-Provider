# GLiNER NER 서버

## 1. 역할

`services/gliner_ner`는 LPL의 고정 표준 HTTP NER 계약을 구현하는 GLiNER 모델
서버와 Docker 이미지 빌드 컨텍스트다. 모델별 요청·응답 변환은 이 서버 내부에서
끝나며 LPL은 다른 NER 서버와 동일한 계약으로 호출한다.

```text
LPL Detection Pipeline
→ HttpNerBackend
→ POST http://ner-server:8008/v1/ner/detect
→ GLiNER NER 서버
→ urchade/gliner_multi-v2.1
```

LPL과 NER 서버는 별도 컨테이너다. NER 서버는 Deployment ID, LPL Registry 또는
후속 LLM 탐지를 알지 못하며 전달받은 원문을 서버의 고정 탐지 정책으로 처리한다.

## 2. 파일 구성

```text
services/gliner_ner/
├─ Dockerfile
├─ .dockerignore
├─ requirements-torch.txt
├─ requirements.txt
└─ gliner_ner_server/
   ├─ main.py
   ├─ contracts.py
   ├─ inference.py
   └─ settings.py
```

- `main.py`: 모델 lifespan, `/health`, `/v1/ner/detect`와 안전한 오류 응답
- `contracts.py`: 추가 필드를 거부하는 엄격한 요청·응답 모델
- `inference.py`: 모델 다운로드·1회 로딩, GPU 호출 직렬화와 출력 검증
- `settings.py`: 모델 revision, 장치, 캐시와 입력 상한 환경 설정
- `requirements-torch.txt`: CUDA 12.8용 PyTorch 고정
- `requirements.txt`: FastAPI와 GLiNER 런타임 버전 고정

## 3. HTTP 계약

### 3.1 상태 확인

```http
GET /health
```

이 Endpoint는 모델 다운로드와 GPU 로딩이 끝난 뒤에만 응답한다.

```json
{
  "status": "ok",
  "modelName": "urchade/gliner_multi-v2.1",
  "modelRevision": "443d26d654e0324125a96bebd8e796c14ff2efe6",
  "device": "cuda"
}
```

### 3.2 개체 탐지

```http
POST /v1/ner/detect
Content-Type: application/json
```

```json
{
  "text": "연락처는 010-1234-5678입니다."
}
```

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

요청과 응답의 추가 필드를 거부한다. 모델 결과도 비신뢰 데이터로 취급하여 span,
원문 조각, 내부 라벨과 유한한 `0~1` 점수를 다시 검증한 뒤 반환한다. 오류 응답에는
사용자 원문이나 모델 예외 메시지를 포함하지 않는다.

서버의 공개 응답 `type`은 LPL 공통 14개 정책 코드로 제한한다. 이 중 GLiNER가
주 탐지기로 담당하는 `PERSONAL_IDENTITY`, `LOCATION`만 고정 추론 라벨로 사용한다.
각 코드는
모델 추론 직전에 의미가 분명한 영어 라벨로 변환하고, 모델 결과는 다시 같은
정책 코드로 복원한다. `P02`, `P03`, `P05`는 주로 Gateway Regex가 후보를 만들고,
나머지 정책과 비표준 형식 보완은 후속 LLM 탐지가 담당한다.
클라이언트는 라벨이나 임계값을 요청으로 선택할 수 없다.

## 4. 장문 입력 처리

기본 모델의 `max_len`은 384다. GLiNER 기본 동작은 이를 넘는 입력을 앞부분만
남기고 잘라 추론할 수 있지만, 개인정보 탐지에서 조용한 누락은 허용할 수 없다.
따라서 서버는 다음 제한 중 하나라도 넘으면 `413 NER_INPUT_TOO_LONG`으로 요청
전체를 거부한다.

- 환경변수 `GLINER_MAX_TEXT_CHARACTERS`의 문자 수 상한
- 모델 `max_len`의 word token 상한
- backbone Encoder의 실제 `max_position_embeddings` subword 상한

현재 버전은 장문 window 분할을 지원하지 않는다. Gateway는 413을 일반 성공으로
처리하면 안 되며 요청을 실패시키거나, 향후 overlap windowing을 구현한 버전으로
교체해야 한다.

## 5. 이미지 빌드

EC2가 `x86_64`이므로 `linux/amd64`로 빌드한다.

```bash
docker buildx build \
  --platform linux/amd64 \
  --tag kss418/gliner-ner:amd64 \
  --load \
  services/gliner_ner
```

이미지는 Python 3.12, PyTorch CUDA 12.8과 GLiNER 0.2.28을 포함한다. 모델
가중치는 이미지에 넣지 않는다. 기본 모델의 SafeTensors와 설정을 고정 revision에서
받고, 내부 `microsoft/mdeberta-v3-base`의 설정과 Tokenizer도 별도의 고정
revision에서 받는다. 원본 Hugging Face 캐시는 수정하지 않고 로컬 실행용 설정에
고정 backbone snapshot 경로를 조립한다.

## 6. EC2 실행

LPL과 동일한 Docker network와 모델 캐시용 named volume을 준비한다.

```bash
sudo docker network inspect lpl-network >/dev/null 2>&1 \
  || sudo docker network create lpl-network

sudo docker volume inspect gliner-cache >/dev/null 2>&1 \
  || sudo docker volume create gliner-cache
```

NER 서버를 실행한다.

```bash
sudo docker run -d \
  --name ner-server \
  --restart unless-stopped \
  --init \
  --gpus all \
  --network lpl-network \
  -p 127.0.0.1:8008:8008 \
  -v gliner-cache:/cache \
  kss418/gliner-ner:amd64
```

첫 실행은 모델을 다운로드하므로 수 분이 걸릴 수 있다. 캐시 볼륨이 유지되면
컨테이너를 다시 만들어도 모델을 다시 받지 않는다.

```bash
sudo docker logs -f ner-server
curl http://127.0.0.1:8008/health
```

CPU 개발 실행이 꼭 필요할 때만 `-e GLINER_DEVICE=cpu`를 사용한다. 운영 기본값은
`cuda`이며 `--gpus all`을 빠뜨리면 느린 CPU로 자동 전환하지 않고 시작에 실패한다.

## 7. LPL Deployment 등록

두 컨테이너가 `lpl-network`에 있으므로 LPL에서 `localhost`가 아니라 컨테이너
DNS 이름 `ner-server`를 사용한다.

```bash
curl -X POST http://127.0.0.1:8000/deployments/ner \
  -H "Content-Type: application/json" \
  -d '{
    "deploymentId": "ner-gliner-multi",
    "baseUrl": "http://ner-server:8008/v1/ner/detect",
    "timeoutMs": 30000,
    "enabled": true
  }'
```

연결 검사는 기존 관리용 Probe를 그대로 사용한다.

```bash
curl -X POST \
  http://127.0.0.1:8000/deployments/ner/ner-gliner-multi/probe
```

NER Deployment에는 `kind`, `adapterType`과 `modelName`을 넣지 않는다. LPL이
등록된 `baseUrl` 전체 Endpoint를 그대로 호출하므로 경로까지 함께 등록해야 한다.
query, fragment와 URL userinfo는 허용하지 않는다.

## 8. 환경변수

| 환경변수 | 기본값 | 역할 |
|---|---|---|
| `GLINER_MODEL_NAME` | `urchade/gliner_multi-v2.1` | 로드할 Hugging Face 모델 |
| `GLINER_MODEL_REVISION` | 고정 commit SHA | 모델 파일 revision |
| `GLINER_BACKBONE_NAME` | `microsoft/mdeberta-v3-base` | 내부 Transformer 모델 |
| `GLINER_BACKBONE_REVISION` | 고정 commit SHA | 내부 Transformer 설정·Tokenizer revision |
| `GLINER_DEVICE` | `cuda` | `cuda`, `cpu`, `auto` 중 하나 |
| `GLINER_CACHE_DIR` | `/cache/huggingface` | 모델 캐시 경로 |
| `GLINER_MAX_TEXT_CHARACTERS` | `100000` | JSON 원문 문자 수 1차 상한 |
| `GLINER_LOCAL_FILES_ONLY` | `false` | 예열된 캐시만 사용할지 여부 |

완전히 오프라인으로 실행하려면 한 번 정상 기동하여 캐시를 예열한 뒤
`GLINER_LOCAL_FILES_ONLY=true`로 컨테이너를 다시 생성한다.
