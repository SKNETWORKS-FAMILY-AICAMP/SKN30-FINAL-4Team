import asyncio
import io
import stat
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import PurePosixPath
from typing import Any

import olefile

try:
    import rhwp

    _RHWP_VERSION = version("rhwp-python") or "unknown"
except (ImportError, OSError, PackageNotFoundError) as error:  # pragma: no cover - host dependency
    # rhwp-python ships a native extension.  Keep the application importable
    # when that extension cannot load (for example on a worker without its
    # runtime DLL); parsing then fails with the same safe domain error as any
    # other parser failure.  Tests and a correctly provisioned host can still
    # replace ``rhwp.Document.from_bytes`` normally.
    _RHWP_IMPORT_ERROR = error

    class _UnavailableDocument:
        @staticmethod
        def from_bytes(*_args: Any, **_kwargs: Any) -> Any:
            raise DocumentParsingError(
                "HWP parser dependency is unavailable"
            ) from _RHWP_IMPORT_ERROR

    class _UnavailableRhwp:
        Document = _UnavailableDocument

    rhwp = _UnavailableRhwp()
    # 배포판이 없으면 version() 도 PackageNotFoundError 를 올린다. 클래스
    # 본문에서 부르면 이 fallback 이 무의미해지므로 여기서 함께 처리한다.
    _RHWP_VERSION = "unavailable"

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
    version = _RHWP_VERSION

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
        extracted_text = document.extract_text()
        ir = document.to_ir()

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
                cells = [_table_cell(cell) for cell in block.cells]
                locator.update(
                    {
                        "rows": block.rows,
                        "cols": block.cols,
                        "cells": cells,
                    }
                )
                for cell in cells:
                    if cell.get("structure_status") == "unresolved":
                        warnings.append(
                            "TABLE_CELL_INLINE_SEGMENTS: "
                            f"{block_id}:cell:{cell.get('row')}:{cell.get('col')} "
                            "preserved as segments; paragraph boundaries are not "
                            "inferred"
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

        # rhwp's plain-text view can omit table content (the mockup samples do
        # this), so the persisted extraction must be rebuilt from the structured
        # blocks.  Cell ``segments`` are used only for a readable projection;
        # the cell's raw ``text`` remains unchanged in the IR locator.
        text = _canonical_text(blocks, fallback=extracted_text)
        if not text.strip():
            raise DocumentParsingError("Document contains no extractable text")

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
    children = [
        child
        for child in getattr(cell, "blocks", [])
        if isinstance(getattr(child, "text", None), str)
    ]
    paragraphs = [child.text for child in children]
    result = {
        "row": cell.row,
        "col": cell.col,
        "row_span": cell.row_span,
        "col_span": cell.col_span,
        "grid_index": cell.grid_index,
        "role": cell.role,
        "text": "\n".join(paragraphs),
    }

    # A well-formed cell normally exposes one ParagraphBlock per paragraph.
    # Some HWP/HWPX samples expose several contiguous inline runs in one
    # ParagraphBlock. Keep those runs verbatim and expose their indices so
    # downstream evidence extraction can work at the smallest available unit
    # without pretending they are paragraph boundaries.
    if len(paragraphs) > 1:
        result["paragraphs"] = paragraphs

    inline_runs = [_inline_segments(child) for child in children]
    if any(len(segments) > 1 for segments in inline_runs):
        segment_values: list[str] = []
        for child, segments in zip(children, inline_runs, strict=True):
            # Once one paragraph is flattened into runs, retain every available
            # run in order.  Single-run paragraphs remain useful neighbours for
            # the same cell, while the paragraph list above keeps their original
            # grouping in the raw locator.
            segment_values.extend(segments or [child.text])
        result["segments"] = [
            {"segment_index": index, "text": value}
            for index, value in enumerate(segment_values)
            if value.strip()
        ]
        result["structure_status"] = "unresolved"
    return result


def _inline_segments(block: Any) -> list[str]:
    inlines = getattr(block, "inlines", None)
    if not isinstance(inlines, (list, tuple)):
        return []
    return [
        value
        for inline in inlines
        if isinstance(value := getattr(inline, "text", None), str)
        and value.strip()
    ]


def _canonical_text(
    blocks: list[DocumentBlock],
    *,
    fallback: str,
) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.block_type != "table":
            if block.text.strip():
                parts.append(block.text)
            continue

        cells = block.source_locator.get("cells")
        if not isinstance(cells, list):
            if block.text.strip():
                parts.append(block.text)
            continue

        cell_parts: list[str] = []
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            segments = cell.get("segments")
            if isinstance(segments, list):
                segment_texts = [
                    segment.get("text")
                    for segment in segments
                    if isinstance(segment, dict)
                    and isinstance(segment.get("text"), str)
                    and segment["text"].strip()
                ]
                if (
                    segment_texts
                    and isinstance(cell.get("text"), str)
                    and "".join(segment_texts) == cell["text"]
                ):
                    cell_parts.append("\n".join(segment_texts))
                    continue
            value = cell.get("text")
            if isinstance(value, str) and value.strip():
                cell_parts.append(value)
        if cell_parts:
            parts.append("\n".join(cell_parts))
        elif block.text.strip():
            parts.append(block.text)

    if not parts:
        return fallback

    canonical = "\n".join(parts)
    return canonical
