"""Pin and exercise every announcement structured-output wire schema."""

from __future__ import annotations

import openai
from jsonschema import Draft202012Validator
from openai.lib._parsing import type_to_response_format_param
import pytest

import worker.vendor  # noqa: F401  # installs the vendored package paths

from semantic_structuring.notice_preparation import SectionScopeDiscovery
from semantic_structuring.run_block_candidate_discovery_test import (
    BlockCandidateDiscovery,
)
from semantic_structuring.source_selection import (
    AnchorCorrectionResponse,
    SourceSelectionExtractionV02,
)


def test_openai_sdk_version_matches_the_private_schema_helper_pin() -> None:
    """Make a deliberate SDK upgrade update this private-helper contract too."""

    assert openai.__version__ == "2.54.0"


@pytest.mark.parametrize(
    "response_schema",
    [
        SectionScopeDiscovery,
        BlockCandidateDiscovery,
        AnchorCorrectionResponse,
        SourceSelectionExtractionV02,
    ],
)
def test_announcement_response_schema_is_strict_openai_json_schema(
    response_schema: type,
) -> None:
    response_format = type_to_response_format_param(response_schema)

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == response_schema.__name__
    Draft202012Validator.check_schema(response_format["json_schema"]["schema"])
