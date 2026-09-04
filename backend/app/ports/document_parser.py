from dataclasses import dataclass
from typing import BinaryIO, Protocol

from app.schemas.parsed_document import ParsedDocument


@dataclass(frozen=True)
class FileSource:
    content: BinaryIO
    filename: str
    mime_type: str
    extension: str


class DocumentParser(Protocol):
    name: str
    version: str

    def supports(self, mime_type: str, extension: str) -> bool: ...

    async def parse(self, source: FileSource) -> ParsedDocument: ...
