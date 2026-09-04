from fastapi import FastAPI
from pydantic import BaseModel

from app.api.v1.responses import describe
from app.core.config import Settings
from main import create_app


class StatusResponse(BaseModel):
    case_id: int
    status: str


def test_describe_success_override_keeps_route_response_model() -> None:
    app = FastAPI()

    @app.get(
        "/status",
        response_model=StatusResponse,
        responses=describe({}, _200="현재 상태"),
    )
    def status() -> StatusResponse:
        return StatusResponse(case_id=1, status="COMPLETED")

    response = app.openapi()["paths"]["/status"]["get"]["responses"]["200"]

    assert response["description"] == "현재 상태"
    assert response["content"]["application/json"]["schema"]["$ref"].endswith(
        "/StatusResponse"
    )


def test_public_openapi_matches_frontend_response_contract() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="x" * 32,
    )
    schema = create_app(settings).openapi()

    assert schema["info"].get("description") is None
    assert schema["paths"]["/api/v1/cases"]["post"]["responses"].keys() >= {
        "200",
        "400",
        "401",
        "413",
        "415",
    }
    assert "202" not in schema["paths"]["/api/v1/cases"]["post"]["responses"]
    assert "422" not in {
        code
        for path in schema["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict)
        for code in operation.get("responses", {})
        if code == "422"
    }

    assert set(
        schema["components"]["schemas"]["CaseStatusResponse"]["properties"]
    ) == {"status"}
    assert set(
        schema["components"]["schemas"]["ReportCaseDisplay"]["properties"]
    ) == {"title", "completed_at"}
    assert set(schema["components"]["schemas"]["ChatMessageResponse"]["properties"]) == {
        "id",
        "role",
        "content",
    }
    assert set(schema["components"]["schemas"]["CplDisplay"]["properties"]) == {
        "confirmed_count",
        "items",
    }
    assert set(
        schema["components"]["schemas"]["FitAvailabilityDisplay"]["properties"]
    ) == {"assessable_count"}
