#!/usr/bin/env python3
"""Validate and sequentially ingest every record in an Existing KB data pack.

Without ``--execute`` this command is a side-effect-free preflight.  The
mutating mode delegates each record to ``ingest_existing_profile.py`` and
emits one valid JSON line per record plus a final summary.  Credentials are
read from the environment (or ``backend/.env``) and are never passed as
command-line arguments or printed.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from .existing_kb_pack import PackValidationError, validate_directory
    from .local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings
except ImportError:  # direct ``python scripts/...`` execution
    from existing_kb_pack import PackValidationError, validate_directory
    from local_supabase_env import LocalSupabaseEnvError, load_local_supabase_settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SINGLE_IMPORTER = Path(__file__).with_name("ingest_existing_profile.py")


def _load_backend_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(BACKEND_ROOT / ".env", override=False)


def _parse_result(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("single-record importer did not emit exactly one JSON result")
    value = json.loads(lines[0])
    if not isinstance(value, dict) or value.get("status") not in {"ingested", "already_ingested"}:
        raise ValueError("single-record importer returned an unexpected result")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_root", type=Path, help="검증을 마친 추출 루트")
    parser.add_argument("--expected-count", type=int, help="전체 팩의 예상 공고 수")
    parser.add_argument("--limit", type=int, help="검증 후 앞 N건만 적재(연결 smoke test용)")
    parser.add_argument("--continue-on-error", action="store_true", help="한 건 실패 후에도 다음 건 계속")
    parser.add_argument("--supabase-compose-env", type=Path, help="호스트 실행용 공식 Supabase Compose .env")
    parser.add_argument("--execute", action="store_true", help="실제로 private Storage와 kb.*를 변경")
    args = parser.parse_args()
    if args.expected_count is not None and args.expected_count < 1:
        parser.error("--expected-count must be positive")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    try:
        summary = validate_directory(args.pack_root, expected_count=args.expected_count)
    except PackValidationError as error:
        print(json.dumps({"status": "invalid", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1

    records = sorted(args.pack_root.resolve().glob("*/pipeline/ingestion_record.v0.1.json"))
    selected = records[: args.limit] if args.limit is not None else records
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "validated_only",
                    "notices_in_pack": summary.notices,
                    "records_selected": len(selected),
                    "execute_required_for_import": True,
                },
                ensure_ascii=False,
            )
        )
        return 0

    _load_backend_env()
    if args.supabase_compose_env:
        try:
            os.environ.update(load_local_supabase_settings(args.supabase_compose_env))
        except LocalSupabaseEnvError as error:
            parser.error(str(error))
    service_key_present = any(
        str(os.environ.get(name) or "").strip()
        for name in ("SUPABASE_SERVICE_ROLE_KEY", "SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY")
    )
    database_present = any(
        str(os.environ.get(name) or "").strip()
        for name in ("SUPABASE_DB_URL", "DATABASE_URL")
    )
    if not service_key_present or not database_present:
        parser.error(
            "mutating import requires a service-role key and SUPABASE_DB_URL/DATABASE_URL "
            "in the environment or backend/.env"
        )

    statuses: Counter[str] = Counter()
    failures = 0
    for ordinal, record in enumerate(selected, start=1):
        completed = subprocess.run(
            [sys.executable, str(SINGLE_IMPORTER), str(record)],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        row: dict[str, Any] = {
            "ordinal": ordinal,
            "record": record.relative_to(args.pack_root.resolve()).as_posix(),
            "returncode": completed.returncode,
        }
        if completed.returncode == 0:
            try:
                result = _parse_result(completed.stdout)
            except (ValueError, json.JSONDecodeError) as error:
                failures += 1
                row["error"] = str(error)
            else:
                row["result"] = result
                statuses[str(result["status"])] += 1
        else:
            failures += 1
            # Child stderr may contain an upstream HTTP response body or a DB
            # diagnostic.  Keep batch logs shareable by never copying it.
            row["error"] = "single-record importer failed; run that record directly on the trusted server for details"
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if failures and not args.continue_on_error:
            break

    processed = sum(statuses.values()) + failures
    print(
        json.dumps(
            {
                "status": "completed" if failures == 0 else "failed",
                "records_selected": len(selected),
                "records_processed": processed,
                "ingested": statuses["ingested"],
                "already_ingested": statuses["already_ingested"],
                "failed": failures,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
