from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentBlock(BaseModel):
    block_id: str
    block_type: Literal["paragraph", "table", "ocr", "heading"]
    text: str
    page_no: int | None = None
    section_path: list[str] = Field(default_factory=list)
    source_locator: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    parser_name: str
    parser_version: str
    text: str
    blocks: list[DocumentBlock]
    warnings: list[str] = Field(default_factory=list)
    partial: bool = False
