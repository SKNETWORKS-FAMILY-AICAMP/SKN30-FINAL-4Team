import asyncio
import io
import stat
import zipfile
from importlib.metadata import version
from pathlib import PurePosixPath
from typing import Any

import olefile
import rhwp

from app.ports.document_parser import FileSource
from app.schemas.parsed_document import DocumentBlock, ParsedDocument


class DocumentParsingError(ValueError):
    pass


MAX_HWPX_ENTRIES = 10_000
MAX_HWPX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_HWPX_COMPRESSION_RATIO = 1_000
HWP_PROTECTED_FLAGS = (1 << 1) | (1 << 2) | (1 << 4) | (1 << 8) | (1 << 10)


class RhwpDocumentParser:
    name = "rhwp-python"
    version = version("rhwp-python")

    def supports(self, mime_type: str, extension: str) -> bool:
        normalized_extension = extension.lower().removeprefix(".")
        normalized_mime = mime_type.lower().strip()
        return (normalized_extension, normalized_mime) in {
            ("hwp", "application/x-hwp"),
            ("hwpx", "application/hwp+zip"),
        }

    async def parse(self, source: FileSource) -> ParsedDocument:
        if not self.supports(source.mime_type, source.extension):
            raise DocumentParsingError("Unsupported document format")
        return await asyncio.to_thread(self._parse_sync, source)

    def _parse_sync(self, source: FileSource) -> ParsedDocument:
        source.content.seek(0)
        content = source.content.read()
        _preflight_source(content, source.extension)
        document = rhwp.Document.from_bytes(
            content,
            source_uri=source.filename,
        )
        text = document.extract_text()
        ir = document.to_ir()
        if not text.strip():
            raise DocumentParsingError("Document contains no extractable text")

        blocks: list[DocumentBlock] = []
        warnings: list[str] = []
        for block_index, block in enumerate(ir.body):
            block_type = type(block).__name__
            provenance = _provenance(block)
            locator: dict[str, Any] = {
                "body_block_index": block_index,
                **provenance,
            }
            section_index = provenance.get("section_index")
            section_path = (
                [f"section:{section_index}"] if section_index is not None else []
            )
            block_id = f"body:{block_index}"

            if block_type in {"ParagraphBlock", "ListItemBlock"}:
                blocks.append(
                    DocumentBlock(
                        block_id=block_id,
                        block_type="paragraph",
                        text=block.text,
                        section_path=section_path,
                        source_locator=locator,
                    )
                )
            elif block_type == "TableBlock":
                locator.update(
                    {
                        "rows": block.rows,
                        "cols": block.cols,
                        "cells": [_table_cell(cell) for cell in block.cells],
                    }
                )
                blocks.append(
                    DocumentBlock(
                        block_id=block_id,
                        block_type="table",
                        text=block.text,
                        section_path=section_path,
                        source_locator=locator,
                    )
                )
            else:
                warnings.append(f"Unsupported {block_type} skipped at {block_id}")

        if not blocks:
            raise DocumentParsingError("Document contains no supported text blocks")

        return ParsedDocument(
            parser_name=self.name,
            parser_version=self.version,
            text=text,
            blocks=blocks,
            warnings=warnings,
            partial=bool(warnings),
        )


def _preflight_source(content: bytes, extension: str) -> None:
    if extension.lower().removeprefix(".") == "hwp":
        try:
            with olefile.OleFileIO(io.BytesIO(content)) as compound_file:
                header = compound_file.openstream("FileHeader").read(40)
        except (OSError, ValueError, olefile.OleFileError) as error:
            raise DocumentParsingError("Invalid HWP document") from error
        if len(header) < 40:
            raise DocumentParsingError("Invalid HWP document")
        flags = int.from_bytes(header[36:40], "little")
        if flags & HWP_PROTECTED_FLAGS:
            raise DocumentParsingError("Protected HWP documents are not supported")
        return

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_HWPX_ENTRIES:
                raise DocumentParsingError("HWPX contains too many entries")

            seen_names: set[str] = set()
            total_size = 0
            for entry in entries:
                normalized_name = entry.filename.replace("\\", "/")
                path = PurePosixPath(normalized_name)
                if (
                    not normalized_name
                    or path.is_absolute()
                    or ".." in path.parts
                    or normalized_name in seen_names
                ):
                    raise DocumentParsingError("HWPX contains an unsafe entry")
                seen_names.add(normalized_name)
                if entry.flag_bits & 0x1:
                    raise DocumentParsingError("Encrypted HWPX is not supported")
                unix_mode = entry.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise DocumentParsingError("HWPX contains an unsafe entry")

                total_size += entry.file_size
                if total_size > MAX_HWPX_UNCOMPRESSED_BYTES:
                    raise DocumentParsingError("HWPX expanded content is too large")
                if (
                    entry.file_size > 0
                    and (
                        entry.compress_size == 0
                        or entry.file_size / entry.compress_size
                        > MAX_HWPX_COMPRESSION_RATIO
                    )
                ):
                    raise DocumentParsingError("HWPX compression ratio is too high")
    except zipfile.BadZipFile as error:
        raise DocumentParsingError("Invalid HWPX document") from error


def _provenance(block: Any) -> dict[str, Any]:
    value = getattr(block, "prov", None)
    if value is None:
        return {}

    locator: dict[str, Any] = {}
    if value.section_idx is not None:
        locator["section_index"] = value.section_idx
    if value.para_idx is not None:
        locator["paragraph_index"] = value.para_idx
    if value.char_start is not None:
        locator["char_start"] = value.char_start
    if value.char_end is not None:
        locator["char_end"] = value.char_end
    if value.page_range is not None:
        locator["page_range"] = value.page_range
    return locator


def _table_cell(cell: Any) -> dict[str, Any]:
    return {
        "row": cell.row,
        "col": cell.col,
        "row_span": cell.row_span,
        "col_span": cell.col_span,
        "grid_index": cell.grid_index,
        "role": cell.role,
        "text": "\n".join(
            child.text
            for child in cell.blocks
            if isinstance(getattr(child, "text", None), str)
        ),
    }
