#!/usr/bin/env bash
# Source this from the caller's shell before Surya client commands.  Cache
# locations stay package-local; server lifecycle remains caller-managed.
_common_ir_surya_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${HF_HOME:=$_common_ir_surya_root/.runtime/models/huggingface}"
: "${HF_HUB_CACHE:=$HF_HOME/hub}"
: "${MODEL_CACHE_DIR:=$_common_ir_surya_root/.runtime/models/datalab}"
export HF_HOME HF_HUB_CACHE MODEL_CACHE_DIR
mkdir -p "$HF_HOME" "$HF_HUB_CACHE" "$MODEL_CACHE_DIR"
unset _common_ir_surya_root
