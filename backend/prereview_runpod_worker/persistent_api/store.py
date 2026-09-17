"""Small durable store containing only sanitized job control metadata.

Signed URLs and request bodies are intentionally absent.  They live only in
the bounded in-memory queue.  After a process restart, queued/running records
therefore become ``infra_retryable`` and the trusted EC2 coordinator must mint
fresh capabilities and submit a new job.
"""

from __future__ import annotations

from datetime import UTC, datetime
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from worker.contracts.accelerator import AcceleratorJobState, AcceleratorJobStatus

__all__ = ["DurableJobStore", "JobRecord", "JobStoreError"]


_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REASON = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_MAX_RECORD_BYTES = 8 * 1024 * 1024
_SCHEMA_VERSION = "prereview.persistent-surya-job/v1"
_ENVELOPE_SCHEMA_VERSION = "prereview.persistent-surya-job-envelope/v1"
_ENVELOPE_MAC_DOMAIN = b"prereview.persistent-surya/job-envelope-mac/v1\x00"
_MAC = re.compile(r"[0-9a-f]{64}")


class JobStoreError(RuntimeError):
    """The durable job journal is unavailable or internally inconsistent."""

    def __init__(self) -> None:
        super().__init__("persistent_job_store_unavailable")


class JobRecord(BaseModel):
    """The complete allow-listed durable representation of one API job."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["prereview.persistent-surya-job/v1"] = _SCHEMA_VERSION
    job_id: str = Field(pattern=_JOB_ID.pattern)
    state: AcceleratorJobState
    logical_compute_key: str = Field(pattern=_SHA256.pattern)
    request_digest: str = Field(pattern=_SHA256.pattern)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    reason_code: str | None = Field(default=None, pattern=_REASON.pattern)
    terminal_output: AcceleratorJobStatus | None = None

    @model_validator(mode="after")
    def _coherent_lifecycle(self) -> "JobRecord":
        times = (self.created_at, self.started_at, self.completed_at)
        if any(
            value is not None and (value.tzinfo is None or value.utcoffset() is None)
            for value in times
        ):
            raise ValueError("job timestamps must be timezone-aware")
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at precedes created_at")
        if self.completed_at is not None:
            lower = self.started_at or self.created_at
            if self.completed_at < lower:
                raise ValueError("completed_at precedes the job lifecycle")

        if self.state in {AcceleratorJobState.QUEUED, AcceleratorJobState.RUNNING}:
            if (
                self.completed_at is not None
                or self.reason_code is not None
                or self.terminal_output is not None
            ):
                raise ValueError("non-terminal record carries terminal fields")
            if self.state == AcceleratorJobState.QUEUED and self.started_at is not None:
                raise ValueError("queued record cannot have started_at")
            if self.state == AcceleratorJobState.RUNNING and self.started_at is None:
                raise ValueError("running record requires started_at")
            return self

        if self.state == AcceleratorJobState.FENCE_LOST:
            raise ValueError("fence_lost is not a remote worker state")
        if self.completed_at is None:
            raise ValueError("terminal record requires completed_at")
        if self.state == AcceleratorJobState.CANCELLED:
            if self.reason_code is not None:
                raise ValueError("cancelled record must not carry a reason")
            if self.terminal_output is not None:
                output = self.terminal_output
                if (
                    output.external_job_id != self.job_id
                    or output.state != self.state
                    or output.logical_compute_key != self.logical_compute_key
                    or output.request_digest != self.request_digest
                    or output.reason_code is not None
                ):
                    raise ValueError("cancelled output does not bind to job metadata")
            return self
        if self.state == AcceleratorJobState.SUCCEEDED:
            if self.reason_code is not None or self.terminal_output is None:
                raise ValueError("succeeded record requires only terminal_output")
        elif not self.reason_code:
            raise ValueError("failed record requires reason_code")

        if self.terminal_output is not None:
            output = self.terminal_output
            if (
                output.external_job_id != self.job_id
                or output.state != self.state
                or output.logical_compute_key != self.logical_compute_key
                or output.request_digest != self.request_digest
                or output.reason_code != self.reason_code
            ):
                raise ValueError("terminal output does not bind to job metadata")
        return self

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "state": self.state.value,
            "logical_compute_key": self.logical_compute_key,
            "request_digest": self.request_digest,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "reason_code": self.reason_code,
            "output": (
                self.terminal_output.model_dump(mode="json")
                if self.terminal_output is not None
                else None
            ),
        }


class DurableJobStore:
    """Single-writer, atomically replaced, authenticated JSON journal."""

    def __init__(self, directory: Path, *, max_records: int, mac_key: bytes) -> None:
        if not isinstance(mac_key, bytes) or len(mac_key) != 32:
            raise ValueError("mac_key must be a SHA-256 key")
        self._directory = directory
        self._max_records = max_records
        self._mac_key = mac_key
        self._records: dict[str, JobRecord] = {}
        self._lock_fd: int | None = None
        self._poisoned = False

    @property
    def available(self) -> bool:
        return self._lock_fd is not None and not self._poisoned

    def open(self) -> None:
        if self._lock_fd is not None:
            raise JobStoreError()
        lock_fd: int | None = None
        try:
            self._directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            if self._directory.is_symlink() or not self._directory.is_dir():
                raise OSError
            lock_path = self._directory / ".writer.lock"
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lock_fd = os.open(lock_path, flags, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = lock_fd
            lock_fd = None
            self._records = self._load_records()
            self._poisoned = False
        except Exception:
            if lock_fd is not None:
                os.close(lock_fd)
            self.close()
            raise JobStoreError() from None

    def close(self) -> None:
        lock_fd, self._lock_fd = self._lock_fd, None
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def count(self) -> int:
        self._require_open()
        return len(self._records)

    def get(self, job_id: str) -> JobRecord | None:
        self._require_open()
        return self._records.get(job_id)

    def all_records(self) -> tuple[JobRecord, ...]:
        self._require_open()
        return tuple(self._records.values())

    def create(self, record: JobRecord) -> None:
        self._require_open()
        if record.job_id in self._records or len(self._records) >= self._max_records:
            raise JobStoreError()
        self._write(record)
        self._records[record.job_id] = record

    def replace(self, record: JobRecord) -> None:
        self._require_open()
        if record.job_id not in self._records:
            raise JobStoreError()
        self._write(record)
        self._records[record.job_id] = record

    def recover_incomplete(self, *, now: datetime) -> int:
        """Fence jobs whose capability-bearing in-memory input was lost."""

        recovered = 0
        for record in tuple(self._records.values()):
            if record.state not in {AcceleratorJobState.QUEUED, AcceleratorJobState.RUNNING}:
                continue
            replacement = record.model_copy(
                update={
                    "state": AcceleratorJobState.INFRA_RETRYABLE,
                    "completed_at": max(now, record.started_at or record.created_at),
                    "reason_code": "worker_restarted",
                }
            )
            replacement = JobRecord.model_validate(replacement.model_dump(mode="python"))
            self.replace(replacement)
            recovered += 1
        return recovered

    def fail_queued_for_shutdown(self, *, now: datetime) -> int:
        changed = 0
        for record in tuple(self._records.values()):
            if record.state != AcceleratorJobState.QUEUED:
                continue
            replacement = JobRecord.model_validate(
                record.model_copy(
                    update={
                        "state": AcceleratorJobState.INFRA_RETRYABLE,
                        "completed_at": max(now, record.created_at),
                        "reason_code": "worker_shutdown",
                    }
                ).model_dump(mode="python")
            )
            self.replace(replacement)
            changed += 1
        return changed

    def _require_open(self) -> None:
        if self._lock_fd is None or self._poisoned:
            raise JobStoreError()

    def _load_records(self) -> dict[str, JobRecord]:
        loaded: dict[str, JobRecord] = {}
        paths = sorted(self._directory.glob("*.json"))
        for path in paths:
            record = self._load_authenticated_record(path)
            if record is None:
                self._quarantine(path)
                continue
            if record.job_id in loaded or len(loaded) >= self._max_records:
                raise JobStoreError()
            loaded[record.job_id] = record
        return loaded

    def _write(self, record: JobRecord) -> None:
        protected = {
            "schema_version": _ENVELOPE_SCHEMA_VERSION,
            "record": record.model_dump(mode="json", exclude_none=False),
        }
        envelope = {
            **protected,
            "mac": self._mac(protected),
        }
        encoded = _canonical_json(envelope)
        if len(encoded) > _MAX_RECORD_BYTES:
            raise JobStoreError()
        target = self._directory / f"{record.job_id}.json"
        temporary_fd: int | None = None
        temporary_path: str | None = None
        replaced = False
        try:
            temporary_fd, temporary_path = tempfile.mkstemp(
                prefix=f".{record.job_id}.", suffix=".tmp", dir=self._directory
            )
            with os.fdopen(temporary_fd, "wb", closefd=True) as handle:
                temporary_fd = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            replaced = True
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            # After replace(), a directory-fsync failure makes durability
            # ambiguous.  Do not serve stale in-memory state or accept another
            # job; readiness closes until an operator restart/reconciliation.
            if replaced:
                self._poisoned = True
            raise JobStoreError() from None
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _load_authenticated_record(self, path: Path) -> JobRecord | None:
        """Return only a strict, MAC-bound record; bad volume data is absent."""

        try:
            if path.is_symlink() or _JOB_ID.fullmatch(path.stem) is None:
                return None
            file_stat = path.stat()
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _MAX_RECORD_BYTES:
                return None
            raw = path.read_bytes()
            if not raw:
                return None
            envelope = json.loads(
                raw,
                object_pairs_hook=_no_duplicate_object,
                parse_constant=_reject_constant,
            )
            if not _is_strict_envelope(envelope):
                return None
            protected = {
                "schema_version": envelope["schema_version"],
                "record": envelope["record"],
            }
            supplied_mac = envelope["mac"]
            if not hmac.compare_digest(supplied_mac, self._mac(protected)):
                return None
            # Pydantic's JSON mode performs the narrow datetime/enum decoding
            # that strict Python-object validation intentionally rejects.
            record = JobRecord.model_validate_json(_canonical_json(envelope["record"]))
            if record.job_id != path.stem:
                return None
            return record
        except Exception:
            return None

    def _mac(self, protected: dict[str, object]) -> str:
        return hmac.new(
            self._mac_key,
            _ENVELOPE_MAC_DOMAIN + _canonical_json(protected),
            "sha256",
        ).hexdigest()

    def _quarantine(self, path: Path) -> None:
        """Best-effort removal of unauthenticated volume data from discovery.

        A journal record is never trusted merely because its permissions look
        private on a Network Volume.  Quarantine failure is deliberately not
        fatal: it remains absent and the trusted coordinator can resubmit it.
        """

        try:
            quarantine = self._directory / ".quarantine"
            quarantine.mkdir(mode=0o700, exist_ok=True)
            if quarantine.is_symlink() or not quarantine.is_dir():
                return
            os.replace(path, quarantine / path.name)
        except OSError:
            return


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _is_strict_envelope(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"schema_version", "record", "mac"}:
        return False
    return (
        value["schema_version"] == _ENVELOPE_SCHEMA_VERSION
        and isinstance(value["record"], dict)
        and isinstance(value["mac"], str)
        and _MAC.fullmatch(value["mac"]) is not None
    )
