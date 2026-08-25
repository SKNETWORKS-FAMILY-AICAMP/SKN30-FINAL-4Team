import asyncio
import hashlib
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

import olefile
from sqlalchemy import Engine, text

from app.core.upload_limits import MAX_UPLOAD_BYTES
from app.ports.object_storage import ObjectStorage


logger = logging.getLogger(__name__)
HWP_COMPOUND_FILE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
HWP_FILE_HEADER_SIGNATURE = b"HWP Document File"
HWPX_MIMETYPE = b"application/hwp+zip"


class InvalidUploadError(ValueError):
    pass


class UploadTooLargeError(InvalidUploadError):
    pass


class UnsupportedDocumentError(InvalidUploadError):
    pass


@dataclass(frozen=True)
class ValidatedUpload:
    original_filename: str
    declared_format: Literal["HWP", "HWPX"]
    extension: Literal["hwp", "hwpx"]
    detected_mime_type: str
    size_bytes: int
    sha256_hex: str


@dataclass(frozen=True)
class CreatedCase:
    case_id: int
    status: Literal["UPLOADED"] = "UPLOADED"


def _safe_filename(filename: str | None) -> str:
    if filename is None or "\x00" in filename:
        raise InvalidUploadError("A valid filename is required")

    value = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not value
        or len(value) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InvalidUploadError("A valid filename is required")
    return value


def _detect_document_format(
    content: BinaryIO,
) -> tuple[Literal["HWP", "HWPX"], str]:
    content.seek(0)
    signature = content.read(len(HWP_COMPOUND_FILE_SIGNATURE))
    content.seek(0)

    if signature == HWP_COMPOUND_FILE_SIGNATURE:
        try:
            with olefile.OleFileIO(content) as compound_file:
                if not compound_file.exists("FileHeader"):
                    raise UnsupportedDocumentError("Invalid HWP document")
                file_header = compound_file.openstream("FileHeader").read(32)
        except (OSError, ValueError, olefile.OleFileError) as error:
            raise UnsupportedDocumentError("Invalid HWP document") from error
        finally:
            content.seek(0)

        if (
            len(file_header) != 32
            or file_header.rstrip(b"\x00") != HWP_FILE_HEADER_SIGNATURE
        ):
            raise UnsupportedDocumentError("Invalid HWP document")
        return "HWP", "application/x-hwp"

    try:
        with zipfile.ZipFile(content) as archive:
            if archive.namelist().count("mimetype") != 1:
                raise UnsupportedDocumentError("Invalid HWPX document")
            mimetype_info = archive.getinfo("mimetype")
            if mimetype_info.file_size > len(HWPX_MIMETYPE):
                raise UnsupportedDocumentError("Invalid HWPX document")
            if archive.read(mimetype_info) != HWPX_MIMETYPE:
                raise UnsupportedDocumentError("Invalid HWPX document")
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise UnsupportedDocumentError("Invalid HWPX document") from error
    finally:
        content.seek(0)

    return "HWPX", HWPX_MIMETYPE.decode("ascii")


def validate_upload(filename: str | None, content: BinaryIO) -> ValidatedUpload:
    original_filename = _safe_filename(filename)
    extension = Path(original_filename).suffix.lower().removeprefix(".")
    if extension not in {"hwp", "hwpx"}:
        raise UnsupportedDocumentError("Only HWP and HWPX files are supported")

    digest = hashlib.sha256()
    size_bytes = 0
    content.seek(0)
    while chunk := content.read(1024 * 1024):
        size_bytes += len(chunk)
        if size_bytes > MAX_UPLOAD_BYTES:
            content.seek(0)
            raise UploadTooLargeError("File exceeds the 50MB limit")
        digest.update(chunk)
    content.seek(0)

    if size_bytes == 0:
        raise InvalidUploadError("File must not be empty")

    declared_format, detected_mime_type = _detect_document_format(content)
    if declared_format.lower() != extension:
        raise UnsupportedDocumentError("Filename extension does not match file content")

    return ValidatedUpload(
        original_filename=original_filename,
        declared_format=declared_format,
        extension=extension,
        detected_mime_type=detected_mime_type,
        size_bytes=size_bytes,
        sha256_hex=digest.hexdigest(),
    )


async def create_case_from_upload(
    engine: Engine,
    storage: ObjectStorage,
    owner_user_id: int,
    filename: str | None,
    content: BinaryIO,
) -> CreatedCase:
    validation_task = asyncio.create_task(
        asyncio.to_thread(validate_upload, filename, content)
    )
    try:
        upload = await asyncio.shield(validation_task)
    except asyncio.CancelledError:
        try:
            await validation_task
        finally:
            raise
    storage_key: str | None = None
    cleanup_keys: set[str] = set()

    try:
        with engine.begin() as connection:
            case_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.inspection_case (owner_user_id)
                    VALUES (:owner_user_id)
                    RETURNING id
                    """
                ),
                {"owner_user_id": owner_user_id},
            )
            if case_id is None:
                raise RuntimeError("Failed to create inspection case")

            storage_key = (
                f"users/{owner_user_id}/cases/{case_id}/"
                f"{uuid4().hex}.{upload.extension}"
            )
            cleanup_keys.add(storage_key)
            stored = await storage.put(storage_key, content)
            cleanup_keys.add(stored.key)
            if stored.key != storage_key:
                raise RuntimeError("Object storage returned a different key")
            if stored.size_bytes != upload.size_bytes:
                raise RuntimeError("Stored file size does not match upload")

            file_asset_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.file_asset (
                        asset_scope,
                        owner_user_id,
                        inspection_case_id,
                        storage_key,
                        original_filename,
                        detected_mime_type,
                        extension,
                        size_bytes,
                        sha256_hex
                    )
                    VALUES (
                        'USER',
                        :owner_user_id,
                        :case_id,
                        :storage_key,
                        :original_filename,
                        :detected_mime_type,
                        :extension,
                        :size_bytes,
                        :sha256_hex
                    )
                    RETURNING id
                    """
                ),
                {
                    "owner_user_id": owner_user_id,
                    "case_id": case_id,
                    "storage_key": storage_key,
                    "original_filename": upload.original_filename,
                    "detected_mime_type": upload.detected_mime_type,
                    "extension": upload.extension,
                    "size_bytes": upload.size_bytes,
                    "sha256_hex": upload.sha256_hex,
                },
            )
            if file_asset_id is None:
                raise RuntimeError("Failed to create file asset")

            connection.execute(
                text(
                    """
                    INSERT INTO sims.uploaded_document (
                        inspection_case_id,
                        file_asset_id,
                        declared_format
                    )
                    VALUES (:case_id, :file_asset_id, :declared_format)
                    """
                ),
                {
                    "case_id": case_id,
                    "file_asset_id": file_asset_id,
                    "declared_format": upload.declared_format,
                },
            )
    except BaseException:
        for cleanup_key in cleanup_keys:
            try:
                await storage.delete(cleanup_key)
            except Exception:
                logger.exception("Failed to compensate stored upload: %s", cleanup_key)
        raise

    return CreatedCase(case_id=case_id)
