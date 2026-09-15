# RunPod HWP/HWPX Profile Pipeline

This portable bundle runs without Docker and does not require a GPU for its
active path:

```text
HWP/HWPX + metadata.json -> Common IR v1 -> existing_program_profile/v0.2
```

OpenAI is used for semantic selection. The profile assembler and contract
validation are local. PDF, OCR, Surya, Supabase, and Docker Compose are not
included in this bundle.

## Install

Use a Linux x86_64 RunPod image with Python 3.12, `uv`, `jq`, and the host
FreeType shared library. A GPU is unnecessary for HWP/HWPX processing.

```bash
apt-get update && apt-get install -y jq libfreetype6
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

./backend/scripts/setup_hwp_profile_pipeline.sh
cp .env.example .env
# Edit .env locally and set OPENAI_API_KEY. Never put the key in an image.
```

If the image stores FreeType outside `/lib/x86_64-linux-gnu`, set its actual
path before running the pipeline:

```bash
export PREREVIEW_FREETYPE_LIB=/path/to/libfreetype.so.6
```

## Input directory

Each notice directory must follow this minimum shape:

```text
PBLN_.../
  metadata.json
  attachments/
    selected-notice.hwp-or-hwpx
```

`metadata.json` needs `notice_id` and `analysis_input`. `analysis_input` may
be a filename string or an object with `path`. Keep all Bizinfo list/detail
metadata in the same JSON; it is copied unchanged to the final ingestion
record.

```json
{
  "notice_id": "PBLN_000000000125982",
  "analysis_input": "selected-notice.hwpx"
}
```

## Run and resume

```bash
set -a; . ./.env; set +a
./backend/scripts/run_existing_hwp_pipeline.sh /data/PBLN_000000000125982
```

Outputs are stored under the notice's `pipeline/` directory. Re-running uses
valid stage artifacts already present and does not repeat completed model
calls. The final handoff artifact is:

```text
pipeline/ingestion_record.v0.1.json
```

It contains independent `portal_metadata`, analysis lineage, and the
validated `existing_program_profile/v0.2`. Portal values are not injected
into Common IR spans or model-generated evidence.

## Optional private Supabase KB ingestion

After the pipeline completes, a trusted worker can upload its artifacts and
write the structured result to `kb.*`:

```bash
export SUPABASE_URL=https://your-supabase-api.example
export SUPABASE_SERVICE_ROLE_KEY=...
export SUPABASE_DB_URL='postgresql://trusted_writer:...@db-host:5432/postgres'

./backend/vendor/portable_existing_request_profiles_20260831/.venv/bin/python \
  ./backend/scripts/ingest_existing_profile.py \
  /data/PBLN_.../pipeline/ingestion_record.v0.1.json
```

`SUPABASE_DB_URL` is a server-only credential. The importer uses it for
`kb.*` writes, so `kb` does not need to be exposed through PostgREST. It
uploads the original source, Common IR, source selection, structured profile,
and ingestion record to the private `existing-kb` bucket.

## Constraints

- Only `.hwp` and `.hwpx` are accepted by this runner.
- PDF/OCR/Surya are intentionally out of scope for this portable bundle.
- `rhwp-python` can require the FreeType preload handled by the runner.
- The OpenAI key is required only for stages that are not already completed.
