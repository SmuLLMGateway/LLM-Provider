"""브라우저 기능 테스트 UI의 정적 제공 계약을 검증합니다."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client_without_lifespan() -> Iterator[TestClient]:
    """애플리케이션 lifespan 실행 없이 정적 파일을 제공합니다."""

    client = TestClient(create_app())
    try:
        yield client
    finally:
        client.close()


def test_ui_index_is_served_without_application_runtime(
    client_without_lifespan: TestClient,
) -> None:
    """모델 Runtime이 시작되지 않아도 테스트 화면을 제공합니다."""

    response = client_without_lifespan.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    html = response.text
    for text in (
        "LPL",
        "NER",
        "LLM",
        "Detect",
        "Generate",
        "Title",
    ):
        assert text in html

    for form_id in (
        "deployment-form",
        "detect-form",
        "generate-form",
        "title-form",
    ):
        assert re.search(
            rf'<form\b[^>]*\bid=["\']{re.escape(form_id)}["\']',
            html,
            re.IGNORECASE,
        )
    assert "deployment-adapter-config" not in html
    assert 'id="detect-regex-candidates"' in html
    assert 'id="detect-organization-profile"' in html
    assert 'id="detect-source-type"' in html
    assert 'id="generate-previous-text"' in html
    assert "detect-existing" not in html
    assert (
        'placeholder="http://ner-server:8008/v1/ner/detect"'
        in html
    )
    for removed_field_id in (
        "deployment-display-name",
        "deployment-model-id",
        "deployment-description",
    ):
        assert removed_field_id not in html


def test_ui_stylesheet_is_served(
    client_without_lifespan: TestClient,
) -> None:
    """브라우저가 UI 스타일시트를 직접 불러올 수 있습니다."""

    response = client_without_lifespan.get("/ui/styles.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert response.text.strip()


def test_ui_script_is_served_and_targets_supported_apis(
    client_without_lifespan: TestClient,
) -> None:
    """UI 스크립트가 요청된 모든 API 흐름을 포함하는지 검증합니다."""

    response = client_without_lifespan.get("/ui/app.js")

    assert response.status_code == 200
    media_type = response.headers["content-type"].split(";", 1)[0]
    assert media_type in {
        "application/javascript",
        "text/javascript",
    }

    script = response.text
    for api_path in (
        "/adapters/llm",
        "/deployments/ner",
        "/deployments/llm",
        "/probe",
        "/detect",
        "/generate",
        "/titles",
    ):
        assert api_path in script
    assert "/adapters/ner" not in script
    assert "innerHTML" not in script
    assert "body.adapterConfig" not in script
    assert "syncAdapterSelect(detail.adapterType)" in script
    assert 'state.deploymentKind === "llm"' in script
    assert "http://ner-server:8008/v1/ner/detect" in script
    assert "body.adapterType" in script
    assert "deploymentDetailRequestVersion" in script
    assert "regexCandidates: parseRegexCandidates()" in script
    assert "organizationProfile: parseOptionalJsonObject(" in script
    assert "if (!raw) return null;" in script
    assert 'sourceType: element("detect-source-type").value' in script
    assert 'element("generate-previous-text").value' in script
    assert "previousText: parsePreviousText()" in script
    assert "previousText는 JSON 배열이어야 합니다" in script
    assert "existingDetections: parseExistingDetections()" not in script
    for removed_reference in (
        "deployment.displayName",
        "detail.displayName",
        "detail.modelId",
        "detail.description",
        "body.modelInfo",
        'payload["modelInfo"]',
    ):
        assert removed_reference not in script


@pytest.mark.parametrize(
    "path",
    (
        "/ui/%2e%2e/main.py",
        "/ui/%2e%2e%2fmain.py",
    ),
)
def test_ui_static_mount_rejects_path_traversal(
    client_without_lifespan: TestClient,
    path: str,
) -> None:
    """정적 Mount를 통해 UI 폴더 밖의 파일을 읽을 수 없습니다."""

    response = client_without_lifespan.get(path)

    assert response.status_code == 404
    assert "def create_app" not in response.text


def test_ui_static_mount_is_not_an_openapi_operation() -> None:
    """정적 파일 Mount가 OpenAPI 작업으로 노출되지 않는지 검증합니다."""

    document = create_app().openapi()

    assert not any(path.startswith("/ui") for path in document["paths"])
