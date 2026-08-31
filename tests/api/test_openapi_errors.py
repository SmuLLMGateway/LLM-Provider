"""실제 공통 오류 응답과 OpenAPI 문서의 계약이 일치하는지 검증합니다."""

from __future__ import annotations

from app.main import create_app


def test_validation_capable_operations_document_safe_error_response() -> None:
    """요청 검증이 가능한 모든 작업은 공통 422 모델을 공개합니다."""

    document = create_app().openapi()
    paths = document["paths"]
    operations = [
        ("/detect", "post"),
        ("/generate", "post"),
        ("/mask", "post"),
        ("/titles", "post"),
    ]
    for kind in ("ner", "llm"):
        operations.extend(
            [
                (f"/deployments/{kind}", "post"),
                (f"/deployments/{kind}/{{deployment_id}}", "get"),
                (f"/deployments/{kind}/{{deployment_id}}", "put"),
                (f"/deployments/{kind}/{{deployment_id}}", "delete"),
                (
                    f"/deployments/{kind}/{{deployment_id}}/enabled",
                    "patch",
                ),
                (
                    f"/deployments/{kind}/{{deployment_id}}/probe",
                    "post",
                ),
            ]
        )

    for path, method in operations:
        validation_response = paths[path][method]["responses"]["422"]
        schema = validation_response["content"]["application/json"][
            "schema"
        ]
        assert schema == {
            "$ref": "#/components/schemas/ApiErrorResponse"
        }


def test_openapi_error_schema_contains_only_safe_public_fields() -> None:
    """OpenAPI에서 입력값을 담는 FastAPI 기본 오류 구조를 제거합니다."""

    schemas = create_app().openapi()["components"]["schemas"]

    assert set(schemas["ApiErrorResponse"]["properties"]) == {"detail"}
    assert set(schemas["ApiErrorDetail"]["properties"]) == {
        "code",
        "message",
    }
    assert schemas["ApiErrorDetail"]["required"] == ["code", "message"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_detect_documents_input_too_large_error_response() -> None:
    """표준 NER 장문 입력 오류를 /detect OpenAPI에도 공개합니다."""

    responses = create_app().openapi()["paths"]["/detect"]["post"][
        "responses"
    ]
    schema = responses["413"]["content"]["application/json"][
        "schema"
    ]

    assert schema == {
        "$ref": "#/components/schemas/ApiErrorResponse"
    }
