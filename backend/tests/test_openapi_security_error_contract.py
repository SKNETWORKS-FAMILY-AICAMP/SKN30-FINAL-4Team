"""Swagger/OpenAPI contract tests for browser Cookie clients.

These tests deliberately inspect the generated document rather than route
implementation details.  A frontend team must be able to discover both the
cookie transport and the stable public error envelope without reading Python.
"""

from __future__ import annotations

from typing import Any

from main import create_app


ERROR_REF = "#/components/schemas/ErrorResponse"
ACCESS_SECURITY = [{"PreReviewAccessCookie": []}]
REFRESH_SECURITY = [{"PreReviewRefreshCookie": []}]


def _operation(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return schema["paths"][path][method]


def _assert_error_response(operation: dict[str, Any], status_code: str) -> None:
    response = operation["responses"][status_code]
    assert response["content"]["application/json"]["schema"] == {"$ref": ERROR_REF}


def test_openapi_documents_http_only_cookie_security_without_bearer_auth() -> None:
    schema = create_app().openapi()
    schemes = schema["components"]["securitySchemes"]

    assert schemes["PreReviewAccessCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "pre_review_access",
        "description": schemes["PreReviewAccessCookie"]["description"],
    }
    assert schemes["PreReviewRefreshCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "pre_review_refresh",
        "description": schemes["PreReviewRefreshCookie"]["description"],
    }
    assert "bearer" not in str(schemes).lower()

    # Public bootstrap/recovery calls do not require an existing Cookie.
    for path in (
        "/api/v1/auth/sign-in",
        "/api/v1/auth/sign-up",
        "/api/v1/auth/password-reset",
    ):
        assert "security" not in _operation(schema, path, "post")

    assert _operation(schema, "/api/v1/auth/refresh", "post")["security"] == REFRESH_SECURITY
    for path, method in (
        ("/api/v1/auth/update-password", "post"),
        ("/api/v1/auth/me", "get"),
        ("/api/v1/analysis-runs", "post"),
        ("/api/v1/analysis-runs/{analysis_run_id}", "get"),
        ("/api/v1/analysis-cases/{analysis_case_id}", "get"),
        ("/api/v1/sim-candidates/{sim_candidate_id}", "get"),
        ("/api/v1/analysis-sessions/active", "get"),
        ("/api/v1/analysis-history", "get"),
    ):
        operation = _operation(schema, path, method)
        assert operation["security"] == ACCESS_SECURITY
        parameter_names = {
            parameter.get("name") for parameter in operation.get("parameters", [])
        }
        assert "X-PreReview-Dev-User" not in parameter_names
        assert "X-PreReview-Dev-Role" not in parameter_names


def test_openapi_uses_the_named_error_response_for_all_documented_failures() -> None:
    schema = create_app().openapi()
    error_schema = schema["components"]["schemas"]["ErrorResponse"]
    assert set(error_schema["required"]) == {"code", "message"}
    assert set(error_schema["properties"]) == {"code", "message", "errors"}

    expected_errors = {
        ("/health/live", "get"): {"500"},
        ("/health/ready", "get"): {"500", "503"},
        ("/api/v1/auth/sign-in", "post"): {"401", "403", "422", "429", "500", "502", "503"},
        ("/api/v1/auth/sign-up", "post"): {"400", "403", "422", "429", "500", "502", "503"},
        ("/api/v1/auth/refresh", "post"): {"401", "403", "422", "429", "500", "502", "503"},
        ("/api/v1/auth/sign-out", "post"): {"403", "422", "500", "503"},
        ("/api/v1/auth/password-reset", "post"): {"403", "422", "500", "503"},
        ("/api/v1/auth/update-password", "post"): {"401", "403", "422", "429", "500", "502", "503"},
        ("/api/v1/auth/me", "get"): {"401", "429", "500", "502", "503"},
        ("/api/v1/analysis-runs", "post"): {"401", "403", "409", "413", "415", "422", "500", "503"},
        ("/api/v1/analysis-runs/{analysis_run_id}", "get"): {"401", "403", "404", "422", "500", "503"},
        ("/api/v1/analysis-cases/{analysis_case_id}", "get"): {"401", "403", "404", "422", "500", "503"},
        ("/api/v1/sim-candidates/{sim_candidate_id}", "get"): {"401", "403", "404", "422", "500", "503"},
        ("/api/v1/analysis-sessions/active", "get"): {"401", "403", "422", "500", "503"},
        ("/api/v1/analysis-history", "get"): {"401", "403", "422", "500", "503"},
    }
    assert len(schema["paths"]) == len(expected_errors)
    for (path, method), status_codes in expected_errors.items():
        operation = _operation(schema, path, method)
        for status_code in status_codes:
            _assert_error_response(operation, status_code)

    # Cookie-mutating operations deliberately continue to return no body on
    # their successful 204 path.
    for path in (
        "/api/v1/auth/refresh",
        "/api/v1/auth/sign-out",
        "/api/v1/auth/update-password",
    ):
        assert "content" not in _operation(schema, path, "post")["responses"]["204"]
