import asyncio
import hashlib
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

import olefile
from sqlalchemy import Connection, Engine, text

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
    created_at: datetime
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


def validate_declared_upload(
    filename: str | None, declared_size_bytes: int
) -> Literal["hwp", "hwpx"]:
    """파일이 오기 **전에** 신고값만으로 걸러낸다.

    RUN-01 은 파일을 받지 않는다. 내용 검사는 RUN-03 이 실제 바이트로 다시
    하므로 여기서 통과했다고 정상 파일이 되는 것은 아니다. 확장자·크기가
    명백히 틀린 요청에 object key 를 예약해 주지 않는 것이 목적이다.
    """
    original_filename = _safe_filename(filename)
    extension = Path(original_filename).suffix.lower().removeprefix(".")
    if extension not in {"hwp", "hwpx"}:
        raise UnsupportedDocumentError("Only HWP and HWPX files are supported")
    if isinstance(declared_size_bytes, bool) or not isinstance(
        declared_size_bytes, int
    ):
        raise InvalidUploadError("A valid file size is required")
    if declared_size_bytes <= 0:
        raise InvalidUploadError("File must not be empty")
    if declared_size_bytes > MAX_UPLOAD_BYTES:
        raise UploadTooLargeError("File exceeds the 50MB limit")
    return extension  # type: ignore[return-value]


def record_uploaded_document(
    connection: Connection,
    *,
    owner_user_id: int,
    case_id: int,
    storage_key: str,
    upload: ValidatedUpload,
) -> int:
    """검사 건 하나에 원본 파일 두 행(file_asset + uploaded_document)을 단다.

    업로드 진입점이 둘(멀티파트 한 번 / Edge 세 번)이지만 원본 기록은 하나다.
    저장소에 바이트를 넣는 방법만 다르고 남기는 행은 같아야 한다.
    """
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
    return file_asset_id


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
            created = connection.execute(
                text(
                    """
                    INSERT INTO sims.inspection_case (owner_user_id)
                    VALUES (:owner_user_id)
                    RETURNING id, created_at
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).one_or_none()
            if created is None:
                raise RuntimeError("Failed to create inspection case")
            case_id, created_at = created

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

            record_uploaded_document(
                connection,
                owner_user_id=owner_user_id,
                case_id=case_id,
                storage_key=storage_key,
                upload=upload,
            )
    except BaseException:
        for cleanup_key in cleanup_keys:
            try:
                await storage.delete(cleanup_key)
            except Exception:
                logger.exception("Failed to compensate stored upload: %s", cleanup_key)
        raise

    return CreatedCase(case_id=case_id, created_at=created_at)
