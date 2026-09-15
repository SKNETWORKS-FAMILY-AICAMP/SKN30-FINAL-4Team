#!/usr/bin/env bash
# Build one Bizinfo HWP/HWPX announcement into Common IR and a validated
# existing-program profile.  PDF is deliberately rejected by this runner.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /absolute/path/to/PBLN_..." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NOTICE_DIR="$(cd "$1" && pwd)"
METADATA="$NOTICE_DIR/metadata.json"
PIPELINE_DIR="$NOTICE_DIR/pipeline"

[[ -f "$METADATA" ]] || { echo "metadata.json is required: $METADATA" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
[[ -n "${OPENAI_API_KEY:-}" ]] || { echo "OPENAI_API_KEY must be set" >&2; exit 2; }

NOTICE_ID="$(jq -er '.notice_id' "$METADATA")"
ANALYSIS_INPUT="$(jq -er '(.analysis_input | if type == "object" then .path else . end)' "$METADATA")"
INPUT_PATH="$NOTICE_DIR/attachments/$ANALYSIS_INPUT"
SOURCE_KIND="${ANALYSIS_INPUT##*.}"
SOURCE_KIND="${SOURCE_KIND,,}"

case "$SOURCE_KIND" in
  hwp|hwpx) ;;
  *)
    echo "Only HWP/HWPX analysis inputs are eligible; got: $ANALYSIS_INPUT" >&2
    exit 2
    ;;
esac
[[ -f "$INPUT_PATH" ]] || { echo "Analysis input does not exist: $INPUT_PATH" >&2; exit 2; }

COMMON_IR="$PIPELINE_DIR/common_ir_v1/$NOTICE_ID.$SOURCE_KIND.json"
SECTION_SCOPES="$PIPELINE_DIR/section_scopes.json"
BLOCK_CANDIDATES="$PIPELINE_DIR/block_candidates.json"
SOURCE_SELECTION="$PIPELINE_DIR/source_selection.json"
PROFILE="$PIPELINE_DIR/structured_profile.v0.2.json"
INGESTION_RECORD="$PIPELINE_DIR/ingestion_record.v0.1.json"

mkdir -p "$PIPELINE_DIR"
if [[ ! -f "$COMMON_IR" ]]; then
  # The Common IR runner intentionally refuses an existing run directory.
  # A prior interrupted setup can leave an empty directory; remove only that
  # exact, verified-empty directory so the run remains reproducible.
  if [[ -d "$PIPELINE_DIR" ]] && [[ -z "$(find "$PIPELINE_DIR" -mindepth 1 -print -quit)" ]]; then
    rmdir "$PIPELINE_DIR"
  elif [[ -d "$PIPELINE_DIR" ]]; then
    echo "Common IR is missing but pipeline directory is non-empty: $PIPELINE_DIR" >&2
    echo "Inspect the partial artifacts; do not delete them automatically." >&2
    exit 1
  fi
  "$ROOT/backend/scripts/run_common_ir_hwp.sh" \
    --input "$INPUT_PATH" \
    --source-kind "$SOURCE_KIND" \
    --notice-id "$NOTICE_ID" \
    --run-dir "$PIPELINE_DIR"
fi

SEMANTIC_ROOT="$ROOT/backend/vendor/portable_existing_request_profiles_20260831"
SEMANTIC_PYTHON="$SEMANTIC_ROOT/.venv/bin/python"
[[ -x "$SEMANTIC_PYTHON" ]] || { echo "Semantic environment is missing: $SEMANTIC_PYTHON" >&2; exit 2; }
export OPENAI_MODEL="${OPENAI_MODEL:-gpt-5.6-luna}"

cd "$SEMANTIC_ROOT"
if [[ ! -f "$SECTION_SCOPES" ]]; then
  "$SEMANTIC_PYTHON" -m semantic_structuring.run_section_scope_discovery_test \
    --notice-id "$NOTICE_ID" --common-ir "$COMMON_IR" --output "$SECTION_SCOPES"
fi
if [[ ! -f "$BLOCK_CANDIDATES" ]]; then
  "$SEMANTIC_PYTHON" -m semantic_structuring.run_block_candidate_discovery_test \
    --notice-id "$NOTICE_ID" --common-ir "$COMMON_IR" \
    --section-scope-artifact "$SECTION_SCOPES" --output "$BLOCK_CANDIDATES"
fi
if [[ ! -f "$SOURCE_SELECTION" ]]; then
  "$SEMANTIC_PYTHON" -m semantic_structuring.run_source_selection_test \
    --notice-id "$NOTICE_ID" --common-ir "$COMMON_IR" \
    --section-scope-artifact "$SECTION_SCOPES" --router-artifact "$BLOCK_CANDIDATES" \
    --output "$SOURCE_SELECTION"
fi
if [[ ! -f "$PROFILE" ]]; then
  "$SEMANTIC_PYTHON" -m semantic_structuring.run_final_profile_assembler \
    --common-ir "$COMMON_IR" --selection-artifact "$SOURCE_SELECTION" \
    --profile-version v0.2 --output "$PROFILE"
fi

jq -n \
  --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg analysis_input "$ANALYSIS_INPUT" \
  --arg common_ir "pipeline/common_ir_v1/$NOTICE_ID.$SOURCE_KIND.json" \
  --slurpfile portal_metadata "$METADATA" \
  --slurpfile structured_profile "$PROFILE" \
  '{schema_version: "bizinfo_existing_ingestion_record/v0.1", generated_at: $generated_at,
    portal_metadata: $portal_metadata[0],
    analysis: {input_path: $analysis_input, common_ir_path: $common_ir},
    structured_profile: $structured_profile[0]}' > "$INGESTION_RECORD"

echo "Completed: $INGESTION_RECORD"
