from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CplFieldCode(StrEnum):
    REQUEST_TYPE = "REQUEST_TYPE"
    PURPOSE_GOAL = "PURPOSE_GOAL"
    IMPLEMENTATION_PLAN = "IMPLEMENTATION_PLAN"
    BUSINESS_PERIOD = "BUSINESS_PERIOD"
    NEW_OR_CHANGED_CONTENT = "NEW_OR_CHANGED_CONTENT"
    BUSINESS_NEED = "BUSINESS_NEED"
    LEGAL_BASIS = "LEGAL_BASIS"
    LINKED_POLICY = "LINKED_POLICY"
    BUDGET = "BUDGET"
    TARGET_AND_CONDITIONS = "TARGET_AND_CONDITIONS"
    SUPPORT_CONTENT_AND_SCALE = "SUPPORT_CONTENT_AND_SCALE"
    DELIVERY_SYSTEM = "DELIVERY_SYSTEM"
    EXPECTED_EFFECTS_AND_PERFORMANCE = "EXPECTED_EFFECTS_AND_PERFORMANCE"


CPL_FIELDS: tuple[CplFieldCode, ...] = tuple(CplFieldCode)
CPL_TOTAL_FIELDS = len(CPL_FIELDS)


class CplStatus(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    PARSE_FAILED = "PARSE_FAILED"


class CplOccurrence(BaseModel):
    raw_text: str
    normalized_value: Any | None = None
    page_no: int | None = Field(default=None, ge=1)
    section_path: list[str] = Field(default_factory=list)
    source_locator: dict[str, Any] = Field(default_factory=dict)
    block_id: str
    extraction_method: Literal["RULE", "LLM"]


class CplSemanticOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    raw_text: str
    normalized_value: str | None


class CplSemanticItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_code: CplFieldCode
    status: Literal["PRESENT", "MISSING", "NEEDS_CONFIRMATION"]
    occurrences: list[CplSemanticOccurrence]
    reason_code: str | None
    explanation: str | None


class CplSemanticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CplSemanticItem]


class CplItem(BaseModel):
    field_code: CplFieldCode
    status: CplStatus
    occurrences: list[CplOccurrence] = Field(default_factory=list)
    reason_code: str | None = None
    explanation: str | None = None


class CplResult(BaseModel):
    ruleset_version: str
    items: list[CplItem]
    warnings: list[str] = Field(default_factory=list)
    model_profile: str | None = None
    prompt_version: str | None = None
    confirmed_count: int = 0
    total_count: int = CPL_TOTAL_FIELDS
    confirmation_rate: float = 0.0

    @model_validator(mode="after")
    def validate_and_calculate_summary(self) -> "CplResult":
        codes = [item.field_code for item in self.items]
        if len(codes) != CPL_TOTAL_FIELDS or set(codes) != set(CPL_FIELDS):
            raise ValueError("CPL result must contain each of the 13 fields exactly once")
        if len(codes) != len(set(codes)):
            raise ValueError("CPL result contains duplicate fields")

        confirmed_count = sum(
            item.status in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
            for item in self.items
        )
        object.__setattr__(self, "confirmed_count", confirmed_count)
        object.__setattr__(self, "total_count", CPL_TOTAL_FIELDS)
        object.__setattr__(
            self,
            "confirmation_rate",
            confirmed_count / CPL_TOTAL_FIELDS * 100,
        )
        return self
