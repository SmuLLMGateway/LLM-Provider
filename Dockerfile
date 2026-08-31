FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 애플리케이션은 권한이 제한된 사용자로 실행합니다.
RUN groupadd --gid 10001 lpl \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --no-create-home \
        --shell /usr/sbin/nologin \
        lpl

COPY requirements.txt ./
RUN python -m pip install --requirement requirements.txt

COPY app ./app
COPY --chown=10001:10001 config ./config

# 빈 객체도 허용되는 종류별 Registry 파일이 모두 있어야 합니다.
RUN test -f /app/config/ner_deployments.json \
    && test -f /app/config/llm_deployments.json \
    && test -f /app/config/prompts.j2 \
    && test -f /app/config/policy_prompts.json \
    && test -f /app/config/mask_prompt.j2 \
    && test -f /app/config/title_prompt.j2 \
    && test -f /app/config/policy_settings.json \
    && test ! -e /app/config/deployments.json

USER lpl

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
