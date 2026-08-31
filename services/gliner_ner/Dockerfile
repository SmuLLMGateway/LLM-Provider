FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TOKENIZERS_PARALLELISM=false \
    HOME=/home/gliner \
    HF_HOME=/cache/huggingface \
    HF_HUB_CACHE=/cache/huggingface/hub \
    TORCH_HOME=/cache/torch \
    GLINER_CACHE_DIR=/cache/huggingface \
    GLINER_MODEL_NAME=urchade/gliner_multi-v2.1 \
    GLINER_MODEL_REVISION=443d26d654e0324125a96bebd8e796c14ff2efe6 \
    GLINER_BACKBONE_NAME=microsoft/mdeberta-v3-base \
    GLINER_BACKBONE_REVISION=a0484667b22365f84929a935b5e50a51f71f159d \
    GLINER_DEVICE=cuda

WORKDIR /srv/gliner

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 gliner \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --create-home \
        --home-dir /home/gliner \
        --shell /usr/sbin/nologin \
        gliner \
    && mkdir -p /cache/huggingface /cache/torch \
    && chown -R 10001:10001 /cache /home/gliner

COPY requirements-torch.txt requirements.txt ./
RUN python -m pip install --requirement requirements-torch.txt \
    && python -m pip install --requirement requirements.txt

COPY --chown=10001:10001 gliner_ner_server ./gliner_ner_server

USER gliner

EXPOSE 8008

HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=10m \
    --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/health', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "gliner_ner_server.main:app", "--host", "0.0.0.0", "--port", "8008", "--workers", "1"]
