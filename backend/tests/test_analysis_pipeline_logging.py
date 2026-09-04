import logging

from pydantic import BaseModel, ValidationError

from app.services.analysis_pipeline import _log_semantic_failure


class _Response(BaseModel):
    count: int


def test_cpl_failure_log_omits_validation_input(
    caplog,
) -> None:
    secret = "request-document-secret"
    try:
        _Response.model_validate({"count": secret})
    except ValidationError as error:
        with caplog.at_level(logging.WARNING):
            _log_semantic_failure("LLM_INVALID_RESPONSE", error)

    assert secret not in caplog.text
    assert "count:int_parsing" in caplog.text
